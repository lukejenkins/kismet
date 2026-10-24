/* test_diag_bridgeq.c - standalone tests for the decoder-bridge queue.
 *
 * Links diag_bridgeq.c (libc only). The "bridge" is a real pipe with an
 * O_NONBLOCK write end, the exact fd kind the helper writes: a reader that is
 * not reading is a full pipe, which is the failure this queue exists for.
 */

#include "diag_bridgeq.h"

#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int failures = 0;

#define CHECK(cond, ...) do { \
    if (!(cond)) { \
        fprintf(stderr, "FAIL %s:%d: ", __FILE__, __LINE__); \
        fprintf(stderr, __VA_ARGS__); \
        fprintf(stderr, "\n"); \
        failures++; \
    } \
} while (0)

static void mkpipe(int p[2]) {
    if (pipe(p) != 0) { perror("pipe"); exit(2); }
    fcntl(p[1], F_SETFL, fcntl(p[1], F_GETFL, 0) | O_NONBLOCK);
    fcntl(p[0], F_SETFL, fcntl(p[0], F_GETFL, 0) | O_NONBLOCK);
}

/* A chunk whose bytes encode their stream offset, so order and completeness
 * can be checked byte for byte on the far side. */
static void fill(uint8_t *b, size_t n, uint64_t base) {
    for (size_t i = 0; i < n; i++)
        b[i] = (uint8_t)((base + i) * 131u >> 3);
}

/* Read everything the pipe holds, appending to out; returns bytes read. */
static size_t drain(int rfd, uint8_t *out, size_t cap, size_t have) {
    size_t got = 0;
    for (;;) {
        ssize_t r = read(rfd, out + have + got, cap - have - got);
        if (r <= 0)
            break;
        got += (size_t)r;
    }
    return got;
}

/* The decoder keeps up: nothing is ever queued or dropped, and the pipe sees
 * exactly the stream. */
static void test_fast_reader_passes_straight_through(void) {
    int p[2]; mkpipe(p);
    diag_bridgeq_t q; diag_bridgeq_init(&q, 1 << 20);
    uint8_t chunk[4096], *seen = malloc(1 << 20);
    size_t seen_n = 0;
    uint64_t off = 0;
    for (int i = 0; i < 64; i++) {
        fill(chunk, sizeof(chunk), off);
        CHECK(diag_bridgeq_push(&q, p[1], chunk, sizeof(chunk)) == DIAG_BRIDGEQ_OK,
              "push %d not OK", i);
        off += sizeof(chunk);
        seen_n += drain(p[0], seen, 1 << 20, seen_n);
    }
    CHECK(q.dropped_chunks == 0 && q.dropped_bytes == 0, "dropped with a fast reader");
    CHECK(q.peak == 0, "queued %zu B with a reader that kept up", q.peak);
    CHECK(q.buf == NULL, "allocated a ring nobody needed");
    CHECK(seen_n == off, "pipe saw %zu of %llu B", seen_n, (unsigned long long)off);
    uint8_t *want = malloc(off); fill(want, off, 0);
    CHECK(memcmp(seen, want, off) == 0, "stream reordered or corrupted");
    free(want); free(seen);
    diag_bridgeq_free(&q); close(p[0]); close(p[1]);
}

/* The decoder stalls: push never blocks, the backlog queues, and once it reads
 * again the pipe sees the whole stream in order. */
static void test_stalled_reader_queues_without_blocking(void) {
    int p[2]; mkpipe(p);
    diag_bridgeq_t q; diag_bridgeq_init(&q, 4 << 20);
    uint8_t chunk[65536];
    uint64_t off = 0;
    /* 1 MiB into a pipe nobody reads: ~16x its capacity. A blocking write
     * would never return here; the test would hang, not fail. */
    for (int i = 0; i < 16; i++) {
        fill(chunk, sizeof(chunk), off);
        CHECK(diag_bridgeq_push(&q, p[1], chunk, sizeof(chunk)) == DIAG_BRIDGEQ_OK,
              "push %d not OK", i);
        off += sizeof(chunk);
    }
    CHECK(diag_bridgeq_pending(&q) > 0, "nothing queued behind a full pipe");
    CHECK(q.dropped_chunks == 0, "dropped with room in the ring");
    CHECK(q.bytes_in == off, "bytes_in %llu != %llu", (unsigned long long)q.bytes_in,
          (unsigned long long)off);

    uint8_t *seen = malloc(off); size_t seen_n = 0;
    for (int spins = 0; spins < 1000 && (diag_bridgeq_pending(&q) || seen_n < off); spins++) {
        seen_n += drain(p[0], seen, off, seen_n);
        CHECK(diag_bridgeq_flush(&q, p[1]) == 0, "flush reported the pipe gone");
    }
    seen_n += drain(p[0], seen, off, seen_n);
    CHECK(seen_n == off, "pipe saw %zu of %llu B", seen_n, (unsigned long long)off);
    CHECK(q.bytes_out == off, "bytes_out %llu", (unsigned long long)q.bytes_out);
    uint8_t *want = malloc(off); fill(want, off, 0);
    CHECK(seen_n == off && memcmp(seen, want, off) == 0, "stream reordered or corrupted");
    free(want); free(seen);
    diag_bridgeq_free(&q); close(p[0]); close(p[1]);
}

