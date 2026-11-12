/* test_evwait_policy.c - the event wait rules of kexts/NVRM/nvrm_evwait.h, on the host: the bits the kernel returns, the
   three modes, when a wait sleeps instead of polling, the per-thread memory and the missed-wake accounting.
   Build and run anywhere:
     cc -std=c11 -Wall -Wextra -Werror -I kexts/NVRM tools/test_evwait_policy.c -o /tmp/test_evwait_policy && /tmp/test_evwait_policy */
#include <stdio.h>
#include <string.h>
#include "nvrm_evwait.h"
static int fails;
#define T(name, cond) do { if (!(cond)) { printf("FAIL %s\n", name); fails++; } else printf("ok   %s\n", name); } while (0)

/* the 2.9 ladder, as nvkmd_nvrm_wait_block chose in 2.9: 1 = event sleep */
static int v29(int on, uint64_t waited_ns) { return on && waited_ns >= 20000; }
/* for the control: a ladder that is not 2.9's */
static int not29(int on, uint64_t waited_ns) { return on && waited_ns >= 30000; }
/* A thread in the adaptive mode whose waits all take wait_ns: each round polls one step of step_ns or, when the rules say
   so, sleeps until the work is done or for 2 ms. Returns how many of waits 200..n-1 slept right after the spin (before
   1 ms) and in *first the first wait that did, -1 none. length_sample: the control, whose mean samples the whole wait
   instead of the time into it at which its last round began. */
static int steady(uint64_t wait_ns, int n, int length_sample, int *first)
{
    struct nvrm_evw_thread t; int early = 0; const uint64_t step_ns = 5000;
    memset(&t, 0, sizeof t); t.seq_fd = -1; *first = -1;
    for (int w = 0; w < n; w++) {
        uint64_t x = 0; int asleep = 0;
        while (x < wait_ns) {
            (void)nvrm_evw_begin(&t, x);
            if (!nvrm_evw_should_sleep(NVRM_EVW_ADAPT, x, t.avg_ns, UINT64_MAX, 0)) { x += step_ns; continue; }
            if (!asleep && x < NVRM_EVW_YIELD_NS) { if (*first < 0) *first = w; if (w >= 200) early++; }
            asleep = 1;
            x += wait_ns - x < NVRM_EVW_MAX_SLEEP_US * 1000ull ? wait_ns - x : NVRM_EVW_MAX_SLEEP_US * 1000ull;
        }
        if (length_sample) t.last_waited_ns = wait_ns;
    }
    return early;
}

