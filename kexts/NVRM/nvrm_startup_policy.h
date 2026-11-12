/* Startup timing decisions shared by NVRM (auto-go) and NVAccel (headless check). Pure functions, no kernel calls,
 * so tools/test_startup_policy.py can compile and exercise them on a host. */
#ifndef NVRM_STARTUP_POLICY_H
#define NVRM_STARTUP_POLICY_H

// NVAccel starts as soon as NVRM claims the card, but the display heads only publish after auto-go has waited its
// settle delay (100 s on a card whose BAR1 could not be moved) and then run the bring-up. A headless deadline counted
// from NVAccel's start therefore fired before any head could exist, the Metal plugin name came back without arming,
// and WindowServer aborted every ten seconds. The deadline is the base wait added to the settle delay NVRM published.
// A settle value above five minutes is a typo in a boot argument, not a reason to withhold Metal indefinitely.
static inline unsigned nvrmHeadlessDeadlineSec(unsigned baseSec, unsigned settleMs)
{
    const unsigned kMaxSettleSec = 300;
    unsigned settleSec = (settleMs + 999u) / 1000u;
    if (settleSec > kMaxSettleSec) settleSec = kMaxSettleSec;
    return baseSec + settleSec;
}

// True when the display enumeration has finished on at least one connector list and none of them carries a display.
// counts[i] is the connected-displays value of nub i, or -1 when that nub has not published one yet. A nub that has
// not published is not evidence of an empty connector list, so it keeps the caller waiting.
static inline bool nvrmNoDisplayOnAnyConnector(const int *counts, unsigned n)
{
    bool seen = false;
    for (unsigned i = 0; i < n; i++) {
        if (counts[i] < 0) return false;
        seen = true;
        if (counts[i] > 0) return false;
    }
    return seen;
}

// Settle delay before auto-go brings the display up. nvrmsettle (milliseconds) overrides the default when present.
static inline unsigned nvrmAutoGoSettleMs(bool bar1Placed, bool turing, bool haveArg, unsigned argMs)
{
    unsigned ms = bar1Placed ? (turing ? 10000u : 500u) : 100000u;
    if (haveArg) ms = argMs;
    return ms;
}

// ---- The waits of auto-go between the settle and the release of the boot hold ----
// Each one is the 2.9 wait with an earlier exit that needs the very condition 2.9 exited on, and 2.9's upper bound:
// a condition that never comes true ends the wait when 2.9's did, never later, and no wait is without a bound. 2.9
// polled in 1 s steps, so the desktop came up to a second after it could have. -nvrmoldwaits (NVRM reads it once at
// start) passes a step of 1000 ms, and the stepper below is then 2.9's loop poll for poll (tools/test_startup_policy.py
// compares the two).
enum : unsigned {
    kNvrmHeadsWaitMs     = 20000,   // how long auto-go waits for the accelerator's heads: twenty 1 s polls in 2.9
    kNvrmHeadsPollMs     = 50,
    kNvrmHeadsStableMs   = 3000,    // "the published mask has not changed": three 1 s polls in a row in 2.9
    kNvrmHeadsRefreshMs  = 250,     // the connector counts (a registry walk) are read again at most this often
    kNvrmStartGateMaxMs  = 5000,    // the longest auto-go waits for start() to have returned (a wait 2.9 did not make)
    kNvrmStartGateStepMs = 10,
    kNvrmArmTries        = 10,      // tries of debug.nvaccelfb=1, 1 s apart in 2.9
    kNvrmArmStepMs       = 1000,
};
// Why the heads wait ended (debug.nvrm_boot_heads_why). The order of the checks is 2.9's.
enum NvrmHeadsWhy { kNvrmHeadsKeepWaiting = 0, kNvrmHeadsEnough = 1, kNvrmHeadsStable = 2, kNvrmHeadsAllConnected = 3,
                    kNvrmHeadsNoDisplay = 4, kNvrmHeadsHybridPanel = 5, kNvrmHeadsNoAccel = 6, kNvrmHeadsTimeout = 7 };
