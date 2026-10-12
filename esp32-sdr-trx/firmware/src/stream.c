/*
 * Packet queue between the two DSP lanes and the USB endpoint.
 *
 * Each capture unit becomes one packet (see narrowband.h). Packets are
 * appended to a byte FIFO in unit order; core 0 drains the FIFO into the USB
 * Serial/JTAG endpoint from its idle loops. A packet that does not fit is
 * dropped whole, so the byte stream always stays aligned; the host sees the
 * gap in the packet's sample index.
 */
#include "stream.h"

#include "control.h"
#include "narrowband.h"
#include "board.h"
#include "platform.h"
#include "soc/usb_serial_jtag_reg.h"

#define FIFO_BYTES 12288u
#define USB_PACKET 64u
#define HEADER NB_HEADER_BYTES
#define STAGING_BYTES (HEADER + DSP_MAX_OUTPUTS * 4u)
#define CPU_CYCLES_PER_SECOND 240000000u
#define DRAIN_TIMEOUT_CYCLES (240u * 100000u) /* 100 ms without progress */

dsp_config stream_cfg = {.r2 = 4, .format = NB_FORMAT_CS8, .shift = 4};

static uint8_t staging[2][STAGING_BYTES] __attribute__((aligned(16)));
static uint8_t fifo[FIFO_BYTES] __attribute__((aligned(16)));

static struct {
    /* head_* are owned by whoever holds the commit turn, tail_* by core 0. */
    volatile uint32_t head_total, tail_total;
    uint32_t head_pos, tail_pos;
    volatile uint32_t commit_seq;
    volatile uint32_t dropped_units, fifo_peak;
    uint32_t dropped_halfword; /* saturating count carried in the packet header */
} q;

volatile uint32_t stream_slips; /* unit joins accepted with a small slip, see capture.c */
volatile uint32_t stream_abort; /* set by capture.c when a run has failed */

/* DC estimate for cs4, in 1/16 LSB of the cs16 samples: a first-order filter over the packets' means, updated in
 * packet order (in unit_body's commit turn). The radio's DC offset can be half the signal's RMS, which would waste
 * most of the 4-bit range. */
static volatile int32_t cs4_dc[2];
static volatile uint32_t cs4_dc_valid;
#define CS4_DC_SHIFT 3 /* time constant of 8 packets (8 ms) */

void stream_begin(void)
{
    q.head_total = q.tail_total = 0;
    q.head_pos = q.tail_pos = 0;
    q.commit_seq = 0;
    q.dropped_units = 0;
    q.fifo_peak = 0;
    stream_abort = 0;
    stream_slips = 0;
    cs4_dc[0] = cs4_dc[1] = 0;
    cs4_dc_valid = 0;
    memory_barrier();
}

uint32_t stream_stat(unsigned index)
{
    switch (index) {
    case NB_STAT_DROPPED: return q.dropped_units;
    case NB_STAT_FIFO_PEAK: return q.fifo_peak;
    case NB_STAT_SLIPS: return stream_slips;
    default: return 0;
    }
}

unsigned stream_set(unsigned op, uint32_t value, uint32_t *effective)
{
    switch (op) {
    case NB_SET_DECIM:
        if (value < 2 || value > 4)
            return CTL_BAD_ARGUMENT;
        stream_cfg.r2 = value;
        *effective = stream_cfg.r2;
        return CTL_OK;
    case NB_SET_FORMAT:
        if (value > NB_FORMAT_CS4)
            return CTL_BAD_ARGUMENT;
        stream_cfg.format = value;
        *effective = stream_cfg.format;
        return CTL_OK;
    case NB_SET_OUTSHIFT:
        if (value > 12)
            return CTL_BAD_ARGUMENT;
        stream_cfg.shift = value;
        *effective = stream_cfg.shift;
        return CTL_OK;
    default:
        return CTL_UNKNOWN_OP;
    }
}

/* ---- USB ------------------------------------------------------------------------------ */

static inline uint32_t fifo_fill(void) { return q.head_total - q.tail_total; }

/*
 * Writes one packet if the endpoint has room. A full 64-byte packet waits for the hardware's IN-EMPTY status, which the
 * register description defines as "up to 64 bytes can be written now", and then writes all of it without polling in
 * between (the hardware flushes a full packet by itself): 4 us instead of 10 us, and 0.87 instead of 0.77 MB/s on the test host.
 * A shorter packet uses the byte-wise path and WR_DONE.
 */
