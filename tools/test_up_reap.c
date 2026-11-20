/* test_up_reap.c - the reaper of finished asynchronous texture uploads (plugin/nvmtl_up_reap.h).
   Build and run anywhere (no GPU, no Metal):  cc -std=c11 -Wall -Wextra -pthread -I plugin tools/test_up_reap.c -o /tmp/test_up_reap && /tmp/test_up_reap
   The model drives the header the way plugin/nvmtl_vk.c does: images hold a handle (the header), a store puts the
   record on the list, a take claims it, and a sweep claims what has run and releases it outside the lock. Fences are
   flags the test sets. Each arm is a sentence about a failure the rules must refuse: a copy released while it runs, a
   header or record freed twice or never, a dropped copy that is not left to its image, a take that waits on itself. */
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <stdlib.h>
#include "nvmtl_up_reap.h"
#include "nvmtl_tex_upload.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }

/* --- model --------------------------------------------------------------------------------------------------- */
typedef struct { int signalled, dropped, released_running, finished, redone; } body_t;   /* the plugin's record */
typedef struct { uint64_t up_token; } image_t;

static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static nvmtl_up_fifo fifo;
static long held, bodies_live, recs_live, bad;   /* counts, and faults the model saw */
static int reap = 1;

static int probe(void *ctx, void *b) {
    (void)ctx; const body_t *x = b;
    if (!x->signalled) return NVMTL_UP_PROBE_PENDING;
    return x->dropped ? NVMTL_UP_PROBE_DROPPED : NVMTL_UP_PROBE_RAN;
}
static void body_free(body_t *b) { free(b); __atomic_sub_fetch(&bodies_live, 1, __ATOMIC_SEQ_CST); }
static void rec_free(nvmtl_up_rec *r) { free(r); __atomic_sub_fetch(&recs_live, 1, __ATOMIC_SEQ_CST); }

/* nvmtl_asyncup_claimed */
static body_t *claimed(nvmtl_up_rec *r, nvmtl_up_claim_kind k) {
    if (!r) return NULL;
    if (k == NVMTL_UP_CLAIM_FREE) { rec_free(r); return NULL; }
    if (k == NVMTL_UP_CLAIM_LEFT) return NULL;
    return r->body;
}
/* nvmtl_asyncup_finish: waits for the copy (here: the copy "lands"), makes a dropped one again with the image */
static void finish(image_t *img, body_t *b, nvmtl_up_rec *r) {
    if (!b) return;
    rec_free(r);
    b->signalled = 1;
    if (b->dropped && img) b->redone = 1;
    b->finished = 1;
    __atomic_sub_fetch(&held, 1, __ATOMIC_SEQ_CST);
    body_free(b);
}
/* the take returns both, since the model frees the header in finish as the plugin does (au->rec) */
static body_t *take(image_t *img, nvmtl_up_rec **rec_out) {
    pthread_mutex_lock(&mu);
    nvmtl_up_rec *r = (nvmtl_up_rec *)(uintptr_t)img->up_token;
    img->up_token = 0;
    const nvmtl_up_claim_kind k = r ? nvmtl_up_claim(&fifo, r) : NVMTL_UP_CLAIM_FINISH;
    pthread_mutex_unlock(&mu);
    *rec_out = r;
    return claimed(r, k);
}
static void consume(image_t *img) {   /* a readback, destroy or re-upload: after it, no copy may still run */
    nvmtl_up_rec *r; body_t *b = take(img, &r);
    finish(img, b, r);
}
static body_t *upload(image_t *img) {   /* the async upload: finish-first, then store */
    consume(img);
    body_t *b = calloc(1, sizeof *b); nvmtl_up_rec *r = malloc(sizeof *r);
    __atomic_add_fetch(&bodies_live, 1, __ATOMIC_SEQ_CST); __atomic_add_fetch(&recs_live, 1, __ATOMIC_SEQ_CST);
    nvmtl_up_rec_init(r, b);
    __atomic_add_fetch(&held, 1, __ATOMIC_SEQ_CST);
    pthread_mutex_lock(&mu);
    nvmtl_up_state s = { img->up_token };
    nvmtl_up_rec *old = (nvmtl_up_rec *)(uintptr_t)nvmtl_up_set(&s, (uint64_t)(uintptr_t)r);
    img->up_token = s.infl;
    if (reap) nvmtl_up_fifo_push(&fifo, r);
    const nvmtl_up_claim_kind k = old ? nvmtl_up_claim(&fifo, old) : NVMTL_UP_CLAIM_FINISH;
    pthread_mutex_unlock(&mu);
    body_t *ob = claimed(old, k);
    finish(img, ob, old);
    return b;
}
/* nvmtl_asyncup_sweep; `during` runs between the release of the body and the release_done, as a grace reap would */
static unsigned sweep(void (*during)(void *), void *arg) {
    if (!reap) return 0;
    nvmtl_up_rec *got[NVMTL_UP_SWEEP_MAX];
    pthread_mutex_lock(&mu);
    const unsigned n = nvmtl_up_sweep_collect(&fifo, NVMTL_UP_SWEEP_MAX, probe, NULL, got);
    pthread_mutex_unlock(&mu);
    for (unsigned i = 0; i < n; i++) {
        body_t *b = got[i]->body;
        if (!b->signalled || b->dropped) { b->released_running = 1; __atomic_add_fetch(&bad, 1, __ATOMIC_SEQ_CST); }
        __atomic_sub_fetch(&held, 1, __ATOMIC_SEQ_CST);
        body_free(b);
        if (during) during(arg);
        pthread_mutex_lock(&mu);
        const int orphan = nvmtl_up_release_done(got[i]);
        pthread_mutex_unlock(&mu);
        if (orphan) rec_free(got[i]);
    }
    return n;
}
static void reset(void) { fifo.head = fifo.tail = NULL; fifo.n = 0; held = bodies_live = recs_live = bad = 0; reap = 1; }

