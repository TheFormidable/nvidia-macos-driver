/* test_fence_plugin.c - the plugin's side of the kernel surface fence (plugin/nvmtl_fence.h), on the host: the numbers
   it repeats from the kext, one command buffer's record of holds, when the fence is usable and when to ask again, which
   of the older pending marks still go to the kernel, and which answers of a kernel wait end a reader's wait.
   Build and run anywhere:
     cc -std=c11 -Wall -Wextra -Werror -I kexts/NVRM -I plugin tools/test_fence_plugin.c -o /tmp/test_fence_plugin && /tmp/test_fence_plugin */
#include <stdio.h>
#include <string.h>
#include "nvrm_fence_abi.h"
#include "nvmtl_fence.h"
static int fails;
#define T(name, cond) do { if (!(cond)) { printf("FAIL %s\n", name); fails++; } else printf("ok   %s\n", name); } while (0)

/* The plugin's copy of a number and the kext's are one number: a mismatch does not compile. */
_Static_assert(NVMTL_FENCE_OP_QUERY == NVRM_FENCE_OP_QUERY && NVMTL_FENCE_OP_OPEN == NVRM_FENCE_OP_OPEN &&
               NVMTL_FENCE_OP_ARM == NVRM_FENCE_OP_ARM && NVMTL_FENCE_OP_CLOSE == NVRM_FENCE_OP_CLOSE &&
               NVMTL_FENCE_OP_WAIT == NVRM_FENCE_OP_WAIT, "fence ops");
_Static_assert(NVMTL_FENCE_F_COMPOSITOR == NVRM_FENCE_F_COMPOSITOR && NVMTL_FENCE_F_AUTO == NVRM_FENCE_F_AUTO, "fence flags");
_Static_assert(NVMTL_FENCE_ST_OK == NVRM_FENCE_ST_OK && NVMTL_FENCE_ST_OFF == NVRM_FENCE_ST_OFF, "fence statuses");
_Static_assert(NVMTL_FENCE_R_NONE == NVRM_FENCE_R_NONE && NVMTL_FENCE_R_READY == NVRM_FENCE_R_READY &&
               NVMTL_FENCE_R_DEAD == NVRM_FENCE_R_DEAD && NVMTL_FENCE_R_TIMEOUT == NVRM_FENCE_R_TIMEOUT &&
               NVMTL_FENCE_R_BUDGET == NVRM_FENCE_R_BUDGET && NVMTL_FENCE_R_ABORTED == NVRM_FENCE_R_ABORTED &&
               NVMTL_FENCE_R_PENDING == NVRM_FENCE_R_PENDING, "fence results");
_Static_assert(NVMTL_FENCE_WAIT_MS <= NVRM_FENCE_WAIT_MAX_MS, "the kernel would clamp the reader's budget");
_Static_assert(sizeof(((nvmtl_fence_hold *)0)->planes[0]) == sizeof(((struct NVRMFenceHold *)0)->planes[0]), "plane mask width");

/* ---- a kernel that keeps the older marks, to prove no mark is ever left set ---- */
typedef int (*route_fn)(nvmtl_fence_legacy *, const nvmtl_fence_thread *, uint32_t, int);
/* for the control: a routing that forgets that the list overflowed */
static int route_forgets_overflow(nvmtl_fence_legacy *L, const nvmtl_fence_thread *t, uint32_t sid, int pending)
{
    const uint32_t was = L->overflow;
    L->overflow = 0;
    const int send = nvmtl_fence_mark_route(L, t, sid, pending);
    L->overflow = was;
    return send;
}
struct world { unsigned left, sent_marks, sent_clears, skipped; };
/* `surfaces` surfaces are written one after the other and then finished. Every third one is held by the kernel fence
   (in the thread's record at the time of its mark); the kernel refuses the mark of every `refuse`th (0 = none). */
