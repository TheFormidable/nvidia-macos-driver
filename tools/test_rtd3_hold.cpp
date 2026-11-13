/* test_rtd3_hold.cpp - system sleep and wake against the opt-in idle power-down monitor (nvrtd3=1), under real threads.
   Build and run from the repository root:
     c++ -std=c++17 -O2 -Wall -Wextra -Werror -pthread -I kexts/NVRM tools/test_rtd3_hold.cpp -o /tmp/test_rtd3_hold && /tmp/test_rtd3_hold
   Optional argument: seconds the scenario of the real hold runs at least (default 1; use 5 or more, and
   -fsanitize=thread, for a long run).

   The monitor (kexts/NVRM/nv_rtd3.cpp) is a thread call that polls the GPU and, after a minute of idle, runs the GPU's
   own suspend path and switches the slot's power resource off; when something wants the GPU again it switches the
   resource on and runs the resume path. System sleep runs the same suspend and resume paths from the power manager's
   thread. NVRM.cpp keeps the two apart with a hold: setPowerState(0) sets a flag of the GPU, then waits until no monitor
   call is inside the monitor's gate (rtd3Hold); every monitor call enters that gate first and then does nothing while
   the flag is set (rtd3Held), except a call made by the thread that runs the wake (fRtd3Waker), which is the wake's
   own switch-on of the power resource (nv_rtd3_before_system_resume); the wake gives the GPU back after its resume
   (rtd3Release). The hold alone is not enough: a poll's suspend and its power-off (or its power-on and its resume) are
   two calls, and a whole sleep and wake can pass between them, so each call also looks at the GPU's state inside the
   gate (suspend only a running GPU, resume and power off only a suspended one).

   This runs the real gate (nv_xnu_quiesce.h) and the real poll (nv_rtd3_tick, nv_rtd3_policy.h) against a model of the
   GPU, with the bodies of rtd3Hold, rtd3Held, rtd3Release, setPowerState and the four monitor calls written as in
   NVRM.cpp (tools/test_irq_timer_wiring.py holds the NVRM.cpp side of them to that form). The monitor polls without
   pause, with a poll that stands for a whole minute, so it suspends, powers off, powers on and resumes the GPU as
   often as the processor allows; the client it sees comes and goes at random. What must hold:
   - the GPU's suspend and resume paths never run at the same time (both are one thread's work in the kernel), the
     suspend path runs only on a running GPU and the resume path only on a suspended one;
   - the power resource is never switched off under a GPU that is not suspended, or while a suspend or resume runs;
   - the resume path never runs with the power resource off (it would talk to a slot without power);
   - from the end of setPowerState(0) until the wake begins (the system is asleep and the slot loses power) nothing
     changes: no suspend or resume runs, the resource stays as it was, and the GPU is not marked running (a GPU marked
     running is not resumed by the wake);
   - the wake ends with the GPU running and the resource on, before it gives the GPU back.
   Each property has a negative control: the same scenario with one line of the hold broken must fail.
   The threads hand the processor to each other at every step (a yield), and a machine that is busy with other work
   answers a yield with another program's whole turn: there a second can hold a single sleep, which shows nothing
   either way. So no scenario is judged by what one second happened to hold: the real hold runs until it has seen
   kMinSleeps sleeps with the monitor powering the GPU off between them, a control until its broken line is caught,
   and each gives up kPatience seconds after its start. Every line ends with the numbers it was judged by.
   Under -fsanitize=thread the checks pass, and the sanitizer reports races on the monitor's plain fields (the state
   and the idle count of nv_rtd3_policy.h), which the wake's switch-on and the poll share in the kernel the same way:
   word-sized, and every decision that acts on them is made inside the gate under the hold. */
#include "nv_xnu_quiesce.h"
#include "nv_rtd3_policy.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <random>
#include <thread>

static int g_fail;
static void check(const char *name, bool ok, const char *detail = "")
{
    std::printf("%s %s%s%s\n", ok ? "ok  " : "FAIL", name, detail[0] ? " - " : "", detail);
    if (!ok) g_fail++;
}
static double g_seconds = 1.0;
static const long kMinSleeps = 200;                    // sleeps the real hold is judged by, where the machine allows
static const double kPatience = 30.0;                  // seconds a scenario waits for what it is judged by
using Clock = std::chrono::steady_clock;
static bool elapsed(Clock::time_point t0, double s) { return std::chrono::duration<double>(Clock::now() - t0).count() >= s; }

