/* test_nvrm_workq_logic.c - the bookkeeping of NVRM's serialized work queues (kexts/NVRM/nvrm_workq_logic.h), on the
   host: items run in the order they were queued, queueing never needs memory while static nodes are left, a spare node
   is used exactly when none is, nothing is accepted outside RUN, and the two-snapshot flush covers what it must and
   still ends for an item that queues itself again.
   Build and run anywhere:
     cc -std=c11 -O2 -Wall -Wextra -Werror -I kexts/NVRM tools/test_nvrm_workq_logic.c -o /tmp/test_nvrm_workq_logic && /tmp/test_nvrm_workq_logic */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "nvrm_workq_logic.h"

static int fails;
static struct nvwq_core q;   /* large: static */
static unsigned count_free(void);
#define T(name, cond) do { if (!(cond)) { printf("FAIL %s\n", name); fails++; } else printf("ok   %s\n", name); } while (0)

static unsigned long long rng = 0x9e3779b97f4a7c15ull;
static unsigned rnd(unsigned n) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (unsigned)(rng % n); }


/* A model run: random enqueue (with and without a spare), dequeue+complete, against a plain array FIFO. Returns the
   number of disagreements. `lifo` makes the model pop from the wrong end: the control that the comparison sees order. */
static int fifo_run(unsigned steps, int lifo, unsigned *spares_used, unsigned *heap_back, unsigned *refused)
{
    static unsigned long ref[200000];
    unsigned rh = 0, rt = 0, bad = 0;
    unsigned long next = 1;
    nvwq_core_init(&q);
    q.state = NVWQ_RUN;
    *spares_used = *heap_back = *refused = 0;
    for (unsigned s = 0; s < steps; s++) {
        unsigned pending = rt - rh;
        if (rnd(100) < (pending > 300 ? 40u : 60u)) {
            int used = 0;
            struct nvwq_node *spare = NULL;
            const unsigned free_before = count_free();
            int r = nvwq_enqueue(&q, (void *)next, NULL, NULL);
            if ((r == 1) != (free_before == 0)) bad++;   /* 1 exactly when no static node is left */
            if (r == 1) {
                spare = (struct nvwq_node *)malloc(sizeof *spare);
                r = nvwq_enqueue(&q, (void *)next, spare, &used);
                if (used) (*spares_used)++; else free(spare);
                if (!used) bad++;   /* with no static node left the spare must be taken */
            }
            if (r == 0) { if (rt >= sizeof ref / sizeof ref[0]) return -1; ref[rt++] = next++; }
            else (*refused)++;
        } else if (rt > rh) {
            struct nvwq_node *n = nvwq_dequeue(&q);
            unsigned long want = lifo ? ref[--rt] : ref[rh++];
            if (!n || (unsigned long)n->item != want) bad++;
            if (n) {
                struct nvwq_node *h = nvwq_complete(&q, n);
                if (h) { (*heap_back)++; free(h); }
            }
        } else if (nvwq_dequeue(&q) != NULL) {
            bad++;
        }
        if (q.depth != rt - rh) bad++;
        if (q.enq_seq - q.done_seq != q.depth) bad++;
    }
    /* drain */
    while (rt > rh) {
        struct nvwq_node *n = nvwq_dequeue(&q);
        unsigned long want = lifo ? ref[--rt] : ref[rh++];
        if (!n || (unsigned long)n->item != want) bad++;
        if (n) { struct nvwq_node *h = nvwq_complete(&q, n); if (h) { (*heap_back)++; free(h); } }
    }
    if (q.depth != 0 || q.head || q.tail) bad++;
    return (int)bad;
}

static unsigned count_free(void) { unsigned n = 0; for (struct nvwq_node *f = q.freelist; f; f = f->next) n++; return n; }

/* The worker in miniature: runs one item; an item whose value is odd queues itself again (value + 2) the first
   `requeue` times it runs. Returns the number of items run before the flush predicate held, or -1 if it never did. */
static int flush_run(int passes, int requeue_forever, int *covered_late)
{
    nvwq_core_init(&q);
    q.state = NVWQ_RUN;
    (void)nvwq_enqueue(&q, (void *)(unsigned long)2, NULL, NULL);
    (void)nvwq_enqueue(&q, (void *)(unsigned long)1, NULL, NULL);   /* queues 3 while it runs */
    (void)nvwq_enqueue(&q, (void *)(unsigned long)4, NULL, NULL);
    int ran = 0, saw3 = 0;
    for (int pass = 0; pass < passes; pass++) {
        unsigned long long t = nvwq_flush_target(&q);
        while (!nvwq_flushed(&q, t)) {
            struct nvwq_node *n = nvwq_dequeue(&q);
            if (!n) return -1;
            unsigned long v = (unsigned long)n->item;
            if (v == 3) saw3 = 1;
            if ((v & 1) && (requeue_forever || v == 1)) (void)nvwq_enqueue(&q, (void *)(v + 2), NULL, NULL);
            (void)nvwq_complete(&q, n);
            if (++ran > 100000) return -1;
        }
    }
    *covered_late = saw3;
    return ran;
}