static void take_during(void *arg) { consume((image_t *)arg); }

/* --- the race: a sweeper thread against takers and uploaders on the same images ---------------------------------- */
#define RIMG 64
static image_t rimg[RIMG];
static volatile int rstop;
static void *sweeper(void *a) { (void)a; while (!rstop) { sweep(NULL, NULL); sched_yield(); } return NULL; }
static void *signaller(void *a) {   /* the GPU: signals everything stored, sooner or later */
    (void)a;
    while (!rstop) {
        pthread_mutex_lock(&mu);
        for (nvmtl_up_rec *r = fifo.head; r; r = r->next) ((body_t *)r->body)->signalled = 1;
        pthread_mutex_unlock(&mu);
        sched_yield();
    }
    return NULL;
}
static void *user(void *a) {
    unsigned seed = (unsigned)(uintptr_t)a;
    for (int i = 0; i < 20000; i++) {
        seed = seed * 1103515245u + 12345u;
        image_t *im = &rimg[(seed >> 8) % RIMG];
        if ((seed >> 4) & 1) upload(im); else consume(im);
    }
    return NULL;
}

int main(void)
{
    arm("unset leaves the reaper on", nvmtl_up_reap_default(NULL, "java") == 1);
    arm("NVMTL_ASYNCUP_REAP=0 turns it off", nvmtl_up_reap_default("0", "java") == 0);
    arm("NVMTL_ASYNCUP_REAP=1 leaves it on", nvmtl_up_reap_default("1", "java") == 1);
    arm("WindowServer keeps 3.0.5 (no reaper), even with the variable set to 1",
        nvmtl_up_reap_default(NULL, "WindowServer") == 0 && nvmtl_up_reap_default("1", "WindowServer") == 0);

    /* (1) 300 textures uploaded once: their places come back once their copies have run */
    reset();
    static image_t once[300];
    for (int i = 0; i < 300; i++) { once[i].up_token = 0; upload(&once[i]); }
    arm("300 textures uploaded once hold 300 places before any copy has run", held == 300);
    arm("a sweep releases nothing while every copy still runs", sweep(NULL, NULL) == 0 && held == 300);
    for (nvmtl_up_rec *r = fifo.head; r; r = r->next) ((body_t *)r->body)->signalled = 1;
    unsigned got = 0, s, most = 0;
    while ((s = sweep(NULL, NULL))) { if (s > most) most = s; got += s; }
    arm("one sweep releases at most NVMTL_UP_SWEEP_MAX", most == NVMTL_UP_SWEEP_MAX);
    arm("after their copies ran, sweeps give back all 300 places and their records", got == 300 && held == 0 && bodies_live == 0);
    arm("so a 301st upload finds room again", nvmtl_up_room((uint64_t)held, 0, 4096));
    arm("each image keeps only its small header", recs_live == 300 && fifo.n == 0);
    for (int i = 0; i < 300; i++) consume(&once[i]);
    arm("the image's next take frees the released header, once", recs_live == 0 && bad == 0);

    /* (2) a copy still running is never released, and the sweep stops at it */
    reset();
    image_t a = {0}, b = {0}, c = {0};
    body_t *ba = upload(&a); body_t *bb = upload(&b); body_t *bc = upload(&c);
    ba->signalled = 1; bc->signalled = 1;   /* b still runs */
    arm("the sweep stops at the oldest copy still running", sweep(NULL, NULL) == 1 && held == 2 && !bb->finished);
    arm("and releases nothing that runs", bad == 0);
    consume(&a); consume(&b); consume(&c);
    arm("the takes then finish the rest; nothing is left", held == 0 && bodies_live == 0 && recs_live == 0);

    /* (3) a copy a channel rebuild dropped is left to its image's take, which makes it again */
    reset();
    image_t d = {0};
    body_t *bd = upload(&d); bd->signalled = 1; bd->dropped = 1;
    image_t e = {0}; body_t *be = upload(&e); be->signalled = 1;
    arm("a dropped copy is not released by the sweep, the one behind it is", sweep(NULL, NULL) == 1 && held == 1 && bad == 0);
    arm("the dropped one is off the list, so it is polled only once", fifo.n == 0);
    nvmtl_up_rec *rd; body_t *td = take(&d, &rd);
    arm("its take finishes it with the image, so its copy is made again", td == bd);
    finish(&d, td, rd);
    arm("and nothing is left", held == 0 && bodies_live == 0 && recs_live == 1);
    consume(&e);
    arm("the released header of the other image goes at its take", recs_live == 0);

    /* (4) a take while the sweep is releasing the same record (a grace reap inside the release destroys its image) */
    reset();
    image_t f = {0}; body_t *bf = upload(&f); bf->signalled = 1;
    arm("a take during the release does not wait on itself and frees nothing twice",
        sweep(take_during, &f) == 1 && f.up_token == 0 && recs_live == 0 && bodies_live == 0 && held == 0);

    /* (5) a texture uploaded again: the second upload takes over the first's handle */
    reset();
    image_t g = {0}; body_t *g1 = upload(&g);
    arm("the first upload is held", held == 1 && !g1->finished);
    upload(&g);
    arm("an upload again finishes the previous one first (its copy has landed) and keeps one record",
        held == 1 && fifo.n == 1 && bodies_live == 1);
    ((body_t *)((nvmtl_up_rec *)(uintptr_t)g.up_token)->body)->signalled = 1;
    sweep(NULL, NULL);
    image_t g_copy = g;   /* the image grace and the parking lots move an image by copying it */
    g.up_token = 0;
    consume(&g_copy);
    arm("a released handle moved with its image is freed by the take on the copy", recs_live == 0 && held == 0);

    /* (6) reaper off: records never go on the list, the take finishes every one as before */
    reset(); reap = 0;
    image_t h = {0}; body_t *bh = upload(&h); bh->signalled = 1;
    arm("with NVMTL_ASYNCUP_REAP=0 nothing is on the list and a sweep gives nothing back", fifo.n == 0 && sweep(NULL, NULL) == 0 && held == 1);
    nvmtl_up_rec *rh; body_t *th = take(&h, &rh);
    arm("and the take gets the record to finish, as in 3.0.5", th == bh);
    finish(&h, th, rh);
    arm("nothing is left", held == 0 && bodies_live == 0 && recs_live == 0);

    /* (7) the race */
    reset();
    for (int i = 0; i < RIMG; i++) rimg[i].up_token = 0;
    pthread_t sw, sg, u[3];
    rstop = 0;
    pthread_create(&sw, NULL, sweeper, NULL); pthread_create(&sg, NULL, signaller, NULL);
    for (int i = 0; i < 3; i++) pthread_create(&u[i], NULL, user, (void *)(uintptr_t)(i * 7919 + 1));
    for (int i = 0; i < 3; i++) pthread_join(u[i], NULL);
    rstop = 1; pthread_join(sw, NULL); pthread_join(sg, NULL);
    for (int i = 0; i < RIMG; i++) consume(&rimg[i]);
    arm("three threads uploading and taking against a sweeper: nothing running released, every record and header freed once",
        bad == 0 && held == 0 && bodies_live == 0 && recs_live == 0 && fifo.n == 0);

    printf("%d failed\n", fails);
    return fails ? 1 : 0;
}
