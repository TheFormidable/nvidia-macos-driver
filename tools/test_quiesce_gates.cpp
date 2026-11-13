/* test_quiesce_gates.cpp - the three small state machines of kexts/NVRM/nv_xnu_quiesce.h (entry gate, generation gate,
   bottom-half hand-off) and the slot table of kexts/NVRM/nv_xnu_slots.h, single-threaded and under real threads.
   Build and run from the repository root:
     c++ -std=c++17 -O2 -Wall -Wextra -Werror -pthread -I kexts/NVRM tools/test_quiesce_gates.cpp -o /tmp/test_quiesce_gates && /tmp/test_quiesce_gates
   Optional argument: seconds per threaded scenario (default 1; use 5 or more, and -fsanitize=thread, for a long run).

   What the kernel relies on, and what is shown here:
   - the interrupt gate: once irqSettle() has closed it and seen it idle, no interrupt handler is inside the RM and none
     gets in until irqOpen(); while open, handlers are never turned away;
   - the generation gate: once a timer has been cancelled (generation moved on) and the wait for idle returned, no
     callback armed before runs its body, so the timer's slot can be reused: a late callback never sees the payload of
     a later timer, or a payload that has been released;
   - the bottom-half hand-off: a requested run is never dropped, also when the thread is told to quit at the same time
     (the run that releases the GPU lock the interrupt handler kept must always happen);
   - every one of them does nothing before its start: a zeroed gate is open and idle, a zeroed hand-off has nothing to
     run and counts as drained, a slot that was never claimed is never found and giving it back does nothing;
   - the slot table hands one slot to each owner, the same one when asked twice, none when full.
   Each threaded property has a negative control: the same scenario with one line of the logic broken must fail. */
#include "nv_xnu_quiesce.h"
#include "nv_xnu_slots.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <random>
#include <thread>
#include <vector>

static int g_fail;
static void check(const char *name, bool ok, const char *detail = "")
{
    std::printf("%s %s%s%s\n", ok ? "ok  " : "FAIL", name, (!ok && detail[0]) ? " - " : "", ok ? "" : detail);
    if (!ok) g_fail++;
}
static double g_seconds = 1.0;
using Clock = std::chrono::steady_clock;
static bool elapsed(Clock::time_point t0, double s) { return std::chrono::duration<double>(Clock::now() - t0).count() >= s; }

/* ---- variants of the logic, so the negative controls run the very same scenarios ---------------------------------- */
struct GateOps {
    int (*enter)(NvXnuGate *);
};
static int enter_real(NvXnuGate *g) { return nv_gate_enter(g); }
/* broken: never looks at `closed` after announcing itself (the re-check the header's argument rests on) */
static int enter_no_recheck(NvXnuGate *g) { __atomic_add_fetch(&g->inside, 1, __ATOMIC_SEQ_CST); return 1; }
/* broken: looks first and announces itself afterwards, so a closer can see "idle" in between */
static int enter_check_first(NvXnuGate *g)
{
    if (__atomic_load_n(&g->closed, __ATOMIC_SEQ_CST)) return 0;
    std::this_thread::yield();
    __atomic_add_fetch(&g->inside, 1, __ATOMIC_SEQ_CST);
    return 1;
}
/* broken: a lock instead of a gate. It keeps the closer's guarantee, but turns an enterer away while another one is
   inside: with two handlers on two work loops, an interrupt dropped although nobody closed anything */
static int enter_one_at_a_time(NvXnuGate *g)
{
    if (__atomic_add_fetch(&g->inside, 1, __ATOMIC_SEQ_CST) != 1 || __atomic_load_n(&g->closed, __ATOMIC_SEQ_CST)) {
        __atomic_sub_fetch(&g->inside, 1, __ATOMIC_SEQ_CST);
        return 0;
    }
    return 1;
}

static int gen_enter_real(NvXnuGen *g, unsigned long long armed) { return nv_gen_enter(g, armed); }
/* broken: runs whatever generation the callback was armed with */
static int gen_enter_no_check(NvXnuGen *g, unsigned long long) { __atomic_add_fetch(&g->inside, 1, __ATOMIC_SEQ_CST); return 1; }