int main(void)
{
    unsigned spares, heap_back, refused;

    nvwq_core_init(&q);
    T("a new queue is OFF, empty, with every static node free", q.state == NVWQ_OFF && !q.head && !q.tail && q.depth == 0
      && count_free() == NVWQ_STATIC_NODES && q.enq_seq == 0 && q.done_seq == 0);

    /* nothing is accepted outside RUN */
    {
        int ok = 1;
        unsigned char states[] = { NVWQ_OFF, NVWQ_STOPPING, NVWQ_LEGACY };
        for (unsigned i = 0; i < sizeof states; i++) {
            struct nvwq_node spare;
            int used = 7;
            nvwq_core_init(&q);
            q.state = states[i];
            if (nvwq_enqueue(&q, (void *)1, NULL, NULL) != 2) ok = 0;
            if (nvwq_enqueue(&q, (void *)1, &spare, &used) != 2 || used != 0) ok = 0;
            if (q.enq_seq || q.depth || q.head) ok = 0;
        }
        T("enqueue is refused (2) in OFF, STOPPING and LEGACY, also with a spare, and changes nothing", ok);
    }
    /* static nodes, then spares */
    {
        int ok = 1;
        nvwq_core_init(&q);
        q.state = NVWQ_RUN;
        for (unsigned i = 0; i < NVWQ_STATIC_NODES; i++) if (nvwq_enqueue(&q, (void *)(unsigned long)(i + 1), NULL, NULL) != 0) ok = 0;
        T("256 items queue with no memory at all", ok && count_free() == 0 && q.depth == NVWQ_STATIC_NODES);
        T("the 257th without a spare is answered 1 and not queued", nvwq_enqueue(&q, (void *)999, NULL, NULL) == 1 && q.depth == NVWQ_STATIC_NODES
          && q.enq_seq == NVWQ_STATIC_NODES);
        struct nvwq_node spare;
        int used = 0;
        T("the 257th with a spare takes it", nvwq_enqueue(&q, (void *)999, &spare, &used) == 0 && used == 1 && spare.heap == 1
          && q.tail == &spare && q.depth == NVWQ_STATIC_NODES + 1);
        struct nvwq_node *n = nvwq_dequeue(&q);
        T("the oldest comes out first", n && n->item == (void *)1 && n->heap == 0);
        T("a static node goes back on the free list", nvwq_complete(&q, n) == NULL && count_free() == 1);
        struct nvwq_node spare2;
        used = 1;
        T("with a static node free again, a spare offered is not used", nvwq_enqueue(&q, (void *)1000, &spare2, &used) == 0 && used == 0
          && count_free() == 0);
        for (unsigned i = 1; i < NVWQ_STATIC_NODES; i++) { n = nvwq_dequeue(&q); (void)nvwq_complete(&q, n); }
        n = nvwq_dequeue(&q);
        T("the spare item comes out in its turn", n == &spare && n->item == (void *)999);
        T("a spare node is handed back for the caller to free", nvwq_complete(&q, n) == &spare);
        n = nvwq_dequeue(&q);
        T("then the item queued after it", n && n->item == (void *)1000);
        (void)nvwq_complete(&q, n);
        T("empty again: depth 0, no head, nothing to dequeue, every static node free", q.depth == 0 && !q.head && !q.tail
          && nvwq_dequeue(&q) == NULL && count_free() == NVWQ_STATIC_NODES);
        T("the deepest the queue was is kept", q.max_depth == NVWQ_STATIC_NODES + 1);
        T("accepted and completed counts agree", q.enq_seq == NVWQ_STATIC_NODES + 2 && q.done_seq == q.enq_seq);
    }
    /* randomized FIFO against an array */
    {
        int bad = fifo_run(200000, 0, &spares, &heap_back, &refused);
        T("200000 random steps: every item comes out in the order it went in, depth and counts agree", bad == 0);
        T("the random run went past the static nodes and every spare came back", spares > 0 && spares == heap_back && refused == 0);
        int bad_lifo = fifo_run(200000, 1, &spares, &heap_back, &refused);
        T("control: a model that pops the newest item disagrees (the comparison sees order)", bad_lifo != 0);
    }
    /* flush */
    {
        int late = 0;
        int ran1 = flush_run(1, 0, &late);
        T("one snapshot runs the three items queued before it and misses the one an item queued while it ran",
          ran1 == 3 && late == 0 && q.head != NULL && (unsigned long)q.head->item == 3);
        int ran2 = flush_run(2, 0, &late);
        T("two snapshots also cover the item queued from inside the queue", ran2 == 4 && late == 1 && q.head == NULL);
        int ran3 = flush_run(2, 1, &late);
        T("an item that queues itself for ever does not keep a two-snapshot flush waiting", ran3 > 0 && ran3 < 10 && q.head != NULL);
        nvwq_core_init(&q);
        q.state = NVWQ_RUN;
        T("a flush of an empty queue is done at once", nvwq_flushed(&q, nvwq_flush_target(&q)));
        (void)nvwq_enqueue(&q, (void *)1, NULL, NULL);
        unsigned long long t = nvwq_flush_target(&q);
        struct nvwq_node *n = nvwq_dequeue(&q);
        T("an item taken but still running is not flushed", !nvwq_flushed(&q, t));
        (void)nvwq_complete(&q, n);
        T("it is once it has completed", nvwq_flushed(&q, t));
        /* STOPPING: refuses new items, the worker still gets what is pending */
        (void)nvwq_enqueue(&q, (void *)5, NULL, NULL);
        q.state = NVWQ_STOPPING;
        T("STOPPING refuses a new item and still hands out the pending one", nvwq_enqueue(&q, (void *)6, NULL, NULL) == 2
          && (n = nvwq_dequeue(&q)) != NULL && n->item == (void *)5 && nvwq_dequeue(&q) == NULL);
    }
    printf(fails ? "\n%d failed\n" : "\nall passed\n", fails);
    return fails ? 1 : 0;
}