/* ---- which line of the hold is broken (all false: as in NVRM.cpp) ---------------------------------------------------- */
struct Variant {
    bool no_hold;            // setPowerState(0) takes no hold, the monitor calls never look at it
    bool hold_after_read;    // the hold is taken after setPowerState has read the GPU's state
    bool no_wait;            // the hold does not wait for a monitor call that is already inside
    bool no_waker;           // the wake does not record its thread: its own switch-on is refused like the monitor's
    bool no_release;         // the wake never gives the GPU back
    bool no_state_check;     // the monitor's suspend, resume and power-off do not look at the GPU's state first
};

/* ---- the model ------------------------------------------------------------------------------------------------------ */
enum { kGpuPmRunning = 0, kGpuPmSuspended = 1 };
static thread_local int tl_self;                      // current_thread()
static const void *current_thread_id() { return &tl_self; }

struct Model {
    Variant v{};
    std::atomic<int> fGpuPm{kGpuPmRunning};
    volatile int fRtd3Hold = 0;
    std::atomic<const void *> fRtd3Waker{nullptr};
    NvXnuGate gate = {1, 0};
    std::atomic<Model *> gpu{nullptr};                 // gNvRtd3Gpu
    struct nv_rtd3 rtd3 = {1, NV_RTD3_ON, 1};          // nvrtd3=1 with a power resource
    std::atomic<int> state_shadow{NV_RTD3_ON};         // what the slot really has (the model's resource)
    std::atomic<int> in_path{0};                       // inside gpuSuspend or gpuResume
    std::atomic<long> path_runs{0}, offs{0}, ons{0}, monitor_offs{0};
    std::atomic<long> bad{0};
    std::atomic<int> busy{0};                          // what rtd3Clients answers
    std::atomic<int> stop{0};
    unsigned idle = 0;

    static void spin(std::mt19937 &r, unsigned n) { for (unsigned i = r() % n; i > 0; i--) std::this_thread::yield(); }

    void gpuSuspend(std::mt19937 &r)
    {
        if (in_path.fetch_add(1) != 0) bad++;          // a second suspend or resume at the same time
        if (fGpuPm.load() != kGpuPmRunning) bad++;     // the suspend path of a GPU that is not running
        path_runs++;
        int e = kGpuPmRunning;
        fGpuPm.compare_exchange_strong(e, kGpuPmSuspended);   // marked early, as gpuSuspend marks it before the standby
        spin(r, 16);                                   // NVKMS, the RM gate, STANDBY
        in_path.fetch_sub(1);
    }
    void gpuResume(std::mt19937 &r)
    {
        if (in_path.fetch_add(1) != 0) bad++;
        if (fGpuPm.load() != kGpuPmSuspended) bad++;   // the resume path (RM RESUME) of a GPU that is not suspended
        path_runs++;
        if (state_shadow.load() != NV_RTD3_ON) bad++;  // resumed on a slot without power
        spin(r, 16);                                   // RM RESUME
        int e = kGpuPmSuspended;
        fGpuPm.compare_exchange_strong(e, kGpuPmRunning);
        in_path.fetch_sub(1);
    }
    // nv_rtd3_power_off / nv_rtd3_power_on: the monitor's state is set inside the call (nv_rtd3_off_done / _on_done)
    int power_off()
    {
        if (rtd3.state != NV_RTD3_ON) return 0;
        if (fGpuPm.load() != kGpuPmSuspended || in_path.load() != 0) bad++;   // off under a GPU that is not in standby
        state_shadow.store(NV_RTD3_OFF);
        offs++;
        nv_rtd3_off_done(&rtd3, 1);
        return 1;
    }
    int power_on()
    {
        if (rtd3.state != NV_RTD3_OFF) return 1;
        state_shadow.store(NV_RTD3_ON);
        ons++;
        nv_rtd3_on_done(&rtd3, 1);
        return 1;
    }