static inline int usb_send_packet(const uint8_t *p, unsigned n)
{
    if (n == USB_PACKET) {
        if (!(REG(USB_SERIAL_JTAG_INT_RAW_REG) & USB_SERIAL_JTAG_SERIAL_IN_EMPTY_INT_RAW))
            return 0;
        REG(USB_SERIAL_JTAG_INT_CLR_REG) = USB_SERIAL_JTAG_SERIAL_IN_EMPTY_INT_CLR;
        for (unsigned i = 0; i < n; i++)
            REG(USB_SERIAL_JTAG_EP1_REG) = p[i];
        return 1;
    }
    if (!(REG(USB_SERIAL_JTAG_EP1_CONF_REG) & USB_SERIAL_JTAG_SERIAL_IN_EP_DATA_FREE))
        return 0;
    REG(USB_SERIAL_JTAG_INT_CLR_REG) = USB_SERIAL_JTAG_SERIAL_IN_EMPTY_INT_CLR; /* keep the status honest for the next packet */
    for (unsigned i = 0; i < n; i++) {
        while (!(REG(USB_SERIAL_JTAG_EP1_CONF_REG) & USB_SERIAL_JTAG_SERIAL_IN_EP_DATA_FREE)) {
        }
        REG(USB_SERIAL_JTAG_EP1_REG) = p[i];
    }
    REG(USB_SERIAL_JTAG_EP1_CONF_REG) = USB_SERIAL_JTAG_WR_DONE;
    return 1;
}

/* Bench modes for comparison (NB_BENCH, mode 1 and 2): the old byte-wise write with a DATA_FREE poll per byte, and one DATA_FREE check
 * followed by 64 writes. */
static inline int usb_send_polled(const uint8_t *p, unsigned n)
{
    if (!(REG(USB_SERIAL_JTAG_EP1_CONF_REG) & USB_SERIAL_JTAG_SERIAL_IN_EP_DATA_FREE))
        return 0;
    for (unsigned i = 0; i < n; i++) {
        while (!(REG(USB_SERIAL_JTAG_EP1_CONF_REG) & USB_SERIAL_JTAG_SERIAL_IN_EP_DATA_FREE)) {
        }
        REG(USB_SERIAL_JTAG_EP1_REG) = p[i];
    }
    REG(USB_SERIAL_JTAG_EP1_CONF_REG) = USB_SERIAL_JTAG_WR_DONE;
    return 1;
}

static inline int usb_send_free(const uint8_t *p, unsigned n)
{
    if (!(REG(USB_SERIAL_JTAG_EP1_CONF_REG) & USB_SERIAL_JTAG_SERIAL_IN_EP_DATA_FREE))
        return 0;
    for (unsigned i = 0; i < n; i++)
        REG(USB_SERIAL_JTAG_EP1_REG) = p[i];
    return 1;
}

static unsigned pump(unsigned min_bytes)
{
    uint32_t fill = fifo_fill();
    if (fill < min_bytes || fill == 0)
        return 0;
    if (!(REG(USB_SERIAL_JTAG_EP1_CONF_REG) & USB_SERIAL_JTAG_SERIAL_IN_EP_DATA_FREE))
        return 0;
    unsigned n = fill < USB_PACKET ? fill : USB_PACKET;
    uint8_t chunk[USB_PACKET];
    unsigned pos = q.tail_pos;
    for (unsigned i = 0; i < n; i++) {
        chunk[i] = fifo[pos];
        if (++pos == FIFO_BYTES)
            pos = 0;
    }
    if (!usb_send_packet(chunk, n))
        return 0;
    q.tail_pos = pos;
    memory_barrier();
    q.tail_total += n;
    return n;
}

void stream_pump(void) { pump(USB_PACKET); }

void stream_finish(void)
{
    uint32_t since = cpu_cycles();
    while (fifo_fill()) {
        if (pump(1))
            since = cpu_cycles();
        else if (cpu_cycles() - since > DRAIN_TIMEOUT_CYCLES)
            break; /* host not reading: give up on the rest */
    }
}

