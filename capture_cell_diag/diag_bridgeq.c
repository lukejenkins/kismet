/* diag_bridgeq.c - see diag_bridgeq.h. */
#include "diag_bridgeq.h"

#include <errno.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

void diag_bridgeq_init(diag_bridgeq_t *q, size_t cap) {
    memset(q, 0, sizeof(*q));
    q->cap = cap ? cap : DIAG_BRIDGEQ_DEFAULT_CAP;
}

void diag_bridgeq_free(diag_bridgeq_t *q) {
    free(q->buf);
    q->buf = NULL;
    q->head = q->len = 0;
}

size_t diag_bridgeq_pending(const diag_bridgeq_t *q) {
    return q->len;
}

int diag_bridgeq_has_room(const diag_bridgeq_t *q, size_t len) {
    return len <= q->cap && q->len <= q->cap - len;
}

/* One non-blocking write. >= 0 bytes taken (0 = would block), -1 = gone. */
static ssize_t take(int fd, const uint8_t *p, size_t n) {
    for (;;) {
        ssize_t w = write(fd, p, n);
        if (w >= 0)
            return w;
        if (errno == EINTR)
            continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK)
            return 0;
        return -1;   /* EPIPE etc.: the decoder is gone */
    }
}

int diag_bridgeq_flush(diag_bridgeq_t *q, int fd) {
    while (q->len > 0) {
        /* The contiguous run from head: up to the end of the ring, or the
         * whole queue if it does not wrap. */
        size_t run = q->cap - q->head;
        if (run > q->len)
            run = q->len;
        ssize_t w = take(fd, q->buf + q->head, run);
        if (w < 0)
            return -1;
        if (w == 0)
            return 0;
        q->head = (q->head + (size_t)w) % q->cap;
        q->len -= (size_t)w;
        q->bytes_out += (uint64_t)w;
    }
    q->head = 0;   /* empty: restart at the front, so the next run is long */
    return 0;
}

static int enqueue(diag_bridgeq_t *q, const uint8_t *p, size_t n) {
    if (n == 0)
        return 0;
    if (q->buf == NULL) {
        q->buf = malloc(q->cap);
        if (q->buf == NULL)
            return -1;
        q->head = q->len = 0;
    }
    size_t tail = (q->head + q->len) % q->cap;
    size_t first = q->cap - tail;
    if (first > n)
        first = n;
    memcpy(q->buf + tail, p, first);
    memcpy(q->buf, p + first, n - first);
    q->len += n;
    if (q->len > q->peak)
        q->peak = q->len;
    return 0;
}

static void drop(diag_bridgeq_t *q, size_t len) {
    if (q->dropped_chunks == 0)
        q->first_drop_offset = q->bytes_in;
    q->dropped_chunks++;
    q->dropped_bytes += (uint64_t)len;
}

int diag_bridgeq_push(diag_bridgeq_t *q, int fd, const uint8_t *buf, size_t len) {
    if (len == 0)
        return DIAG_BRIDGEQ_OK;

    /* The backlog first: the pipe must see the stream in order, and every byte
     * it takes from the backlog is room for this chunk. */
    if (diag_bridgeq_flush(q, fd) != 0)
        return DIAG_BRIDGEQ_GONE;

    /* Decided before a byte is written, assuming the pipe takes none of it --
     * so the chunk is either delivered whole (written + queued) or dropped
     * whole, never split between the two. */
    if (!diag_bridgeq_has_room(q, len)) {
        drop(q, len);
        q->bytes_in += (uint64_t)len;
        return DIAG_BRIDGEQ_DROPPED;
    }

    size_t off = 0;
    if (q->len == 0) {
        ssize_t w = take(fd, buf, len);
        if (w < 0)
            return DIAG_BRIDGEQ_GONE;
        off = (size_t)w;
        q->bytes_out += (uint64_t)w;
    }
    if (enqueue(q, buf + off, len - off) != 0) {
        /* No memory for the ring. Nothing of this chunk went out unless the
         * pipe took a head of it, and a head alone is the split this module
         * exists to avoid -- but it cannot be unwritten. Count the remainder as
         * dropped; the counters say so and the decoder resyncs at the next
         * 0x7E. */
        drop(q, len - off);
        q->bytes_in += (uint64_t)len;
        return DIAG_BRIDGEQ_DROPPED;
    }
    q->bytes_in += (uint64_t)len;
    return DIAG_BRIDGEQ_OK;
}