static int bh_next_real(NvXnuBh *b) { return nv_bh_next(b); }
/* broken: tests quit before pending, which is what dropped a pending bottom half (and with it the GPU lock) at stop */
static int bh_next_quit_first(NvXnuBh *b)
{
    if (b->quit) return NV_BH_EXIT;
    if (b->pending) { b->pending = 0; b->busy = 1; return NV_BH_RUN; }
    return NV_BH_SLEEP;
}

/* ---- single-threaded rules -------------------------------------------------------------------------------------------- */
static void rules()
{
    NvXnuGate g = {};
    check("gate: zeroed is open and idle", nv_gate_idle(&g) && g.closed == 0);
    check("gate: an enter on an open gate gets in and makes it busy", nv_gate_enter(&g) == 1 && !nv_gate_idle(&g));
    nv_gate_leave(&g);
    check("gate: the leave makes it idle again", nv_gate_idle(&g));
    nv_gate_close(&g);
    check("gate: closed, an enter is refused and leaves nothing behind", nv_gate_enter(&g) == 0 && nv_gate_idle(&g));
    nv_gate_close(&g);
    check("gate: closing twice is the same as once", nv_gate_enter(&g) == 0 && nv_gate_idle(&g));
    nv_gate_open(&g);
    check("gate: opened again, an enter gets in", nv_gate_enter(&g) == 1);
    nv_gate_close(&g);
    check("gate: closed while somebody is inside, it is not idle until that one leaves", !nv_gate_idle(&g));
    nv_gate_leave(&g);
    check("gate: ... and idle after the leave", nv_gate_idle(&g));

    NvXnuGen t = {};
    check("generation: zeroed is generation 0 and idle", nv_gen_current(&t) == 0 && nv_gen_idle(&t));
    const unsigned long long a = nv_gen_current(&t);
    check("generation: a callback armed with the current generation runs", nv_gen_enter(&t, a) == 1 && !nv_gen_idle(&t));
    nv_gen_leave(&t);
    const unsigned long long b = nv_gen_invalidate(&t);
    check("generation: invalidate returns the new generation", b == a + 1 && nv_gen_current(&t) == b);
    check("generation: a callback armed before is refused and leaves nothing behind", nv_gen_enter(&t, a) == 0 && nv_gen_idle(&t));
    check("generation: one armed with the new generation runs", nv_gen_enter(&t, b) == 1);
    nv_gen_leave(&t);

    NvXnuBh h = {};
    check("bottom half: zeroed has nothing to run and counts as drained", nv_bh_next(&h) == NV_BH_SLEEP && nv_bh_drained(&h));
    nv_bh_request(&h);
    check("bottom half: a request is not drained until it ran", !nv_bh_drained(&h));
    check("bottom half: a request runs once", nv_bh_next(&h) == NV_BH_RUN && h.busy == 1 && h.pending == 0 && !nv_bh_drained(&h));
    nv_bh_request(&h);
    nv_bh_request(&h);
    nv_bh_done(&h);
    const int again = nv_bh_next(&h);
    nv_bh_done(&h);
    check("bottom half: requests made while it runs are served by one more run", again == NV_BH_RUN);
    check("bottom half: then nothing is left", nv_bh_next(&h) == NV_BH_SLEEP && nv_bh_drained(&h));
    nv_bh_request(&h);
    h.quit = 1;
    const int first = nv_bh_next(&h);
    nv_bh_done(&h);
    const int second = nv_bh_next(&h);
    check("bottom half: pending and quit together still runs once, then exits", first == NV_BH_RUN && second == NV_BH_EXIT);
    NvXnuBh q = {};
    q.quit = 1;
    check("bottom half: quit with nothing pending exits at once", nv_bh_next(&q) == NV_BH_EXIT);
    NvXnuBh c = {};
    nv_bh_request(&c);
    c.quit = 1;
    check("control: a hand-off that tests quit first drops the pending run", bh_next_quit_first(&c) == NV_BH_EXIT);

    void *volatile slots[4] = {};
    int o[6];
    check("slots: nothing is found before anything was claimed", nv_slot_find(slots, 4, &o[0]) == -1);
    check("slots: giving back a slot that was never claimed does nothing", !nv_slot_release(slots, 4, -1, &o[0]) && !nv_slot_release(slots, 4, 2, &o[0]));
    const int s0 = nv_slot_claim(slots, 4, &o[0]), s1 = nv_slot_claim(slots, 4, &o[1]);
    check("slots: two owners get two slots", s0 >= 0 && s1 >= 0 && s0 != s1);
    check("slots: claiming twice gives the same slot", nv_slot_claim(slots, 4, &o[0]) == s0);
    check("slots: an owner is found in its slot", nv_slot_find(slots, 4, &o[1]) == s1);
    nv_slot_claim(slots, 4, &o[2]);
    nv_slot_claim(slots, 4, &o[3]);
    check("slots: a fifth owner of four slots gets none", nv_slot_claim(slots, 4, &o[4]) == -1);
    check("slots: another owner cannot give a slot back", !nv_slot_release(slots, 4, s1, &o[0]) && nv_slot_find(slots, 4, &o[1]) == s1);
    check("slots: the owner can, and the slot is free again", nv_slot_release(slots, 4, s1, &o[1]) && nv_slot_find(slots, 4, &o[1]) == -1
          && nv_slot_claim(slots, 4, &o[4]) == s1);
    check("slots: a second give-back does nothing", !nv_slot_release(slots, 4, s1, &o[1]) && nv_slot_find(slots, 4, &o[4]) == s1);
    check("slots: a NULL owner is never claimed or found", nv_slot_claim(slots, 4, nullptr) == -1 && nv_slot_find(slots, 4, nullptr) == -1);
}

