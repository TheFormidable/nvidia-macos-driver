/* nvmtl_loss.h - what the log says when the Vulkan device is lost. Pure text, so tools/test_loss.c proves it on the host.
   A lost device used to leave one line ("fence wait -> -4"), and WindowServer's later abort said nothing about why. */
#ifndef NVMTL_LOSS_H
#define NVMTL_LOSS_H
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static inline const char *nvmtl_vk_result_name(int r)
{
    switch (r) {
    case 0: return "VK_SUCCESS";
    case 2: return "VK_TIMEOUT";
    case -1: return "VK_ERROR_OUT_OF_HOST_MEMORY";
    case -2: return "VK_ERROR_OUT_OF_DEVICE_MEMORY";
    case -3: return "VK_ERROR_INITIALIZATION_FAILED";
    case -4: return "VK_ERROR_DEVICE_LOST";
    case -5: return "VK_ERROR_MEMORY_MAP_FAILED";
    default: return "an unknown VkResult";
    }
}
/* One sentence naming what ended the device: where it was noticed, the result, how long the wait ran, which caller was
   submitting, how long the device had been up, and what that combination means. `where` is "submit" or "fence". */
static inline void nvmtl_loss_cause(char *out, size_t cap, const char *where, int vkresult, long waited_ms, const char *who,
                                    long up_seconds, const char *process)
{
    const int submit = where && !strcmp(where, "submit");
    const char *meaning =
        vkresult == -4 && submit ? "the GPU channel was already dead when work was queued (an earlier fault or reset; see the fault-context lines above)" :
        vkresult == -4 ? "the GPU faulted or was reset while this work ran (see the fault-context lines above)" :
        vkresult == 2 ? "the fence neither signalled nor reported an error for the whole give-up time: a stalled channel" :
        (vkresult == -1 || vkresult == -2) ? "the driver ran out of memory while submitting, not a GPU fault" :
        "an unexpected result from the Vulkan driver";
    snprintf(out, cap, "device lost in %s: %s -> %s (%d) after %ld ms, submitted for %s, device up %ld s: %s",
             process ? process : "?", submit ? "queue submit" : "fence wait", nvmtl_vk_result_name(vkresult),
             vkresult, waited_ms, who ? who : "?", up_seconds, meaning);
}

/* NVK can rebuild a GPU channel that faulted or stalled instead of losing the device. It then signals the fences of the
   one submission it did not run (or, after a second fault in replayed work, of every submission that was in flight)
   from the CPU, and answers "dropped" for them through nvmtl_nvk_fence_dropped_v1: 1 dropped, 0 ran, -1 not one of its
   fences. Only 1 means dropped; -1 is treated as "ran", which is what a plugin without the question assumed. */
static inline int nvmtl_drop_seen(int answer) { return answer > 0; }
/* NVMTL_DROP_REPORT: unset, empty or anything else reports a dropped command buffer as failed; only a value starting
   with 0 reports it as completed (the opt-out, read once). */
static inline int nvmtl_drop_report_parse(const char *e) { return !(e && e[0] == '0'); }
/* What nvmtl_vk_submit_finish answers for a submission whose fence signalled: -1 is "retired with an error" (Metal sees
   a failed command buffer, nothing is quarantined, the backend stays up), never -2 (device lost). */
static inline int nvmtl_drop_result(int dropped, int report_on) { return dropped && report_on ? -1 : 0; }
/* the first dropped command buffer of a process and every 256th after it are logged */
static inline int nvmtl_drop_log_due(unsigned long long n) { return n == 1 || (n && (n & 0xFF) == 0); }

/* nvmtl_nvk_recover_stats_v1(out, n): NVK's channel-recovery counters for this process, version 1. */
enum {
    NVMTL_REC_VERSION = 0, NVMTL_REC_ENABLED = 1, NVMTL_REC_KICKS = 2, NVMTL_REC_REPLAYS = 3, NVMTL_REC_DROP_ALLS = 4,
    NVMTL_REC_DROPPED = 5, NVMTL_REC_REPLAYED = 6, NVMTL_REC_GAVE_UP = 7, NVMTL_REC_XID = 8, NVMTL_REC_KIND = 9,
    NVMTL_REC_WHY = 10, NVMTL_REC_INJECTED = 11, NVMTL_REC_KEXT = 12, NVMTL_REC_LAST_US = 13, NVMTL_REC_MAX_US = 14,
    NVMTL_REC_FENCES_DROPPED = 15, NVMTL_REC_N = 16
};
/* The kind of loss NVK classified (its enum nvrm_rec_kind). Only CHANNEL and STALL are rebuilt; the others end the device
   as before, and this name is what the log says about them. */
