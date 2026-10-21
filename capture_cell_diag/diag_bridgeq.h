/* diag_bridgeq.h - the bounded queue between the DIAG read loop and the
 * Python decode bridge.
 *
 * A BLOCKING write of every chunk into the bridge's stdin pipe is not safe.
 * When the decoder falls behind a DIAG burst the 64 KiB pipe fills, the loop
 * stops reading the tty, the USB-serial driver's buffers overflow, and the
 * modem's bytes are dropped with nothing in the kernel log. With several DIAG
 * sources on one host this costs a majority of a modem's frames to CRC damage,
 * while host load alone does not.
 *
 * This queue decouples the two. The pipe is O_NONBLOCK; a chunk is written
 * straight through when the pipe takes it, and the remainder is queued. The
 * poll loop drains the queue as POLLOUT allows. The read loop never waits on
 * the decoder.
 *
 * What overflow drops is DECODER INPUT, never capture: the rawlog= tee, the
 * kismetdb raw stream, the native tap and the CRC census all see every
 * byte before this queue is involved. A lagging decoder costs observations and
 * says so (the counters below); it never costs the capture.
 *
 * A chunk is taken WHOLE or dropped WHOLE. A partly-queued chunk would hand
 * the decoder a byte run that is neither the stream nor a clean cut of it. A
 * whole-chunk drop is a cut at a read boundary, which the decoder's HDLC
 * deframer resynchronises from at the next 0x7E, as it does from any gap.
 *
 * Pure libc, no Kismet types, so the selftest drives it against a real pipe.
 */
#ifndef DIAG_BRIDGEQ_H
#define DIAG_BRIDGEQ_H

#include <stddef.h>
#include <stdint.h>

/* 32 MiB: ~10 s of a 3 MB/s DIAG burst, ~100x a normal
 * stream's second. Allocated lazily, on the first byte that does not fit the
 * pipe, so a source whose decoder keeps up never pays for it. */
#define DIAG_BRIDGEQ_DEFAULT_CAP  (32u * 1024u * 1024u)

typedef struct {
    uint8_t *buf;                 /* ring storage; NULL until first needed */
    size_t   cap;                 /* ring size in bytes */
    size_t   head;                /* index of the oldest queued byte */
    size_t   len;                 /* bytes queued */

    uint64_t bytes_in;            /* bytes offered by the read loop */
    uint64_t bytes_out;           /* bytes written into the pipe */
    uint64_t dropped_bytes;       /* bytes refused because the ring was full */
    uint64_t dropped_chunks;      /* chunks refused (whole) */
    uint64_t first_drop_offset;   /* bytes_in at the first drop; valid when
                                   * dropped_chunks > 0 */
    size_t   peak;                /* the most bytes ever queued */
} diag_bridgeq_t;

/* Result of diag_bridgeq_push(). */
#define DIAG_BRIDGEQ_OK        0  /* all of it written or queued */
#define DIAG_BRIDGEQ_GONE    (-1) /* the pipe is gone (EPIPE etc.): the
                                   * decoder died; same contract as the old
                                   * write_all() */
#define DIAG_BRIDGEQ_DROPPED   1  /* the chunk did not fit and was dropped
                                   * whole; counted */

/* cap == 0 picks DIAG_BRIDGEQ_DEFAULT_CAP. Allocates nothing. */
void diag_bridgeq_init(diag_bridgeq_t *q, size_t cap);
void diag_bridgeq_free(diag_bridgeq_t *q);

/* Offer one chunk. `fd` must be O_NONBLOCK. Anything already queued is written
 * first, so the pipe sees the stream in order; then as much of `buf` as the
 * pipe takes; the rest is queued, or -- if it does not fit -- the WHOLE chunk
 * is dropped (see above). A chunk is never split between "dropped" and
 * "delivered". */
int diag_bridgeq_push(diag_bridgeq_t *q, int fd, const uint8_t *buf, size_t len);

/* Write queued bytes into `fd` until it would block or the queue is empty.
 * 0 = fine (check diag_bridgeq_pending), -1 = the pipe is gone. */
int diag_bridgeq_flush(diag_bridgeq_t *q, int fd);

/* Bytes queued. Non-zero means the poll loop should ask for POLLOUT. */
size_t diag_bridgeq_pending(const diag_bridgeq_t *q);

/* Would a chunk of `len` bytes be queued rather than dropped right now,
 * assuming the pipe takes none of it? A replay (whose input can wait) reads
 * only while this holds, so a replay never drops. */
int diag_bridgeq_has_room(const diag_bridgeq_t *q, size_t len);

#endif