/* ---- the interrupt gate under threads ----------------------------------------------------------------------------------
   Enterers stand for interrupt handlers on several work loops, the closer for irqSettle()/irqOpen(). While the closer
   holds the gate closed and has seen it idle ("sealed"), an enterer that gets in, or one still inside, is a violation.

   The other half is that an open gate turns nobody away: a refused enter is an interrupt whose top half did not run.
   "Was it open" cannot be answered by reading `closed` before and after the attempt (a whole close and open fits in
   between), so the closer counts in `era`: odd from just before a close until just after the open, even in between.
   An enterer that reads the same even number before and after its attempt had the gate open all the way through, and
   a refusal then is a violation. Any other pair of readings proves nothing and is not counted.

   The closer's wait for idle has a bound, as every such wait in the kernel has, and running into it is a violation: a
   closed gate empties as soon as the enterers inside have had the processor once. Without the bound the first control
   below did not end on a machine with one processor: its enter ignores `closed`, the closer only gets to look while
   all three enterers are switched out, and they are switched out inside the gate nearly every time. */
static const double kIdleBound = 10.0;   // seconds
struct GateRun { long bad, cycles, open_tries, turned_away; };
static GateRun gate_scenario(const GateOps &ops, double seconds)
{
    NvXnuGate g = {};
    std::atomic<int> sealed{0}, users{0}, stop{0};
    std::atomic<long> bad{0}, entered{0}, open_tries{0}, refused_open{0};
    std::atomic<unsigned long> era{0};
    auto enterer = [&] {
        std::mt19937 r(std::random_device{}());
        while (!stop.load()) {
            const unsigned long before = era.load();
            const int in = ops.enter(&g);
            if (!(before & 1) && era.load() == before) {   // open from before the attempt until after it
                open_tries++;
                if (!in) refused_open++;
            }
            if (in) {
                if (sealed.load()) bad++;
                users++;
                for (int i = (int)(r() % 64); i > 0; i--) std::atomic_signal_fence(std::memory_order_seq_cst);
                if (r() % 8 == 0) std::this_thread::yield();
                if (sealed.load()) bad++;
                users--;
                nv_gate_leave(&g);
                entered++;
            }
        }
    };
    std::vector<std::thread> th;
    for (int i = 0; i < 3; i++) th.emplace_back(enterer);
    const auto t0 = Clock::now();
    long cycles = 0;
    std::mt19937 r(12345);
    while (!elapsed(t0, seconds)) {
        era++;                                   // odd: a close is on its way
        nv_gate_close(&g);
        const auto closed_at = Clock::now();
        bool idle = nv_gate_idle(&g);
        while (!idle && !elapsed(closed_at, kIdleBound)) { std::this_thread::yield(); idle = nv_gate_idle(&g); }
        if (!idle) { bad++; break; }             // closed, and still somebody inside after the bound
        sealed.store(1);
        if (users.load() != 0) bad++;
        for (int i = (int)(r() % 200); i > 0; i--) std::this_thread::yield();
        if (users.load() != 0) bad++;
        sealed.store(0);
        nv_gate_open(&g);
        era++;                                   // even: open, and it stays open until the next odd number
        for (int i = (int)(r() % 200); i > 0; i--) std::this_thread::yield();
        cycles++;
    }
    stop.store(1);
    for (auto &t : th) t.join();
    return GateRun{bad.load() + (entered.load() == 0 ? 1 : 0), cycles, open_tries.load(), refused_open.load()};
}