    /* as in NVRM.cpp */
    Model *rtd3Enter()
    {
        if (!nv_gate_enter(&gate)) return nullptr;
        Model *n = gpu.load();
        if (!n) nv_gate_leave(&gate);
        return n;
    }
    void rtd3Leave() { nv_gate_leave(&gate); }
    void rtd3Hold(double waitS)
    {
        if (v.no_hold) return;
        __atomic_store_n(&fRtd3Hold, 1, __ATOMIC_SEQ_CST);                    // OSCompareAndSwap(0, 1, &fRtd3Hold)
        if (gpu.load() != this) return;
        bool idle_now = nv_gate_idle(&gate);
        if (v.no_wait) return;
        const auto t0 = Clock::now();
        while (!idle_now && !elapsed(t0, waitS)) { std::this_thread::yield(); idle_now = nv_gate_idle(&gate); }
        if (!idle_now) bad++;                                                  // the 30 s ran out: counted as a failure here
    }
    void rtd3Release() { __atomic_store_n(&fRtd3Hold, 0, __ATOMIC_SEQ_CST); }
    // The monitor's calls look at the GPU's state inside the gate as well: the poll's suspend and its power-off (or its
    // power-on and its resume) are two calls, and a whole sleep and wake can come between them.
    bool pmIs(int want) { return v.no_state_check || fGpuPm.load() == want; }
    bool rtd3Held()
    {
        if (v.no_hold) return false;
        return __atomic_load_n(&fRtd3Hold, __ATOMIC_SEQ_CST) != 0 && current_thread_id() != fRtd3Waker.load();
    }
    // nv_rtd3_before_system_resume
    void before_system_resume(const struct nv_rtd3_ops *o)
    {
        if (rtd3.state != NV_RTD3_OFF) return;
        int ok = o->acpi_on ? o->acpi_on(o->ctx) : 0;
        nv_rtd3_on_done(&rtd3, ok);
        idle = 0;
    }
    void setPowerState(unsigned long ordinal, const struct nv_rtd3_ops *o, std::mt19937 &r)
    {
        if (ordinal == 0 && !v.hold_after_read) rtd3Hold(10.0);
        const int pm = fGpuPm.load();
        if (ordinal == 0 && v.hold_after_read) { spin(r, 4); rtd3Hold(10.0); }
        if (ordinal == 0 && pm == kGpuPmRunning) gpuSuspend(r);
        else if (ordinal != 0 && pm == kGpuPmSuspended) {
            if (!v.no_waker) fRtd3Waker.store(current_thread_id());
            before_system_resume(o);
            gpuResume(r);
            fRtd3Waker.store(nullptr);
        }
        if (ordinal != 0) {
            // before the GPU is given back: the wake has left it running on a powered slot
            if (fGpuPm.load() != kGpuPmRunning || state_shadow.load() != NV_RTD3_ON) bad++;
            if (!v.no_release) rtd3Release();
        }
    }
};

static Model *g_m;                                     // the monitor's calls take no context, as in NVRM.cpp
static thread_local std::mt19937 *tl_rng;
static int heads(void *) { if (!g_m->rtd3Enter()) return 0; g_m->rtd3Leave(); return 0; }
static int clients(void *) { Model *n = g_m->rtd3Enter(); if (!n) return 1; int b = n->busy.load(); n->rtd3Leave(); return b; }
// The poll can lose the processor between the two calls of a pair (suspend then power-off, power-on then resume).
static void op_suspend(void *) { if (Model *n = g_m->rtd3Enter()) { if (!n->rtd3Held() && n->pmIs(kGpuPmRunning)) n->gpuSuspend(*tl_rng); n->rtd3Leave(); } Model::spin(*tl_rng, 8); }
static void op_resume(void *) { if (Model *n = g_m->rtd3Enter()) { if (!n->rtd3Held() && n->pmIs(kGpuPmSuspended)) n->gpuResume(*tl_rng); n->rtd3Leave(); } }
static int op_off(void *) { int ok = 0; if (Model *n = g_m->rtd3Enter()) { if (!n->rtd3Held() && n->pmIs(kGpuPmSuspended)) { ok = n->power_off(); if (ok) n->monitor_offs++; } n->rtd3Leave(); } return ok; }
static int op_on(void *) { int ok = 0; if (Model *n = g_m->rtd3Enter()) { if (!n->rtd3Held()) ok = n->power_on(); n->rtd3Leave(); } Model::spin(*tl_rng, 8); return ok; }
static const struct nv_rtd3_ops kOps = { nullptr, heads, clients, op_suspend, op_resume, op_off, op_on };