struct NvrmHeadsWait { unsigned stepMs, elapsedMs, stableMs; int last, want; bool hybrid; };

static inline unsigned nvrmPopCount(unsigned m) { unsigned c = 0; for (; m; m &= m - 1) c++; return c; }
static inline void nvrmHeadsWaitInit(NvrmHeadsWait *w, unsigned stepMs, int want, bool hybrid)
{
    w->stepMs = stepMs ? stepMs : 1000u; w->elapsedMs = 0; w->stableMs = 0; w->last = -1; w->want = want; w->hybrid = hybrid;
}
// One poll. mask = debug.nvaccel_heads_published (0 when it cannot be read), connected = connected-displays of display
// nub 0 (0 when unknown), noDisplayAnywhere = every connector list is enumerated and none carries a display (only
// looked at while the mask is 0, as in 2.9), accelRefused = the accelerator declined this macOS's class layout, so no
// head will ever be published to it. KeepWaiting: the caller sleeps w->stepMs and then calls nvrmHeadsWaitSlept().
static inline NvrmHeadsWhy nvrmHeadsWaitPoll(NvrmHeadsWait *w, unsigned mask, unsigned connected, bool noDisplayAnywhere,
                                             bool accelRefused)
{
    if (accelRefused) return kNvrmHeadsNoAccel;
    if (w->hybrid) return (mask & 0x10u) ? kNvrmHeadsHybridPanel : kNvrmHeadsKeepWaiting;   // the other GPU's panel is head 4
    const unsigned heads = nvrmPopCount(mask);
    if ((int)heads >= w->want) return kNvrmHeadsEnough;
    w->stableMs = (mask && (int)mask == w->last) ? w->stableMs + w->stepMs : 0;
    w->last = (int)mask;
    if (w->stableMs >= kNvrmHeadsStableMs) return kNvrmHeadsStable;
    if (connected > 0 && heads >= connected) return kNvrmHeadsAllConnected;
    if (!mask && noDisplayAnywhere) return kNvrmHeadsNoDisplay;
    return kNvrmHeadsKeepWaiting;
}
// After the caller's sleep of w->stepMs: false once the wait has lasted kNvrmHeadsWaitMs (2.9: after the twentieth sleep).
static inline bool nvrmHeadsWaitSlept(NvrmHeadsWait *w)
{
    w->elapsedMs += w->stepMs;
    return w->elapsedMs < kNvrmHeadsWaitMs;
}
// The boot raster measurement watches each head's line counter for 250 ms. A head that is scanning out at any rate the
// measurement can accept (20 Hz or more, more than 100 counter changes) changes it hundreds of times in 50 ms, so a
// counter that has not moved once in the first 50 ms proves "not running" as well as the full window does.
static inline bool nvrmRasterHeadIdle(unsigned long long elapsedNs, unsigned moves)
{
    return moves == 0 && elapsedNs >= 50000000ull;
}
// Between two tries of debug.nvaccelfb=1: a write that failed while head 0 was not published yet can only succeed once
// it is, so the retry may come as soon as bit 0 of the mask appears (polled, at most kNvrmArmStepMs later). With head
// 0 already there, or the mask unreadable, nothing readable says when to retry: the full step, as in 2.9.
static inline bool nvrmArmRetryMayPoll(bool maskReadable, unsigned mask)
{
    return maskReadable && !(mask & 1u);
}
// The accelerator declined the macOS class layout: no head will be published and no arm can succeed, so auto-go waits
// for what the boot hold is there for, the framebuffer's display path being up. 2.9 spent at most the heads wait plus
// its ten arm tries before it released the hold; this wait ends no later than that, and lasts at most 20 s itself.
static inline unsigned nvrmAccelRefusedWaitMs(unsigned headsElapsedMs)
{
    const unsigned total = kNvrmHeadsWaitMs + kNvrmArmTries * kNvrmArmStepMs;
    const unsigned left = headsElapsedMs < total ? total - headsElapsedMs : 0;
    return left < kNvrmHeadsWaitMs ? left : kNvrmHeadsWaitMs;
}