/* ---- the generation gate with a timer slot that is reused ------------------------------------------------------------
   The model of one nano-timer slot: the owner creates a timer (a payload), arms it (hands a token to the "timer" thread,
   which delivers it late and at random), cancels and destroys it (moves the generation on and waits for idle), then
   releases the payload and creates the next one in the same slot. The callback body must only ever see the payload
   the token was armed for, and never one that was released. */
struct Payload { std::atomic<int> alive{0}; std::atomic<long> id{0}; };
struct Slot { NvXnuGen gate = {}; std::atomic<Payload *> payload{nullptr}; };
struct Shot { int slot; unsigned long long token; long id; };

static long gen_scenario(int (*enter)(NvXnuGen *, unsigned long long), double seconds, long *reused)
{
    const int kSlots = 2;
    Slot slots[kSlots];
    std::vector<Payload> pool(4096);   // never freed: a "released" payload is one marked dead, so a late read is defined
    std::mutex mbx;
    std::vector<Shot> mail;
    std::atomic<int> stop{0};
    std::atomic<long> bad{0}, ran{0};
    std::atomic<long> next_id{1};
    std::atomic<unsigned> pool_at{0};

    auto timer = [&] {
        std::mt19937 r(std::random_device{}());
        while (!stop.load()) {
            Shot s{-1, 0, 0};
            {
                std::lock_guard<std::mutex> l(mbx);
                if (!mail.empty()) { size_t i = r() % mail.size(); s = mail[i]; mail[i] = mail.back(); mail.pop_back(); }
            }
            if (s.slot < 0) { std::this_thread::yield(); continue; }
            for (int i = (int)(r() % 50); i > 0; i--) std::this_thread::yield();   // delivered late
            Slot &sl = slots[s.slot];
            if (!enter(&sl.gate, s.token)) continue;
            Payload *p = sl.payload.load();
            if (!p || !p->alive.load() || p->id.load() != s.id) bad++;
            for (int i = (int)(r() % 20); i > 0; i--) std::this_thread::yield();
            if (!p || !p->alive.load() || p->id.load() != s.id) bad++;
            ran++;
            nv_gen_leave(&sl.gate);
        }
    };
    auto owner = [&](int k) {
        std::mt19937 r(std::random_device{}() + (unsigned)k);
        Slot &sl = slots[k];
        while (!stop.load()) {
            Payload *p = &pool[pool_at++ % pool.size()];
            p->id.store(next_id++);
            p->alive.store(1);
            sl.payload.store(p);
            unsigned long long tok = nv_gen_invalidate(&sl.gate);          // create
            for (int arm = (int)(r() % 4) + 1; arm > 0; arm--) {
                std::lock_guard<std::mutex> l(mbx);
                mail.push_back(Shot{k, tok, p->id.load()});               // start (and re-arm)
            }
            if (r() % 2) {                                                 // cancel, then start again
                tok = nv_gen_invalidate(&sl.gate);
                while (!nv_gen_idle(&sl.gate)) std::this_thread::yield();
                std::lock_guard<std::mutex> l(mbx);
                mail.push_back(Shot{k, tok, p->id.load()});
            }
            for (int i = (int)(r() % 100); i > 0; i--) std::this_thread::yield();
            nv_gen_invalidate(&sl.gate);                                   // destroy: the generation moves on ...
            while (!nv_gen_idle(&sl.gate)) std::this_thread::yield();      // ... and the wait for idle
            p->alive.store(0);                                             // the payload is released
        }
    };
    std::vector<std::thread> th;
    for (int i = 0; i < 2; i++) th.emplace_back(timer);
    for (int k = 0; k < kSlots; k++) th.emplace_back(owner, k);
    const auto t0 = Clock::now();
    while (!elapsed(t0, seconds)) std::this_thread::sleep_for(std::chrono::milliseconds(10));
    stop.store(1);
    for (auto &t : th) t.join();
    *reused = next_id.load() - 1;
    return bad.load() + (ran.load() == 0 ? 1 : 0);
}