/* ---- packets -------------------------------------------------------------------------- */

static inline __attribute__((always_inline)) void put16(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v;
    p[1] = (uint8_t)(v >> 8);
}

static inline __attribute__((always_inline)) void put32(uint8_t *p, uint32_t v)
{
    put16(p, v);
    put16(p + 2, v >> 16);
}

static const uint16_t cs4_mant[8] = NB_CS4_MANT;

/* num / den by shift and subtract: core 1 must not call the ROM's division helpers. */
static inline __attribute__((always_inline)) uint32_t udiv32(uint32_t num, uint32_t den)
{
    uint32_t quo = 0;
    for (int b = 31; b >= 0; b--) {
        if ((num >> b) >= den) {
            num -= den << b;
            quo |= 1u << b;
        }
    }
    return quo;
}

static inline __attribute__((always_inline)) int32_t sdiv32(int32_t num, uint32_t den)
{
    return num < 0 ? -(int32_t)udiv32((uint32_t)-num, den) : (int32_t)udiv32((uint32_t)num, den);
}

/*
 * Packs the packet's n cs16 samples at p into n cs4 bytes in place (byte k is written after sample k is read, and
 * k <= 4k), after removing the DC estimate. Returns the gain byte for the header (see narrowband.h) and the packet's
 * mean I and Q (1/16 LSB) in mean[]. Inlined into both cores' unit_body: no ROM calls, so no division and no 64-bit
 * arithmetic.
 */
static inline __attribute__((always_inline)) unsigned pack_cs4(uint8_t *p, unsigned n, int32_t *mean)
{
    const int32_t di = (cs4_dc[0] + 8) >> 4, dq = (cs4_dc[1] + 8) >> 4;
    int32_t si = 0, sq = 0;  /* signed sums: at most DSP_MAX_OUTPUTS * 32768 = 2^24 */
    uint32_t sum = 0;        /* sum of |I - dc| + |Q - dc|: at most 2 * 512 * 65535 < 2^26 */
    for (unsigned k = 0; k < n; k++) {
        int32_t i = (int16_t)(p[4 * k] | p[4 * k + 1] << 8), q = (int16_t)(p[4 * k + 2] | p[4 * k + 3] << 8);
        si += i;
        sq += q;
        i -= di;
        q -= dq;
        sum += (uint32_t)(i < 0 ? -i : i) + (uint32_t)(q < 0 ? -q : q);
    }
    mean[0] = mean[1] = 0;
    /* The largest gain g = M[j] / 2^(8+e) with g * mean|x| <= TARGET/100 LSB. mean|x| * 100 = sum * 50 / n
     * (sum * 50 < 2^32); the comparison drops 8 bits on the left so nothing overflows. */
    unsigned e = 0, j = 7;
    if (n) {
        mean[0] = sdiv32(si * 16, n);
        mean[1] = sdiv32(sq * 16, n);
        const uint32_t mean100 = udiv32(sum * 50u, n);
        for (e = 0; e < NB_CS4_MAX_E; e++) {
            for (j = 8; j-- > 0;)
                if (((uint32_t)cs4_mant[j] * mean100) >> 8 <= (NB_CS4_TARGET << e))
                    goto found;
        }
        j = 0;
    found:;
    }
    const int32_t m = cs4_mant[j];
    const unsigned sh = 8 + e;
    for (unsigned k = 0; k < n; k++) {
        int32_t i = (int16_t)(p[4 * k] | p[4 * k + 1] << 8) - di, q = (int16_t)(p[4 * k + 2] | p[4 * k + 3] << 8) - dq;
        int32_t vi = (i * m) >> sh, vq = (q * m) >> sh; /* floor: the host adds 0.5 */
        vi = vi < -8 ? -8 : vi > 7 ? 7 : vi;
        vq = vq < -8 ? -8 : vq > 7 ? 7 : vq;
        p[k] = (uint8_t)((vi & 15) | (vq & 15) << 4);
    }
    return e | j << 4;
}

/*
 * Body of stream_unit() and stream_unit_core1(), inlined into both so core 1
 * runs its copy from its own instruction bank and never fetches from core 0's.
 * `core1` is a constant at each call site.
 */