static struct world run_world(route_fn route, unsigned surfaces, unsigned refuse)
{
    static unsigned char kmark[4096];
    struct world w = { 0, 0, 0, 0 };
    nvmtl_fence_legacy L;
    memset(&L, 0, sizeof L); memset(kmark, 0, sizeof kmark);
    for (uint32_t s = 1; s <= surfaces; s++) {
        nvmtl_fence_thread t = { 0, { 0 } };
        if (s % 3 == 0) { t.n = 1; t.sid[0] = s; }
        if (!route(&L, &t, s, 1)) { w.skipped++; continue; }
        const uint32_t answer = (refuse && s % refuse == 0) ? 0x80000002u : 0u;
        if (!answer) kmark[s] = 1;
        w.sent_marks++;
        nvmtl_fence_mark_sent(&L, s, answer);
    }
    for (uint32_t s = 1; s <= surfaces; s++) {
        nvmtl_fence_thread none = { 0, { 0 } };
        if (route(&L, &none, s, 0)) { kmark[s] = 0; w.sent_clears++; }
    }
    for (uint32_t s = 1; s <= surfaces; s++) w.left += kmark[s];
    return w;
}

int main(void)
{
    uint32_t ids[NVMTL_FENCE_HOLD_MAX];

    /* the numbers */
    T("the plugin's operation numbers are the kext's", NVMTL_FENCE_OP_QUERY == NVRM_FENCE_OP_QUERY && NVMTL_FENCE_OP_OPEN == NVRM_FENCE_OP_OPEN
      && NVMTL_FENCE_OP_ARM == NVRM_FENCE_OP_ARM && NVMTL_FENCE_OP_CLOSE == NVRM_FENCE_OP_CLOSE && NVMTL_FENCE_OP_WAIT == NVRM_FENCE_OP_WAIT);
    T("the plugin's flags are the kext's", NVMTL_FENCE_F_COMPOSITOR == NVRM_FENCE_F_COMPOSITOR && NVMTL_FENCE_F_AUTO == NVRM_FENCE_F_AUTO);
    T("the plugin's statuses are the kext's", NVMTL_FENCE_ST_OK == NVRM_FENCE_ST_OK && NVMTL_FENCE_ST_OFF == NVRM_FENCE_ST_OFF);
    T("the plugin's results are the kext's", NVMTL_FENCE_R_NONE == NVRM_FENCE_R_NONE && NVMTL_FENCE_R_READY == NVRM_FENCE_R_READY
      && NVMTL_FENCE_R_DEAD == NVRM_FENCE_R_DEAD && NVMTL_FENCE_R_TIMEOUT == NVRM_FENCE_R_TIMEOUT && NVMTL_FENCE_R_BUDGET == NVRM_FENCE_R_BUDGET
      && NVMTL_FENCE_R_ABORTED == NVRM_FENCE_R_ABORTED && NVMTL_FENCE_R_PENDING == NVRM_FENCE_R_PENDING);
    T("a reader's budget is one the kernel does not clamp", NVMTL_FENCE_WAIT_MS == 2000u && NVMTL_FENCE_WAIT_MS <= NVRM_FENCE_WAIT_MAX_MS);
    T("the tag word and the sizes", NVMTL_BUSY_KFENCE == 0x4B464E43u && NVMTL_FENCE_HOLD_MAX == 16 && NVMTL_FENCE_LEGACY_MAX == 64
      && NVMTL_FENCE_PROBE_NS == 1000000000ull);

    /* one command buffer's holds */
    nvmtl_fence_hold h;
    memset(&h, 0xa5, sizeof h);
    nvmtl_fence_hold_reset(&h);
    T("reset empties the record", h.cookie == 0 && h.n == 0 && h.overflow == 0 && h.sid[0] == 0 && h.sid[15] == 0 && h.planes[15] == 0);
    nvmtl_fence_hold_note(&h, 100, 0, 0);
    nvmtl_fence_hold_note(&h, 200, 1, 0);
    nvmtl_fence_hold_note(&h, 100, 1, 2);
    nvmtl_fence_hold_note(&h, 100, 0, 1);   /* an earlier part seen later does not lower the last writer */
    nvmtl_fence_hold_note(&h, 300, 2, 1);
    T("a surface seen again is one hold", h.n == 3 && h.sid[0] == 100 && h.sid[1] == 200 && h.sid[2] == 300 && !h.overflow);
    T("its planes are merged", h.planes[0] == 0x03 && h.planes[1] == 0x02 && h.planes[2] == 0x04);
    T("the highest part is kept", h.last[0] == 2 && h.last[1] == 0 && h.last[2] == 1);
    T("the plane bit is the one a reader asks for", h.planes[2] == (uint8_t)(1u << (2 & 7)));
    nvmtl_fence_hold_note(&h, 0, 0, 5);
    T("surface 0 is not a hold", h.n == 3);
    memset(ids, 0xff, sizeof ids);
    T("part 0 arms the surface it wrote last", nvmtl_fence_hold_last(&h, 0, ids) == 1 && ids[0] == 200);
    T("part 1 arms the surface it wrote last", nvmtl_fence_hold_last(&h, 1, ids) == 1 && ids[0] == 300);
    T("part 2 arms the surface it wrote last", nvmtl_fence_hold_last(&h, 2, ids) == 1 && ids[0] == 100);
    T("a part that wrote no surface last arms nothing", nvmtl_fence_hold_last(&h, 3, ids) == 0);
    {   /* every surface is armed by exactly one part */
        unsigned total = 0;
        for (uint32_t p = 0; p < 8; p++) total += nvmtl_fence_hold_last(&h, p, ids);
        T("every surface is armed by exactly one part", total == h.n);
    }
    T("control: a surface written again later is not armed by its first part", nvmtl_fence_hold_last(&h, 0, ids) == 1 && ids[0] != 100);
    nvmtl_fence_hold_reset(&h);
    for (uint32_t s = 1; s <= 16; s++) nvmtl_fence_hold_note(&h, 1000 + s, s, 7);
    T("sixteen surfaces fit", h.n == 16 && !h.overflow && h.sid[15] == 1016);
    T("a plane above 7 lands on a bit of the mask", h.planes[7] == 0x01 && h.planes[8] == 0x02);
    nvmtl_fence_hold_note(&h, 1001, 2, 9);
    T("a known surface still merges when the record is full", h.n == 16 && !h.overflow && h.planes[0] == 0x06 && h.last[0] == 9);
    nvmtl_fence_hold_note(&h, 2000, 0, 7);
    T("the 17th surface sets overflow and is not recorded", h.n == 16 && h.overflow == 1);
    T("fifteen surfaces of part 7 and the one of part 9 come back", nvmtl_fence_hold_last(&h, 7, ids) == 15 && nvmtl_fence_hold_last(&h, 9, ids) == 1 && ids[0] == 1001);
    nvmtl_fence_hold_reset(&h);
    T("reset clears overflow", h.n == 0 && h.overflow == 0);

    /* the thread's record */
    nvmtl_fence_thread th = { 2, { 100, 300 } };
    T("the thread's record names what the last open took", nvmtl_fence_thread_has(&th, 100) && nvmtl_fence_thread_has(&th, 300) && !nvmtl_fence_thread_has(&th, 200));
    T("surface 0 and an empty record hold nothing", !nvmtl_fence_thread_has(&th, 0) && !nvmtl_fence_thread_has(NULL, 100)
      && !nvmtl_fence_thread_has(&(nvmtl_fence_thread){ 0, { 100 } }, 100));

    /* usable, and when to ask */
    nvmtl_fence_state st;
    memset(&st, 0, sizeof st);
    T("never asked: not usable, and a probe is due at once", !nvmtl_fence_usable(&st, 0) && !nvmtl_fence_usable(&st, 1)
      && nvmtl_fence_probe_due(&st, 0, 0) && nvmtl_fence_probe_due(&st, 1, 12345));
    T("an old kext (the call fails): off, no change to log", nvmtl_fence_probe_set(&st, 1000, 0xe00002c2u, 1, 1) == 0 && !st.active && !st.accel);
    T("while off, one probe a second", !nvmtl_fence_probe_due(&st, 0, 1000) && !nvmtl_fence_probe_due(&st, 0, 1000 + NVMTL_FENCE_PROBE_NS - 1)
      && nvmtl_fence_probe_due(&st, 0, 1000 + NVMTL_FENCE_PROBE_NS));
    T("the kernel answers active: on, logged once", nvmtl_fence_probe_set(&st, 5000, NVMTL_FENCE_ST_OK, 1, 0) == 1 && st.active && !st.accel
      && nvmtl_fence_probe_set(&st, 5000, NVMTL_FENCE_ST_OK, 1, 0) == 0);
    T("application holds are usable without the accelerator", nvmtl_fence_usable(&st, 0) && !nvmtl_fence_probe_due(&st, 0, ~0ull));
    T("compositor holds need the accelerator", !nvmtl_fence_usable(&st, 1));
    T("and it is asked again once a second", !nvmtl_fence_probe_due(&st, 1, 5000 + NVMTL_FENCE_PROBE_NS - 1) && nvmtl_fence_probe_due(&st, 1, 5000 + NVMTL_FENCE_PROBE_NS));
    T("the accelerator reads the table: compositor holds are usable", nvmtl_fence_probe_set(&st, 9000, NVMTL_FENCE_ST_OK, 1, 1) == 1
      && nvmtl_fence_usable(&st, 1) && !nvmtl_fence_probe_due(&st, 1, ~0ull));
    T("a call that answers OFF switches the fence off", nvmtl_fence_probe_set(&st, 20000, NVMTL_FENCE_ST_OFF, 0, 0) == 1
      && !st.active && !st.accel && !nvmtl_fence_usable(&st, 0) && !nvmtl_fence_usable(&st, 1));
    T("and schedules a probe one second later", !nvmtl_fence_probe_due(&st, 0, 20000) && nvmtl_fence_probe_due(&st, 0, 20000 + NVMTL_FENCE_PROBE_NS));
    T("a kernel that answers not active stays off", nvmtl_fence_probe_set(&st, 30000, NVMTL_FENCE_ST_OK, 0, 1) == 0 && !st.active && !st.accel
      && !nvmtl_fence_usable(&st, 0));
    T("a status that is not OK is off whatever the words say", nvmtl_fence_probe_set(&st, 40000, 0x80000001u, 1, 1) == 0 && !st.active);
    T("control: usable is not simply the last answer's active word", (nvmtl_fence_probe_set(&st, 50000, NVMTL_FENCE_ST_OK, 1, 0), 1)
      && nvmtl_fence_usable(&st, 0) != nvmtl_fence_usable(&st, 1));

    /* the older pending mark */
    nvmtl_fence_legacy L;
    memset(&L, 0, sizeof L);
    T("surface 0 is always sent, mark and clear", nvmtl_fence_mark_route(&L, &th, 0, 1) == 1 && nvmtl_fence_mark_route(&L, &th, 0, 0) == 1 && L.n == 0);
    nvmtl_fence_mark_sent(&L, 0, 0);
    T("surface 0 is never remembered", L.n == 0 && !L.overflow);
    T("a surface the kernel fence holds is not marked", nvmtl_fence_mark_route(&L, &th, 100, 1) == 0 && L.n == 0);
    T("and its clear is not sent", nvmtl_fence_mark_route(&L, &th, 100, 0) == 0);
    T("any other surface is marked", nvmtl_fence_mark_route(&L, &th, 200, 1) == 1 && nvmtl_fence_mark_route(&L, NULL, 100, 1) == 1);
    nvmtl_fence_mark_sent(&L, 200, 0);
    T("a mark the kernel took is remembered", L.n == 1 && L.sid[0] == 200);
    nvmtl_fence_mark_sent(&L, 200, 0);
    T("once", L.n == 1);
    T("its clear is sent, once", nvmtl_fence_mark_route(&L, &th, 200, 0) == 1 && L.n == 0 && nvmtl_fence_mark_route(&L, &th, 200, 0) == 0);
    T("a clear for a surface never marked is not sent", nvmtl_fence_mark_route(&L, &th, 999, 0) == 0);
    nvmtl_fence_mark_sent(&L, 400, 0x80000002u);
    T("a refused mark is not remembered", L.n == 0 && nvmtl_fence_mark_route(&L, &th, 400, 0) == 0);
    nvmtl_fence_mark_sent(&L, 500, 0); nvmtl_fence_mark_sent(&L, 501, 0); nvmtl_fence_mark_sent(&L, 502, 0);
    T("forgetting one keeps the others", nvmtl_fence_mark_route(&L, NULL, 500, 0) == 1 && L.n == 2
      && nvmtl_fence_mark_route(&L, NULL, 502, 0) == 1 && nvmtl_fence_mark_route(&L, NULL, 501, 0) == 1 && L.n == 0);
    for (uint32_t s = 1; s <= 64; s++) nvmtl_fence_mark_sent(&L, 5000 + s, 0);
    T("64 surfaces are remembered", L.n == 64 && !L.overflow);
    nvmtl_fence_mark_sent(&L, 6000, 0);
    T("the 65th sets overflow", L.n == 64 && L.overflow == 1);
    T("after an overflow every clear is sent", nvmtl_fence_mark_route(&L, &th, 6000, 0) == 1 && nvmtl_fence_mark_route(&L, &th, 7777, 0) == 1
      && nvmtl_fence_mark_route(&L, &th, 5001, 0) == 1 && L.n == 63);
    T("but a pending mark of a held surface is still not sent", nvmtl_fence_mark_route(&L, &th, 300, 1) == 0);

    /* no mark is ever left set in the kernel */
    struct world w = run_world(nvmtl_fence_mark_route, 30, 0);
    T("30 surfaces: every mark the kernel took is cleared", w.left == 0 && w.sent_marks == 20 && w.sent_clears == 20 && w.skipped == 10);
    w = run_world(nvmtl_fence_mark_route, 30, 4);
    T("refused marks cost no clear", w.left == 0 && w.sent_marks == 20 && w.sent_clears == 15);
    w = run_world(nvmtl_fence_mark_route, 600, 0);
    T("600 surfaces (the list overflows): every mark is cleared", w.left == 0 && w.sent_marks == 400 && w.sent_clears == 600);
    w = run_world(route_forgets_overflow, 600, 0);
    T("control: a routing that forgets the overflow leaves marks set", w.left == 400 - 64 && w.sent_clears == 64);
    w = run_world(route_forgets_overflow, 30, 0);
    T("control: without an overflow the two routings agree", w.left == 0 && w.sent_clears == 20);

    /* a kernel wait's answer */
    T("NONE, READY, DEAD, TIMEOUT and BUDGET end the reader's wait", nvmtl_fence_wait_handled(NVMTL_FENCE_ST_OK, NVMTL_FENCE_R_NONE)
      && nvmtl_fence_wait_handled(NVMTL_FENCE_ST_OK, NVMTL_FENCE_R_READY) && nvmtl_fence_wait_handled(NVMTL_FENCE_ST_OK, NVMTL_FENCE_R_DEAD)
      && nvmtl_fence_wait_handled(NVMTL_FENCE_ST_OK, NVMTL_FENCE_R_TIMEOUT) && nvmtl_fence_wait_handled(NVMTL_FENCE_ST_OK, NVMTL_FENCE_R_BUDGET));
    T("ABORTED and PENDING fall through to the older wait", !nvmtl_fence_wait_handled(NVMTL_FENCE_ST_OK, NVMTL_FENCE_R_ABORTED)
      && !nvmtl_fence_wait_handled(NVMTL_FENCE_ST_OK, NVMTL_FENCE_R_PENDING));
    T("a result this plugin does not know falls through", !nvmtl_fence_wait_handled(NVMTL_FENCE_ST_OK, 7) && !nvmtl_fence_wait_handled(NVMTL_FENCE_ST_OK, ~0u));
    {
        static const uint32_t bad[] = { NVRM_FENCE_ST_BAD_VERSION, NVRM_FENCE_ST_OFF, NVRM_FENCE_ST_BAD_ARG, NVRM_FENCE_ST_FULL,
                                        NVRM_FENCE_ST_DENIED, NVRM_FENCE_ST_STALE, NVRM_FENCE_ST_MAP, NVRM_FENCE_ST_DISTRUST, 0xe00002c2u, 1u };
        int any = 0;
        for (unsigned i = 0; i < sizeof bad / sizeof bad[0]; i++)
            for (uint32_t r = 0; r <= 7; r++) any |= nvmtl_fence_wait_handled(bad[i], r);
        T("every error status falls through, whatever the result word", !any);
    }

    printf(fails ? "\n%d FAILED\n" : "\nall passed\n", fails);
    return fails ? 1 : 0;
}