/* ---- the bottom-half hand-off under threads -----------------------------------------------------------------------------
   One "interrupt handler" thread requests runs, as interruptOccurred does (inside the gate), one bottom-half thread
   serves them, as bottomHalfThread does. Each round ends like a stop: the gate is closed and settled, then the thread is
   told to quit, sometimes right after a request it has not picked up yet. Every request must be followed by a run that
   started after it; a dropped one is a GPU lock never released. */
static long bh_scenario(int (*next)(NvXnuBh *), double seconds, long *rounds_out)
{
    long bad = 0, rounds = 0;
    const auto t0 = Clock::now();
    std::mt19937 r(777);
    while (!elapsed(t0, seconds)) {
        std::mutex lock;
        std::condition_variable cv;
        NvXnuBh bh = {};
        NvXnuGate gate = {};
        long requested = 0, served = 0;   // under lock: the request count when the last run started
        bh.alive = 1;
        std::thread worker([&] {
            std::unique_lock<std::mutex> l(lock);
            for (;;) {
                const int what = next(&bh);
                if (what == NV_BH_EXIT) break;
                if (what == NV_BH_SLEEP) { cv.wait(l); continue; }
                served = requested;
                l.unlock();
                std::this_thread::yield();
                l.lock();
                nv_bh_done(&bh);
                cv.notify_all();
            }
            bh.alive = 0;
            cv.notify_all();
        });
        std::atomic<int> stop{0};
        std::thread handler([&] {
            std::mt19937 rr(std::random_device{}());
            while (!stop.load()) {
                if (!nv_gate_enter(&gate)) { std::this_thread::yield(); continue; }
                if (rr() % 2) {
                    std::lock_guard<std::mutex> l(lock);
                    nv_bh_request(&bh);
                    requested++;
                    cv.notify_one();
                }
                nv_gate_leave(&gate);
                if (rr() % 4 == 0) std::this_thread::yield();
            }
        });
        for (int i = (int)(r() % 300); i > 0; i--) std::this_thread::yield();
        // irqSettle: close, wait for the handler to be out ...
        nv_gate_close(&gate);
        while (!nv_gate_idle(&gate)) std::this_thread::yield();
        stop.store(1);
        handler.join();
        {
            std::unique_lock<std::mutex> l(lock);
            if (r() % 2) {
                // ... and drain (bottomHalfDrain): every request served before the quit
                cv.wait_for(l, std::chrono::seconds(5), [&] { return nv_bh_drained(&bh); });
                if (!nv_bh_drained(&bh)) bad++;
            } else {
                // a request the thread has not picked up yet, and the quit right behind it (bottomHalfStop at once)
                nv_bh_request(&bh);
                requested++;
            }
            // bottomHalfStop
            bh.quit = 1;
            cv.notify_all();
            cv.wait_for(l, std::chrono::seconds(5), [&] { return bh.alive == 0; });
            if (bh.alive) bad++;
            if (served != requested) bad++;   // the last request was never run
        }
        worker.join();
        rounds++;
    }
    *rounds_out = rounds;
    return bad + (rounds == 0 ? 1 : 0);
}