static inline __attribute__((always_inline)) void unit_body(const unsigned core1, uint32_t sequence,
                                                            const uint32_t *cur, const uint32_t *prev,
                                                            unsigned first, unsigned count, uint32_t start,
                                                            uint32_t out_start)
{
    uint8_t *buf = staging[core1];
    void (*idle)(void) = core1 ? 0 : stream_pump;
    unsigned n = (core1 ? dsp_unit_core1 : dsp_unit)(&stream_cfg, cur, prev, first, count, start,
                                                     sequence != 0, buf + HEADER, idle);
    unsigned gain = 0;
    int32_t mean[2] = {0, 0};
    if (stream_cfg.format == NB_FORMAT_CS4)
        gain = pack_cs4(buf + HEADER, n, mean);
    unsigned bytes_per = stream_cfg.format == NB_FORMAT_CS16 ? 4 : stream_cfg.format == NB_FORMAT_CS8 ? 2 : 1;
    unsigned payload = n * bytes_per;
    unsigned total = HEADER + payload;

    /* Wait for our turn: packets must enter the FIFO in sequence order. */
    while (q.commit_seq != sequence && !stream_abort) {
        if (!core1)
            stream_pump();
    }
    if (stream_abort)
        return;

    if (stream_cfg.format == NB_FORMAT_CS4 && n) { /* in packet order: update the DC estimate */
        if (!cs4_dc_valid) {
            cs4_dc[0] = mean[0];
            cs4_dc[1] = mean[1];
            cs4_dc_valid = 1;
        } else {
            cs4_dc[0] += (mean[0] - cs4_dc[0]) >> CS4_DC_SHIFT;
            cs4_dc[1] += (mean[1] - cs4_dc[1]) >> CS4_DC_SHIFT;
        }
    }

    uint32_t dropped = q.dropped_units;
    buf[0] = NB_MAGIC0;
    buf[1] = NB_MAGIC1;
    put16(buf + 2, stream_cfg.format | (stream_cfg.r2 << 8));
    put16(buf + 4, n);
    put16(buf + 6, dropped > 0xFFFF ? 0xFFFF : dropped);
    put32(buf + 8, sequence);
    put32(buf + 12, out_start);
    put16(buf + 16, stream_cfg.shift | gain << 8);
    uint32_t sum = 0;
    for (unsigned i = 0; i < HEADER - 2; i += 2)
        sum += buf[i] | (buf[i + 1] << 8);
    put16(buf + HEADER - 2, 0xFFFFu - (sum & 0xFFFFu));

    uint32_t fill = fifo_fill();
    if (FIFO_BYTES - fill >= total) {
        unsigned pos = q.head_pos;
        unsigned part = FIFO_BYTES - pos < total ? FIFO_BYTES - pos : total;
        for (unsigned i = 0; i < part; i++)
            fifo[pos + i] = buf[i];
        for (unsigned i = part; i < total; i++)
            fifo[i - part] = buf[i];
        pos += total;
        if (pos >= FIFO_BYTES)
            pos -= FIFO_BYTES;
        q.head_pos = pos;
        memory_barrier();
        q.head_total += total;
        if (fill + total > q.fifo_peak)
            q.fifo_peak = fill + total;
    } else {
        q.dropped_units++;
    }
    memory_barrier();
    q.commit_seq = sequence + 1;
}

void stream_unit(uint32_t sequence, const uint32_t *cur, const uint32_t *prev, unsigned first, unsigned count,
                 uint32_t start, uint32_t out_start)
{
    unit_body(0, sequence, cur, prev, first, count, start, out_start);
}

void CORE1_CODE stream_unit_core1(uint32_t sequence, const uint32_t *cur, const uint32_t *prev, unsigned first,
                                  unsigned count, uint32_t start, uint32_t out_start)
{
    unit_body(1, sequence, cur, prev, first, count, start, out_start);
}


#ifdef PROFILE
volatile uint32_t dsp_prof[4]; /* cycles per section of dsp_unit(), see dsp.c */
#endif
volatile uint32_t dsp_scalar_only;
/* ---- decimator timing ------------------------------------------------------------------ */