/* The ring fills: a chunk that does not fit is dropped WHOLE and counted, and
 * every chunk that is delivered is delivered whole and in order. */
static void test_overflow_drops_whole_chunks_and_counts_them(void) {
    int p[2]; mkpipe(p);
    enum { CH = 1000 };
    diag_bridgeq_t q; diag_bridgeq_init(&q, 10 * CH);   /* tiny ring */
    uint8_t chunk[CH];
    uint64_t off = 0;
    int dropped = 0, first_drop = -1;
    /* Fill the pipe itself first so everything after it must queue. */
    for (int i = 0; i < 400; i++) {
        fill(chunk, CH, off);
        int r = diag_bridgeq_push(&q, p[1], chunk, CH);
        CHECK(r == DIAG_BRIDGEQ_OK || r == DIAG_BRIDGEQ_DROPPED, "push %d = %d", i, r);
        if (r == DIAG_BRIDGEQ_DROPPED) {
            if (first_drop < 0) first_drop = i;
            dropped++;
        }
        off += CH;
    }
    CHECK(dropped > 0, "a 10-chunk ring behind a full pipe never dropped");
    CHECK(q.dropped_chunks == (uint64_t)dropped, "dropped_chunks %llu != %d",
          (unsigned long long)q.dropped_chunks, dropped);
    CHECK(q.dropped_bytes == (uint64_t)dropped * CH, "dropped_bytes not whole chunks");
    CHECK(q.first_drop_offset == (uint64_t)first_drop * CH,
          "first_drop_offset %llu != %llu", (unsigned long long)q.first_drop_offset,
          (unsigned long long)first_drop * CH);
    CHECK(q.peak <= 10 * CH, "ring overfilled: peak %zu", q.peak);

    /* Everything delivered = bytes_in - dropped, and it is whole chunks. */
    uint8_t *seen = malloc(off); size_t seen_n = 0;
    for (int spins = 0; spins < 1000; spins++) {
        seen_n += drain(p[0], seen, off, seen_n);
        diag_bridgeq_flush(&q, p[1]);
        if (!diag_bridgeq_pending(&q)) { seen_n += drain(p[0], seen, off, seen_n); break; }
    }
    CHECK(seen_n == off - q.dropped_bytes, "delivered %zu, expected %llu", seen_n,
          (unsigned long long)(off - q.dropped_bytes));
    CHECK(seen_n % CH == 0, "a chunk was split: %zu B delivered", seen_n);
    /* Each delivered chunk is one of the offered ones, intact, in order. */
    uint64_t want_off = 0; size_t pos = 0;
    while (pos + CH <= seen_n) {
        uint8_t ref[CH]; int matched = 0;
        while (want_off < off) {
            fill(ref, CH, want_off);
            want_off += CH;
            if (memcmp(seen + pos, ref, CH) == 0) { matched = 1; break; }
        }
        CHECK(matched, "delivered chunk at %zu matches no offered chunk in order", pos);
        if (!matched) break;
        pos += CH;
    }
    free(seen);
    diag_bridgeq_free(&q); close(p[0]); close(p[1]);
}

