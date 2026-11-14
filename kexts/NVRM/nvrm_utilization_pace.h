/* When NVRM makes the GPU utilization read NVAccel asks for once a second (NVRM_UTILIZATION_FN). Pure functions, no
 * kernel calls, so tools/test_utilization_pace.py can compile and exercise them on a host.
 *
 * A read the firmware does not answer holds the RM's locks until the RM gives up on it (about 6 s). Issued again every
 * second it keeps other processes and the window server waiting on those locks, which stalls shutdown. The time a read
 * takes includes the wait for those locks, so one slow read can also be another caller (a mode set, a channel recovery)
 * holding them: a slow read pauses the sampler, kUtilSlowStop in a row stop it for this boot, and a fast answer, or a
 * fast failure, starts the count again. */
#ifndef NVRM_UTILIZATION_PACE_H
#define NVRM_UTILIZATION_PACE_H

// Longest a utilization read may take before it counts as slow: two orders above a control round trip, an order below
// the RM's own wait for a firmware answer (1.5 x its 4 s default). After each slow read the next kUtilBackoffNs are
// skipped, so a firmware that stopped answering holds the locks a few times in a minute, not once a second.
static const unsigned long long kUtilHangNs    = 500ull * 1000 * 1000;
static const unsigned long long kUtilBackoffNs = 20ull * 1000 * 1000 * 1000;
static const unsigned kUtilSlowStop = 3;

// A read is made at `now` only once the pause after the last slow read is over (both in the caller's clock).
static inline bool nvrmUtilPauseOver(unsigned long long now, unsigned long long notBefore)
{
    return now >= notBefore;
}

// After a read the sampler made, answered or not, that took tookNs: true when it was slow. A slow read adds one to
// *slowInRow and sets *stop at the kUtilSlowStop-th in a row; a fast one sets *slowInRow back to 0.
static inline bool nvrmUtilSlowRead(unsigned *slowInRow, bool *stop, unsigned long long tookNs)
{
    if (tookNs <= kUtilHangNs) { *slowInRow = 0; return false; }
    if (++*slowInRow >= kUtilSlowStop) *stop = true;
    return true;
}

#endif