struct Run { long bad, sleeps, path_runs, monitor_offs; double seconds; };
// control: the scenario ends at the first violation; else it runs for `seconds` and on until it has seen kMinSleeps
// sleeps and a power-off by the monitor. Either way it ends kPatience seconds after that at the latest.
static Run scenario(const Variant &v, double seconds, bool control)
{
    Model m;
    m.v = v;
    g_m = &m;
    m.gpu.store(&m);                                   // rtd3Arm
    nv_gate_open(&m.gate);
    std::thread monitor([&] {
        std::mt19937 r(std::random_device{}());
        tl_rng = &r;
        while (!m.stop.load()) {
            if (r() % 4 == 0) m.busy.store(r() % 2 ? 1 : 0);
            nv_rtd3_tick(&m.rtd3, &kOps, &m.idle, NV_RTD3_IDLE_SECONDS);   // one poll stands for the whole idle minute
            if (r() % 4 == 0) std::this_thread::yield();
        }
    });
    std::mt19937 r(4242);
    tl_rng = &r;
    long sleeps = 0;
    const auto t0 = Clock::now();
    const double least = control ? 0.0 : seconds;
    auto seen_enough = [&] {
        return control ? m.bad.load() > 0 : (sleeps >= kMinSleeps && m.monitor_offs.load() > 0);
    };
    while (!elapsed(t0, least) || (!seen_enough() && !elapsed(t0, least + kPatience))) {
        Model::spin(r, 64);                            // the system is awake for a while
        m.setPowerState(0, &kOps, r);
        // asleep: the slot loses power, and nothing may move until the wake
        const int pm0 = m.fGpuPm.load(), st0 = m.state_shadow.load();
        const long runs0 = m.path_runs.load();
        if (pm0 == kGpuPmRunning || m.in_path.load() != 0) m.bad++;
        Model::spin(r, 64);
        if (m.fGpuPm.load() != pm0 || m.state_shadow.load() != st0 || m.path_runs.load() != runs0 || m.in_path.load() != 0) m.bad++;
        m.setPowerState(1, &kOps, r);
        if (m.v.no_release) m.rtd3Release();           // so that the control's monitor keeps running into the next cycle
        sleeps++;
    }
    const double took = std::chrono::duration<double>(Clock::now() - t0).count();
    m.stop.store(1);
    monitor.join();
    g_m = nullptr;
    return Run{m.bad.load(), sleeps, m.path_runs.load(), m.monitor_offs.load(), took};
}

int main(int argc, char **argv)
{
    if (argc > 1) g_seconds = std::atof(argv[1]);
    if (g_seconds <= 0) g_seconds = 1.0;
    char why[200];

    Run run = scenario(Variant{}, g_seconds, false);
    std::snprintf(why, sizeof why, "%ld violations in %ld sleeps in %.1f s (%ld suspends and resumes, %ld power-offs by the monitor)",
                  run.bad, run.sleeps, run.seconds, run.path_runs, run.monitor_offs);
    check("the hold keeps the monitor out of every sleep and wake, and the monitor still powers the GPU off and on between them",
          run.bad == 0 && run.sleeps > 0 && run.monitor_offs > 0, why);

    struct { const char *name; Variant v; } controls[] = {
        {"control: a system sleep that leaves the GPU to the monitor is caught", Variant{true, false, false, false, false, false}},
        {"control: a hold taken only after the state was read is caught", Variant{false, true, false, false, false, false}},
        {"control: a hold that does not wait for the call inside is caught", Variant{false, false, true, false, false, false}},
        {"control: a wake whose own switch-on is refused is caught", Variant{false, false, false, true, false, false}},
        {"control: monitor calls that look only at the hold, not at the GPU's state, are caught", Variant{false, false, false, false, false, true}},
    };
    for (auto &c : controls) {
        run = scenario(c.v, g_seconds, true);
        std::snprintf(why, sizeof why, "%ld violations in %ld sleeps in %.1f s", run.bad, run.sleeps, run.seconds);
        check(c.name, run.bad > 0, why);
    }
    // A wake that never gives the GPU back keeps every sleep safe; what it breaks is the monitor, which never powers the
    // GPU off again after the first sleep.
    Variant nr{}; nr.no_release = true;
    {
        Model m;
        m.v = nr;
        g_m = &m;
        m.gpu.store(&m);
        nv_gate_open(&m.gate);
        std::mt19937 r(7);
        tl_rng = &r;
        m.setPowerState(0, &kOps, r);
        m.setPowerState(1, &kOps, r);
        const long offs0 = m.monitor_offs.load();
        for (int i = 0; i < 1000; i++) { m.busy.store(i % 3 == 0); nv_rtd3_tick(&m.rtd3, &kOps, &m.idle, NV_RTD3_IDLE_SECONDS); }
        const long without = m.monitor_offs.load() - offs0;
        m.rtd3Release();
        for (int i = 0; i < 1000; i++) { m.busy.store(i % 3 == 0); nv_rtd3_tick(&m.rtd3, &kOps, &m.idle, NV_RTD3_IDLE_SECONDS); }
        const long with = m.monitor_offs.load() - offs0 - without;
        g_m = nullptr;
        std::snprintf(why, sizeof why, "%ld power-offs while held after the wake, %ld once given back", without, with);
        check("control: a wake that never gives the GPU back leaves the monitor without a GPU (no power-off after it)",
              without == 0 && with > 0, why);
    }

    std::printf("%s\n", g_fail ? "FAILED" : "PASS");
    return g_fail ? 1 : 0;
}