/* A backlog is written before a new chunk: the pipe never sees them reordered. */
static void test_backlog_goes_before_the_next_chunk(void) {
    int p[2]; mkpipe(p);
    diag_bridgeq_t q; diag_bridgeq_init(&q, 1 << 20);
    uint8_t big[200000]; fill(big, sizeof(big), 0);
    CHECK(diag_bridgeq_push(&q, p[1], big, sizeof(big)) == DIAG_BRIDGEQ_OK, "big push");
    CHECK(diag_bridgeq_pending(&q) > 0, "a 200 KB chunk fit a pipe?");
    /* Make room in the pipe, then push a small chunk: it must go AFTER the
     * backlog, even though the pipe now has room for it directly. */
    uint8_t *seen = malloc(sizeof(big) + 100); size_t seen_n = 0;
    seen_n += drain(p[0], seen, sizeof(big) + 100, seen_n);
    uint8_t small[100]; fill(small, sizeof(small), sizeof(big));
    CHECK(diag_bridgeq_push(&q, p[1], small, sizeof(small)) == DIAG_BRIDGEQ_OK, "small push");
    for (int spins = 0; spins < 1000 && seen_n < sizeof(big) + 100; spins++) {
        diag_bridgeq_flush(&q, p[1]);
        seen_n += drain(p[0], seen, sizeof(big) + 100, seen_n);
    }
    uint8_t want[sizeof(big) + 100]; fill(want, sizeof(want), 0);
    CHECK(seen_n == sizeof(want) && memcmp(seen, want, sizeof(want)) == 0,
          "backlog and new chunk reordered (%zu B seen)", seen_n);
    free(seen);
    diag_bridgeq_free(&q); close(p[0]); close(p[1]);
}

/* A full ring whose pipe has since drained: the push writes the backlog FIRST
 * and so finds room. Checking room before writing would drop a chunk the queue
 * could hold -- the ordering is kept either way (the len == 0 gate), so this is
 * what the flush-first buys (a mutation pass found the ordering test could not
 * see it). */
static void test_the_backlog_is_written_before_room_is_judged(void) {
    int p[2]; mkpipe(p);
    enum { CH = 1000 };
    diag_bridgeq_t q; diag_bridgeq_init(&q, 10 * CH);
    uint8_t chunk[CH]; fill(chunk, CH, 0);
    /* Fill the pipe, then the ring, with nobody reading. */
    int i;
    for (i = 0; i < 1000; i++)
        if (diag_bridgeq_push(&q, p[1], chunk, CH) == DIAG_BRIDGEQ_DROPPED)
            break;
    CHECK(i < 1000, "never filled the ring");
    CHECK(!diag_bridgeq_has_room(&q, CH), "the ring should be full now");
    uint64_t dropped = q.dropped_chunks;
    /* The decoder catches up on the PIPE only; nothing has flushed the ring. */
    uint8_t *sink = malloc(1 << 20);
    drain(p[0], sink, 1 << 20, 0);
    CHECK(diag_bridgeq_push(&q, p[1], chunk, CH) == DIAG_BRIDGEQ_OK,
          "dropped a chunk the drained pipe had room for");
    CHECK(q.dropped_chunks == dropped, "counted a drop that need not happen");
    free(sink);
    diag_bridgeq_free(&q); close(p[0]); close(p[1]);
}

/* The decoder died: GONE, the old write_all() contract the callers goto on. */
static void test_a_dead_reader_is_gone(void) {
    int p[2]; mkpipe(p);
    close(p[0]);
    diag_bridgeq_t q; diag_bridgeq_init(&q, 0);
    uint8_t chunk[16] = {0};
    CHECK(diag_bridgeq_push(&q, p[1], chunk, sizeof(chunk)) == DIAG_BRIDGEQ_GONE,
          "a closed reader was not reported gone");
    diag_bridgeq_free(&q); close(p[1]);
}

static void test_has_room(void) {
    diag_bridgeq_t q; diag_bridgeq_init(&q, 100);
    CHECK(diag_bridgeq_has_room(&q, 100), "an empty 100 B ring has no room for 100");
    CHECK(!diag_bridgeq_has_room(&q, 101), "room for more than the ring");
    q.len = 60;
    CHECK(diag_bridgeq_has_room(&q, 40) && !diag_bridgeq_has_room(&q, 41), "room arithmetic");
    CHECK(!diag_bridgeq_has_room(&q, (size_t)-1), "overflowing length reported as room");
    q.len = 0;
    diag_bridgeq_free(&q);
    diag_bridgeq_init(&q, 0);
    CHECK(q.cap == DIAG_BRIDGEQ_DEFAULT_CAP, "cap 0 did not pick the default");
    diag_bridgeq_free(&q);
}

int main(void) {
    signal(SIGPIPE, SIG_IGN);   /* as the helper does: EPIPE, not a signal */
    test_fast_reader_passes_straight_through();
    test_stalled_reader_queues_without_blocking();
    test_overflow_drops_whole_chunks_and_counts_them();
    test_backlog_goes_before_the_next_chunk();
    test_the_backlog_is_written_before_room_is_judged();
    test_a_dead_reader_is_gone();
    test_has_room();
    if (failures) {
        fprintf(stderr, "test_diag_bridgeq: %d FAILED\n", failures);
        return 1;
    }
    printf("test_diag_bridgeq: all passed\n");
    return 0;
}