static inline const char *nvmtl_rec_kind_name(uint64_t k)
{
    static const char *const names[] = { "not classified", "CHANNEL (one channel faulted)", "STALL (a channel made no progress)",
        "MEMORY (ECC error)", "GSP (the GPU's firmware crashed or stopped answering)", "BUS (the GPU fell off the bus)",
        "RESUME (the GPU did not come back from sleep)", "UNKNOWN (an error NVK does not rebuild for)" };
    return k < sizeof names / sizeof names[0] ? names[k] : "?";
}
/* Why NVK did not rebuild (its enum nvrm_rec_why); 0 when the last attempt succeeded or none was made. */
static inline const char *nvmtl_rec_why_name(uint64_t w)
{
    static const char *const names[] = { "no refusal recorded", "recovery is off (NVK_CHANNEL_RECOVER=0)", "this kind of loss is not rebuilt",
        "3 rebuilds of this channel in the last 30 s already", "the channel's bookkeeping did not allow a plan",
        "the new channel could not be built", "the old channel could not be freed", "a copy channel faulted again in replayed work",
        "another rebuild of the channel took too long", "the channel was being destroyed",
        "the channel has a shape NVK cannot rebuild", "the channel's state was not captured",
        "the channel it waited on was still making progress", "the channel had already been given up" };
    return w < sizeof names / sizeof names[0] ? names[w] : "?";
}
/* One line on NVK's channel recovery for the device-loss log: the kind of loss it named and why it did not rebuild.
   Writes nothing and returns 0 when NVK has no such statistics (an older NVK) or a version this plugin does not know. */
static inline int nvmtl_loss_recovery_line(char *out, size_t cap, const uint64_t *st, unsigned n)
{
    if (!out || !cap) return 0;
    out[0] = 0;
    if (!st || n <= NVMTL_REC_WHY || st[NVMTL_REC_VERSION] != 1) return 0;
    if (!st[NVMTL_REC_ENABLED]) { snprintf(out, cap, "NVK channel recovery is off in this process, so nothing was rebuilt"); return 1; }
    snprintf(out, cap, "NVK channel recovery: last loss %s, Xid %llu, not rebuilt because: %s; %llu rebuilt (%llu replayed, %llu dropped all), "
             "%llu flushes dropped, %llu given up in this process",
             nvmtl_rec_kind_name(st[NVMTL_REC_KIND]), (unsigned long long)st[NVMTL_REC_XID], nvmtl_rec_why_name(st[NVMTL_REC_WHY]),
             (unsigned long long)(st[NVMTL_REC_REPLAYS] + st[NVMTL_REC_DROP_ALLS]), (unsigned long long)st[NVMTL_REC_REPLAYS],
             (unsigned long long)st[NVMTL_REC_DROP_ALLS], (unsigned long long)st[NVMTL_REC_DROPPED], (unsigned long long)st[NVMTL_REC_GAVE_UP]);
    return 1;
}
/* The line for a dropped command buffer: how many so far, how it is reported, and NVK's counters when it has them. It
   holds the word "fault" so that a release build keeps it in the system log (nvmtl_log_failure.h). */
static inline void nvmtl_drop_line(char *out, size_t cap, unsigned long long n, long waited_ms, const char *process,
                                   int report_on, const uint64_t *st, unsigned nst)
{
    if (!out || !cap) return;
    char rec[200] = "";
    if (st && nst > NVMTL_REC_XID && st[NVMTL_REC_VERSION] == 1)
        snprintf(rec, sizeof rec, "; NVK: Xid %llu, %llu channel rebuilds, %llu flushes dropped, %llu replayed",
                 (unsigned long long)st[NVMTL_REC_XID], (unsigned long long)(st[NVMTL_REC_REPLAYS] + st[NVMTL_REC_DROP_ALLS]),
                 (unsigned long long)st[NVMTL_REC_DROPPED], (unsigned long long)st[NVMTL_REC_REPLAYED]);
    snprintf(out, cap, "after a GPU fault, a channel rebuild dropped this command buffer (%llu so far in %s, fence after %ld ms): "
             "it did not run and is reported %s%s",
             n, process ? process : "?", waited_ms,
             report_on ? "as failed; the device stays up" : "as completed (NVMTL_DROP_REPORT=0)", rec);
}
#endif
