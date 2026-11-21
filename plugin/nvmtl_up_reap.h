// nvmtl_up_reap.h: giving back finished asynchronous texture uploads without waiting (NVMTL_ASYNCUP_REAP).
//
// An asynchronous upload (nvmtl_tex_upload.h) keeps its record, its staging buffer and one of the process's
// NVMTL_UP_HELD_MAX places until something takes its handle from the image: the next upload to the same image, a
// readback, a CPU map, a surface-out or the image's death. A texture that is uploaded once and then only sampled is
// never taken from again while it lives, so it holds all of that for its whole life, and once 256 such textures
// exist every later upload to any other texture takes the synchronous path (submit and wait for the whole queue).
//
// The reaper gives back the records whose copy the GPU has already finished, at the next upload, without waiting:
//   - every stored record is also put on a first-in first-out list (in store order, which is close to submit order on
//     the single queue);
//   - a sweep walks the list from its oldest record and polls each fence with a zero timeout. It stops at the first
//     fence that has not signalled, so it never releases a copy still in flight. A record whose fence signalled because
//     a channel rebuild dropped its copy is taken off the list and left to the image's next take, which makes the copy
//     again (only the take has the image). The others are claimed (BUSY) under the lock and released outside it:
//     the submission is finished (its fence has signalled, so this does not block), the staging buffer destroyed, the
//     counts lowered and the big record freed;
//   - what stays is this small header, still named by the image's handle (an nvk_image is copied to new addresses by
//     the image grace and the parking lots, so nothing may point back at the image). The image's next take finds it
//     RELEASED and only frees it;
//   - a take that finds the record BUSY (the sweep is releasing it on another thread, or on this thread, from a grace
//     reap inside the release) does not wait and does not free it: the copy has finished, which is all the take needs
//     before it touches the image's memory, and the sweep frees the header when it is done (orphan).
// Every state change happens under the plugin's g_asyncup_mutex, so exactly one side frees each header and each record.
// With NVMTL_ASYNCUP_REAP=0 nothing is put on the list, every record stays LIVE and every take finishes it as before.
//
// Pure, so tools/test_up_reap.c holds the rules without a GPU; the plugin supplies the fence poll.

#ifndef NVMTL_UP_REAP_H
#define NVMTL_UP_REAP_H

#include <stdint.h>
#include <string.h>

enum { NVMTL_UP_LIVE = 0, NVMTL_UP_BUSY = 1, NVMTL_UP_RELEASED = 2 };

// What an image's handle names. `body` is the plugin's record (staging, command buffer, fence) while LIVE or BUSY, and
// NULL once RELEASED.
typedef struct nvmtl_up_rec {
    struct nvmtl_up_rec *prev, *next;
    void *body;
    uint8_t st, linked, orphan;
} nvmtl_up_rec;

typedef struct { nvmtl_up_rec *head, *tail; uint64_t n; } nvmtl_up_fifo;

// At most this many records are released by one sweep, so one upload never pays for a long backlog at once. Each
// upload stores at most one record, so a sweep per upload that can release several catches up.
#define NVMTL_UP_SWEEP_MAX 16u

// NVMTL_ASYNCUP_REAP: unset or anything else than a value starting with 0 is on, except in WindowServer, whose
// uploads are synchronous by default (nvmtl_switch_defaults.h) and which keeps what it did in 3.0.5 even when it is
// opted in to the asynchronous uploads.
static inline int nvmtl_up_reap_default(const char *env, const char *progname)
{
    if (env && env[0] == '0') return 0;
    return !(progname && !strcmp(progname, "WindowServer"));
}

static inline void nvmtl_up_rec_init(nvmtl_up_rec *r, void *body)
{
    r->prev = r->next = 0; r->body = body; r->st = NVMTL_UP_LIVE; r->linked = 0; r->orphan = 0;
}

// Under the lock. A record is on the list at most once.
static inline void nvmtl_up_fifo_push(nvmtl_up_fifo *f, nvmtl_up_rec *r)
{
    if (r->linked) return;
    r->next = 0; r->prev = f->tail;
    if (f->tail) f->tail->next = r; else f->head = r;
    f->tail = r; r->linked = 1; f->n++;
}
static inline void nvmtl_up_fifo_unlink(nvmtl_up_fifo *f, nvmtl_up_rec *r)
{
    if (!r->linked) return;
    if (r->prev) r->prev->next = r->next; else f->head = r->next;
    if (r->next) r->next->prev = r->prev; else f->tail = r->prev;
    r->prev = r->next = 0; r->linked = 0; f->n--;
}

typedef enum {
    NVMTL_UP_CLAIM_FINISH = 0,   // LIVE: taken off the list; the caller finishes the body as before (waits if needed)
    NVMTL_UP_CLAIM_LEFT   = 1,   // BUSY: its copy has finished; the sweep that is releasing it frees the header
    NVMTL_UP_CLAIM_FREE   = 2,   // RELEASED: nothing is in flight; the caller frees the header only
} nvmtl_up_claim_kind;

// Under the lock, for a handle just cleared from its image (a take, or the handle a store replaced).
static inline nvmtl_up_claim_kind nvmtl_up_claim(nvmtl_up_fifo *f, nvmtl_up_rec *r)
{
    if (r->st == NVMTL_UP_BUSY) { r->orphan = 1; return NVMTL_UP_CLAIM_LEFT; }
    if (r->st == NVMTL_UP_RELEASED) return NVMTL_UP_CLAIM_FREE;
    nvmtl_up_fifo_unlink(f, r);
    return NVMTL_UP_CLAIM_FINISH;
}

// What a zero-timeout poll of a record's fence found.
enum { NVMTL_UP_PROBE_PENDING = 0, NVMTL_UP_PROBE_RAN = 1, NVMTL_UP_PROBE_DROPPED = 2 };

// Under the lock: claim up to `max` records whose copy has run, oldest first, into out[]; returns how many. Stops at the
// first record whose fence has not signalled (or that the poll could not answer). A dropped one is taken off the list
// and stays LIVE for the take that can make its copy again; it is not counted against max, but the polls are bounded
// to twice max.
static inline unsigned nvmtl_up_sweep_collect(nvmtl_up_fifo *f, unsigned max, int (*probe)(void *ctx, void *body),
                                              void *ctx, nvmtl_up_rec **out)
{
    unsigned k = 0, polls = 0;
    while (f->head && k < max && polls < 2 * max) {
        nvmtl_up_rec *r = f->head;
        polls++;
        const int p = probe(ctx, r->body);
        if (p == NVMTL_UP_PROBE_DROPPED) { nvmtl_up_fifo_unlink(f, r); continue; }
        if (p != NVMTL_UP_PROBE_RAN) break;
        nvmtl_up_fifo_unlink(f, r);
        r->st = NVMTL_UP_BUSY;
        out[k++] = r;
    }
    return k;
}

// Under the lock, after the sweep released a claimed record's body (outside the lock): 1 when a take cleared the handle
// meanwhile and the caller must free the header now; otherwise the header stays on its image as RELEASED.
static inline int nvmtl_up_release_done(nvmtl_up_rec *r)
{
    r->body = 0;
    if (r->orphan) return 1;
    r->st = NVMTL_UP_RELEASED;
    return 0;
}

#endif