int main(void)
{
    /* ABI */
    T("a 2.9 user space reads the same count", (uint32_t)nvrm_evw_pack(0xfffffff0u, NVRM_EVW_R_WOKEN) == 0xfffffff0u);
    T("count and reason round-trip", nvrm_evw_seq(nvrm_evw_pack(7, NVRM_EVW_R_TIMEOUT)) == 7 && nvrm_evw_reason_of(nvrm_evw_pack(7, NVRM_EVW_R_TIMEOUT)) == NVRM_EVW_R_TIMEOUT);
    T("a 2.9 kext reports no reason", nvrm_evw_reason_of((uint64_t)123u) == NVRM_EVW_R_LEGACY);
    T("the reason is bits 32..39 and nothing else", nvrm_evw_pack(0, 0xffffu) == 0xffull << 32 && nvrm_evw_reason_of(~0ull) == 0xffu
      && nvrm_evw_reason_of(0xffffff00ffffffffull) == 0);
    T("the numbers user space and the kernel agree on", NVRM_EVW_SEL_WAIT == 6u && NVRM_EVW_SEL_CAPS == 7u && NVRM_EVW_SEL_NOTE == 9u
      && NVRM_EVW_CAP_WAIT == 1u && NVRM_EVW_CAP_REASON == 2u && NVRM_EVW_NOTE_MISSED == 1 && NVRM_EVW_NOTE_FELL_BACK == 2
      && NVRM_EVW_R_LEGACY == 0 && NVRM_EVW_R_COUNT == 1 && NVRM_EVW_R_MOVED == 2 && NVRM_EVW_R_WOKEN == 3 && NVRM_EVW_R_TIMEOUT == 4
      && NVRM_EVW_R_CAPPED == 5 && NVRM_EVW_R_ABORTED == 6 && NVRM_EVW_R_SPURIOUS == 7);
    /* kernel classification */
    T("timeout 0 reads the count", nvrm_evw_classify(0, 0, 5, 5) == NVRM_EVW_R_COUNT);
    T("a moved count does not sleep", nvrm_evw_classify(0, 0, 5, 6) == NVRM_EVW_R_MOVED);
    T("an event ends the sleep", nvrm_evw_classify(1, NVRM_EVW_WR_AWAKENED, 5, 6) == NVRM_EVW_R_WOKEN);
    T("an event at the deadline still counts as a wake", nvrm_evw_classify(1, NVRM_EVW_WR_TIMED_OUT, 5, 6) == NVRM_EVW_R_WOKEN);
    T("a quiet timeout", nvrm_evw_classify(1, NVRM_EVW_WR_TIMED_OUT, 5, 5) == NVRM_EVW_R_TIMEOUT);
    T("an interrupted sleep", nvrm_evw_classify(1, NVRM_EVW_WR_INTERRUPTED, 5, 6) == NVRM_EVW_R_ABORTED);
    T("awake with nothing new", nvrm_evw_classify(1, NVRM_EVW_WR_AWAKENED, 5, 5) == NVRM_EVW_R_SPURIOUS);
    /* modes */
    T("default: WindowServer keeps 2.9's eager sleep", nvrm_evw_mode_parse(NULL, 1) == NVRM_EVW_EAGER && nvrm_evw_mode_parse("", 1) == NVRM_EVW_EAGER);
    T("default: every other process is adaptive", nvrm_evw_mode_parse(NULL, 0) == NVRM_EVW_ADAPT);
    T("NVRM_EVENT_WAIT=0 is the opt-out, in WindowServer too", nvrm_evw_mode_parse("0", 0) == NVRM_EVW_POLL && nvrm_evw_mode_parse("0", 1) == NVRM_EVW_POLL);
    T("NVRM_EVENT_WAIT=1 means what it meant in 2.9", nvrm_evw_mode_parse("1", 0) == NVRM_EVW_EAGER && nvrm_evw_mode_parse("yes", 0) == NVRM_EVW_EAGER);
    T("NVRM_EVENT_WAIT=2 asks for adaptive", nvrm_evw_mode_parse("2", 1) == NVRM_EVW_ADAPT);
    /* POLL and EAGER are 2.9, at every wait length and with any deadline */
    int same = 1;
    for (uint64_t w = 0; w < 30000000ull; w += 777) {
        if (nvrm_evw_should_sleep(NVRM_EVW_POLL, w, 0, UINT64_MAX, 0) != v29(0, w)) same = 0;
        if (nvrm_evw_should_sleep(NVRM_EVW_EAGER, w, 5000000, 1, 0) != v29(1, w)) same = 0;
    }
    T("POLL and EAGER choose exactly as 2.9 did", same);
    int differs = 0;
    for (uint64_t w = 0; w < 30000000ull; w += 777) if (nvrm_evw_should_sleep(NVRM_EVW_EAGER, w, 5000000, 1, 0) != not29(1, w)) differs = 1;
    T("control: the same comparison tells a ladder that sleeps from 30 us apart from 2.9's", differs);
    T("EAGER keeps the 2 ms sleep", nvrm_evw_timeout_us(NVRM_EVW_EAGER, 5) == 2000);
    /* ADAPT */
    int early = 0;
    for (uint64_t w = 0; w < NVRM_EVW_YIELD_NS; w += 333) early |= nvrm_evw_should_sleep(NVRM_EVW_ADAPT, w, 0, UINT64_MAX, 0);
    T("ADAPT: a wait under 1 ms never sleeps (2.9's spin and yield)", !early);
    T("ADAPT: at 1 ms it sleeps where 2.9 began usleep(50)", nvrm_evw_should_sleep(NVRM_EVW_ADAPT, NVRM_EVW_YIELD_NS, 0, UINT64_MAX, 0));
    T("ADAPT: a thread with long waits sleeps after the spin", nvrm_evw_should_sleep(NVRM_EVW_ADAPT, 50000, 3000000, UINT64_MAX, 0) && !nvrm_evw_should_sleep(NVRM_EVW_ADAPT, 49999, 3000000, UINT64_MAX, 0));
    T("ADAPT: never sleeps past the caller's deadline", !nvrm_evw_should_sleep(NVRM_EVW_ADAPT, 5000000, 0, 99999, 0) && nvrm_evw_timeout_us(NVRM_EVW_ADAPT, 700000) == 700 && nvrm_evw_timeout_us(NVRM_EVW_ADAPT, UINT64_MAX) == 2000);
    T("a process that fell back polls", !nvrm_evw_should_sleep(NVRM_EVW_ADAPT, 5000000, 5000000, UINT64_MAX, 1) && !nvrm_evw_should_sleep(NVRM_EVW_EAGER, 5000000, 0, UINT64_MAX, 1));
    /* the per-thread memory */
    struct nvrm_evw_thread t; memset(&t, 0, sizeof t); t.seq_fd = -1;
    T("first round is not a new wait", !nvrm_evw_begin(&t, 0));
    (void)nvrm_evw_begin(&t, 400000); (void)nvrm_evw_begin(&t, 4000000);
    T("a smaller waited_ns starts a new wait and folds the old one in", nvrm_evw_begin(&t, 10) && t.avg_ns == 1000000);
    for (int i = 0; i < 40; i++) { (void)nvrm_evw_begin(&t, 100000); (void)nvrm_evw_begin(&t, 0); }
    T("short waits bring the mean back down", t.avg_ns < NVRM_EVW_LONG_NS);
    int have = 1; (void)nvrm_evw_seen(&t, 9, &have);
    T("no count remembered at the start", !have);
    nvrm_evw_remember(&t, 9, 41);
    T("the count is reused for the same event only", nvrm_evw_seen(&t, 9, &have) == 41 && have && (nvrm_evw_seen(&t, 10, &have), !have));
    nvrm_evw_forget(&t); (void)nvrm_evw_seen(&t, 9, &have);
    T("forget drops it", !have);
    /* missed wakes */
    int missed = 0, fb = 0; memset(&t, 0, sizeof t);
    for (int i = 0; i < 15; i++) fb |= nvrm_evw_account(&t, NVRM_EVW_R_TIMEOUT, 1, 2000, &missed);
    T("15 missed wakes do not fall back", !fb && t.misses == 15 && missed);
    T("a delivered wake ends the run", !nvrm_evw_account(&t, NVRM_EVW_R_WOKEN, 1, 2000, &missed) && t.misses == 0 && !missed);
    for (int i = 0; i < 16; i++) fb = nvrm_evw_account(&t, NVRM_EVW_R_TIMEOUT, 1, 2000, &missed);
    T("16 in a row fall back", fb);
    memset(&t, 0, sizeof t); fb = 0;
    for (int i = 0; i < 64; i++) {
        fb |= nvrm_evw_account(&t, NVRM_EVW_R_TIMEOUT, 1, 2000, &missed);
        if (i % 15 == 14) fb |= nvrm_evw_account(&t, NVRM_EVW_R_WOKEN, 0, 2000, &missed);
    }
    T("control: 64 missed wakes with a delivered one after every 15 never fall back", !fb);
    /* a moved count (MOVED: the kext did not sleep, the count had already moved) is a late event: it ends the run too */
    memset(&t, 0, sizeof t); fb = 0;
    for (int i = 0; i < 5; i++) fb |= nvrm_evw_account(&t, NVRM_EVW_R_TIMEOUT, 1, 2000, &missed);
    T("a moved count ends a run of missed wakes, and is not a miss itself",
      !fb && t.misses == 5 && !nvrm_evw_account(&t, NVRM_EVW_R_MOVED, 0, 2000, &missed) && t.misses == 0 && !missed);
    T("a moved count ends the run whatever `done` says (it is only looked at after a timeout)",
      (t.misses = 7, !nvrm_evw_account(&t, NVRM_EVW_R_MOVED, 1, 2000, &missed)) && t.misses == 0 && !missed);
    memset(&t, 0, sizeof t); fb = 0;
    for (int i = 0; i < 15; i++) fb |= nvrm_evw_account(&t, NVRM_EVW_R_TIMEOUT, 1, 2000, &missed);
    fb |= nvrm_evw_account(&t, NVRM_EVW_R_MOVED, 0, 2000, &missed);
    for (int i = 0; i < 15; i++) fb |= nvrm_evw_account(&t, NVRM_EVW_R_TIMEOUT, 1, 2000, &missed);
    T("15 missed wakes, a moved count, 15 more: no fallback", !fb && t.misses == 15);
    T("and the next one in a row falls back", nvrm_evw_account(&t, NVRM_EVW_R_TIMEOUT, 1, 2000, &missed) && missed && t.misses == 16);
    memset(&t, 0, sizeof t); fb = 0; int at = 0;
    for (int i = 1; i <= 16 && !at; i++) if (nvrm_evw_account(&t, NVRM_EVW_R_TIMEOUT, 1, 2000, &missed)) at = i;
    T("16 with nothing in between: fallback at exactly the sixteenth", at == (int)NVRM_EVW_MISS_LIMIT);
    /* control: only an answer that shows the count moved ends the run; every other answer leaves it as it is */
    static const unsigned keep[] = { NVRM_EVW_R_LEGACY, NVRM_EVW_R_COUNT, NVRM_EVW_R_CAPPED, NVRM_EVW_R_ABORTED, NVRM_EVW_R_SPURIOUS, 99 };
    memset(&t, 0, sizeof t); fb = 0;
    for (int i = 0; i < 15; i++) {
        fb |= nvrm_evw_account(&t, NVRM_EVW_R_TIMEOUT, 1, 2000, &missed);
        for (unsigned k = 0; k < sizeof keep / sizeof keep[0]; k++) fb |= nvrm_evw_account(&t, keep[k], 1, 2000, &missed);
        fb |= nvrm_evw_account(&t, NVRM_EVW_R_TIMEOUT, 0, 2000, &missed);    /* a timeout with the work still running */
        fb |= nvrm_evw_account(&t, NVRM_EVW_R_TIMEOUT, 1, 700, &missed);     /* a sleep cut short by the deadline */
    }
    T("control: 15 missed wakes with every other answer in between keep the run, and the sixteenth falls back",
      !fb && t.misses == 15 && nvrm_evw_account(&t, NVRM_EVW_R_TIMEOUT, 1, 2000, &missed));
    /* who sleeps when, for steady waits: the numbers the comment above nvrm_evw_begin gives */
    int first = 0, e = 0;
    e = steady(900000, 2000, 0, &first);
    T("steady waits under 1 ms are polled, every one", e == 0 && first == -1);
    int rare = 1;
    for (uint64_t l = 1100000; l <= 2000000; l += 300000) { e = steady(l, 2000, 0, &first); if (e < 1800 / 60 || e > 1800 / 20 || first < 30 || first > 70) rare = 0; }
    T("steady waits of 1 to 2 ms: polled for the first millisecond, but one in forty or so sleeps after the spin", rare);
    e = steady(2100000, 2000, 0, &first);
    T("steady waits a little over one spin and one sleep: after some fifty waits, all sleep after the spin", e == 1800 && first >= 30 && first <= 70);
    e = steady(2900000, 2000, 0, &first);
    T("steady waits just under 3 ms: the same", e == 1800 && first >= 30 && first <= 70);
    e = steady(3100000, 2000, 0, &first);
    T("steady waits longer than 3 ms: all sleep after the spin from the third", e == 1800 && first == 2);
    e = steady(1500000, 2000, 1, &first);
    T("control: a mean of whole wait lengths would make 1.5 ms waits sleep after the spin every time", e == 1800 && first >= 0 && first < 10);
    t.misses = 0; fb = 0;
    for (int i = 0; i < 100; i++) fb |= nvrm_evw_account(&t, NVRM_EVW_R_TIMEOUT, 0, 2000, &missed);
    T("a timeout while the GPU is still working is not a miss", !fb && t.misses == 0);
    for (int i = 0; i < 100; i++) fb |= nvrm_evw_account(&t, NVRM_EVW_R_TIMEOUT, 1, 700, &missed);
    T("a sleep cut short by the deadline is not a miss", !fb && t.misses == 0);
    for (int i = 0; i < 100; i++) fb |= nvrm_evw_account(&t, NVRM_EVW_R_LEGACY, 1, 2000, &missed);
    T("a 2.9 kext never trips the fallback", !fb && t.misses == 0);
    T("capped, aborted and spurious returns poll one step", nvrm_evw_must_poll(NVRM_EVW_R_CAPPED) && nvrm_evw_must_poll(NVRM_EVW_R_ABORTED) && nvrm_evw_must_poll(NVRM_EVW_R_SPURIOUS) && !nvrm_evw_must_poll(NVRM_EVW_R_WOKEN) && !nvrm_evw_must_poll(NVRM_EVW_R_TIMEOUT) && !nvrm_evw_must_poll(NVRM_EVW_R_LEGACY));
    printf(fails ? "\n%d FAILED\n" : "\nall passed\n", fails);
    return fails != 0;
}