// A firmware that sizes BAR1 above 8 GB (16 GB on an RTX 4060 Ti 16 GB, 32 GB on the largest cards) leaves a window
// the bring-up has not run on: an RTX 3060 and an RTX 5080 resized to 16 GB both stopped during bring-up, and 8 GB is
// the size proven on hardware. Returns the size to resize to (8 GB), or 0 to leave BAR1 as it is: already 8 GB or
// smaller, the card does not offer 8 GB (bit 13 of the supported-sizes mask, 2^(20+13) bytes), or the user asked to
// keep the firmware's size.
static inline unsigned long long nvrmBar1ShrinkTarget(unsigned long long currentSize, unsigned supportedMask, bool keepFirmwareSize)
{
    const unsigned long long k8GB = 8ULL << 30;
    if (keepFirmwareSize || currentSize <= k8GB) return 0;
    return ((supportedMask >> 13) & 1) ? k8GB : 0;
}

// auto-go holds the registry until the display stack is armed so WindowServer does not start on a half-armed GPU. With
// no head published and no display on any connector, WindowServer runs on another GPU, so the hold is released before
// arming and the arming still runs, which keeps a display plugged in later working. Any head published: arm, then release.
static inline bool nvrmReleaseBeforeArming(unsigned headMask, bool noDisplayAnywhere)
{
    return headMask == 0 && noDisplayAnywhere;
}

// GSP stall handling in the RM (kernel_gsp.c, _kgspIsRpcTimeoutFatal and _kgspCheckAndLogTimeout), modelled here so the
// decision can be tested. An RPC that times out is classed by the GSP's heartbeat: a dead heartbeat is GSP_ERROR (the GSP
// is wedged or crashed), a live one is GSP_RPC_TIMEOUT (GSP-RM is slow). A crash report from CrashCat is always fatal and
// is looked at on every poll. With timeout classification off, which is the RM default, a dead heartbeat is not fatal
// until the same RPC has timed out `threshold` times, so a wedged GSP kept WindowServer polling for 6 s per RPC until
// then (#454). With it on, a dead heartbeat is fatal at the first timeout and a slow GSP keeps the old count.
static inline bool nvrmGspTimeoutFatal(bool crashReport, bool classificationOn, bool heartbeatAlive, unsigned timeoutCount, unsigned threshold)
{
    if (crashReport) return true;
    if (timeoutCount == threshold) return true;
    if (!heartbeatAlive) return classificationOn;
    return false;
}

// With no 64-bit memory window in the host bridge's ACPI _CRS, BAR1 is placed from a fixed fallback range only when
// something above 4 GB is known to be routed. A BAR1 the firmware left below 4 GB on a host that decodes nothing above
// 4 GB (Above 4G Decoding off) is fine where it is, and moving it made a board start; but a card whose BAR1 must grow
// to 4 GB or more (a 64 MB laptop BAR1 resized to 8 GB) can only live above 4 GB, so it needs the fallback range when
// other PCI devices already sit up there. 2.6.3 refused every unplaced BAR1 below 4 GB, and a 64 MB BAR1 that 2.6.2
// had resized stayed at 64 MB: the desktop froze on a stale frame.
static inline bool nvrmFallbackWindowAllowed(unsigned long long firmwareBar1, unsigned long long resizeTo, bool above4GRouted)
{
    const unsigned long long k4GB = 4ULL << 30;
    if (firmwareBar1 >= k4GB) return true;
    return resizeTo >= k4GB && above4GRouted;
}

// The name the registry carries when the RM's released-chip table has no entry for a PCI id (a newer board revision):
// the generation and chip the RM itself reports, never a made-up retail name. snprintf semantics; returns the length.
#ifdef KERNEL
#include <libkern/libkern.h>
#else
#include <stdio.h>
#endif
static inline int nvrmFallbackModelName(char *out, unsigned long outLen, const char *gen, const char *chip, unsigned deviceId)
{
    return snprintf(out, outLen, "NVIDIA %s GPU (%s, 10DE:%04X)", gen, chip, deviceId & 0xffff);
}

#endif