/* ---- the slot table under threads: eight GPUs starting at once get eight different slots ---------------------------- */
static long slot_scenario(double seconds, long *rounds_out)
{
    long bad = 0, rounds = 0;
    const auto t0 = Clock::now();
    while (!elapsed(t0, seconds)) {
        void *volatile slots[8] = {};
        int owners[9];
        int got[9];
        std::vector<std::thread> th;
        for (int i = 0; i < 9; i++) th.emplace_back([&, i] { got[i] = nv_slot_claim(slots, 8, &owners[i]); });
        for (auto &t : th) t.join();
        int none = 0, seen[8] = {};
        for (int i = 0; i < 9; i++) {
            if (got[i] < 0) { none++; continue; }
            if (seen[got[i]]++) bad++;
            if (slots[got[i]] != &owners[i]) bad++;
        }
        if (none != 1) bad++;
        rounds++;
    }
    *rounds_out = rounds;
    return bad;
}

int main(int argc, char **argv)
{
    if (argc > 1) g_seconds = std::atof(argv[1]);
    if (const char *e = std::getenv("QUIESCE_SECONDS")) g_seconds = std::atof(e);
    if (g_seconds <= 0) g_seconds = 1.0;
    std::printf("threaded scenarios: %.1f s each\n", g_seconds);
    rules();

    char why[160];
    long n = 0;
    GateRun gr = gate_scenario(GateOps{enter_real}, g_seconds);
    std::snprintf(why, sizeof why, "%ld violations in %ld close/open cycles", gr.bad, gr.cycles);
    check("gate: after close and idle nobody is inside and nobody gets in, under three enterers", gr.bad == 0 && gr.cycles > 0, why);
    std::snprintf(why, sizeof why, "%ld of %ld attempts made while the gate was open were turned away", gr.turned_away, gr.open_tries);
    check("gate: while it is open no enterer is turned away", gr.turned_away == 0 && gr.open_tries > 0, why);
    gr = gate_scenario(GateOps{enter_no_recheck}, g_seconds / 2);
    std::snprintf(why, sizeof why, "%ld violations in %ld cycles", gr.bad, gr.cycles);
    check("control: an enter without the re-check of closed is caught", gr.bad > 0, why);
    gr = gate_scenario(GateOps{enter_check_first}, g_seconds);
    std::snprintf(why, sizeof why, "%ld violations in %ld cycles", gr.bad, gr.cycles);
    check("control: an enter that looks before it announces itself is caught", gr.bad > 0, why);
    gr = gate_scenario(GateOps{enter_one_at_a_time}, g_seconds / 2);
    std::snprintf(why, sizeof why, "%ld of %ld attempts made while the gate was open were turned away", gr.turned_away, gr.open_tries);
    check("control: an enter that lets one in at a time is caught turning enterers away from an open gate", gr.turned_away > 0, why);

    long bad = gen_scenario(gen_enter_real, g_seconds, &n);
    std::snprintf(why, sizeof why, "%ld bad callbacks over %ld timers", bad, n);
    check("generation: a late callback never sees a released payload or a later timer's payload", bad == 0 && n > 0, why);
    bad = gen_scenario(gen_enter_no_check, g_seconds / 2, &n);
    std::snprintf(why, sizeof why, "%ld bad callbacks over %ld timers", bad, n);
    check("control: a callback that does not check its generation is caught", bad > 0, why);

    bad = bh_scenario(bh_next_real, g_seconds, &n);
    std::snprintf(why, sizeof why, "%ld problems in %ld start/stop rounds", bad, n);
    check("bottom half: no request is dropped, also when the quit follows it at once", bad == 0 && n > 0, why);
    bad = bh_scenario(bh_next_quit_first, g_seconds / 2, &n);
    std::snprintf(why, sizeof why, "%ld problems in %ld rounds", bad, n);
    check("control: a hand-off that tests quit before pending is caught", bad > 0, why);

    bad = slot_scenario(g_seconds / 2, &n);
    std::snprintf(why, sizeof why, "%ld problems in %ld rounds", bad, n);
    check("slots: nine owners racing for eight slots: eight distinct slots, one refusal", bad == 0 && n > 0, why);

    std::printf("%s\n", g_fail ? "FAILED" : "PASS");
    return g_fail ? 1 : 0;
}