/* Runs outside a capture, on banks 0 and 1 (bank 3 holds the ROM's working memory). */
uint32_t stream_dsp_bench(uint32_t arg)
{
    if (arg & NB_DSP_VERIFY) { /* bytes that differ between the plain C and the SIMD FIR */
        uint32_t *c = (uint32_t *)CAPTURE_BANK_BASE, *p = (uint32_t *)(CAPTURE_BANK_BASE + CAPTURE_BANK_BYTES);
        uint32_t sd = 777;
        for (unsigned i = 0; i < DSP_RING_PAIRS; i++) {
            sd = sd * 1664525u + 1013904223u; c[i] = (sd >> 8) & 0xFFFFFu;
            sd = sd * 1664525u + 1013904223u; p[i] = (sd >> 8) & 0xFFFFFu;
        }
        uint8_t *ref = (uint8_t *)(CAPTURE_BANK_BASE + 2 * CAPTURE_BANK_BYTES);
        dsp_config vc = {.r2 = arg & 15, .format = (arg >> 4) & 1, .shift = 4};
        dsp_scalar_only = 1;
        unsigned n0 = (arg & 0x100 ? dsp_unit_core1 : dsp_unit)(&vc, c, p, 0, 16000, 0, 1, ref, 0);
        dsp_scalar_only = 0;
        unsigned n1 = (arg & 0x100 ? dsp_unit_core1 : dsp_unit)(&vc, c, p, 0, 16000, 0, 1, staging[0] + HEADER, 0);
        uint32_t bad = n0 != n1;
        for (unsigned i = 0; i < n0 * (vc.format ? 2 : 4) && i < DSP_MAX_OUTPUTS * 4; i++)
            bad += ref[i] != staging[0][HEADER + i];
        return bad;
    }
#ifdef PROFILE
    if (arg & NB_DSP_PROFILE_RESET)
        dsp_prof[0] = dsp_prof[1] = dsp_prof[2] = 0;
    if ((arg & 0xF000) == NB_DSP_PROFILE)
        return dsp_prof[(arg >> 4) & 3];
#endif
    uint32_t *cur = (uint32_t *)CAPTURE_BANK_BASE, *prev = (uint32_t *)(CAPTURE_BANK_BASE + CAPTURE_BANK_BYTES);
    uint32_t seed = 12345;
    for (unsigned i = 0; i < DSP_RING_PAIRS; i++) {
        seed = seed * 1664525u + 1013904223u;
        cur[i] = (seed >> 8) & 0xFFFFFu;
        seed = seed * 1664525u + 1013904223u;
        prev[i] = (seed >> 8) & 0xFFFFFu;
    }
    memory_barrier();
    dsp_config c = {.r2 = arg & 15, .format = (arg >> 4) & 15, .shift = 4};
    uint32_t t0 = cpu_cycles();
    (arg & 0x100 ? dsp_unit_core1 : dsp_unit)(&c, cur, prev, 0, 16000, 0, 1, staging[0] + HEADER, 0);
    return cpu_cycles() - t0;
}

/* ---- throughput test ------------------------------------------------------------------ */

/* Streams 64-byte blocks (0xBE, 0xEF, 16-bit counter, ...) as fast as the host takes them. */
uint32_t stream_bench(unsigned seconds, unsigned mode)
{
    const uint32_t second = CPU_CYCLES_PER_SECOND;
    uint32_t sent = 0, counter = 0;
    uint32_t second_start = cpu_cycles(), last_progress = second_start;
    unsigned elapsed = 0;
    uint8_t block[USB_PACKET];
    while (!serial_rx_pending()) {
        uint32_t now = cpu_cycles();
        if (now - second_start >= second) {
            second_start += second;
            if (++elapsed == seconds)
                break;
        }
        if (now - last_progress > DRAIN_TIMEOUT_CYCLES * 5)
            break;
        block[0] = 0xBE;
        block[1] = 0xEF;
        put16(block + 2, counter);
        for (unsigned i = 4; i < USB_PACKET; i++)
            block[i] = (uint8_t)(i * 3 + counter);
        int sent_one = mode == 0 ? usb_send_packet(block, USB_PACKET)
                       : mode == 1 ? usb_send_polled(block, USB_PACKET)
                                   : usb_send_free(block, USB_PACKET);
        if (sent_one) {
            counter++;
            sent += USB_PACKET;
            last_progress = cpu_cycles();
        }
    }
    return sent;
}
