/* v301_boost_policy.m: the boost policy of release 3.0.1 (kexts/NVRM/nvrm_boost_policy_abi.h), read through NVRM's user
 * client as NVK reads it, by a process that is not root. The policy is one value of NVRM.kext (0 undecided, 1 idle
 * mode, 2 normal) that NVK reads through selector 13 at each renewal of its boost lease; reading is open, setting
 * needs root. The program opens the user client as NVK does (NVAccel with type 0x4E560000, the control node; NVRM with
 * type 0 when NVAccel refuses), and:
 *   read       a read (op 0) answers the value of the read-only sysctl debug.nvrm_boost_policy; 1000 reads in a row
 *              all answer it (their mean time is information: NVK makes one a second per busy process)
 *   refused    a set of the value 3 (past the last policy) is refused with kIOReturnBadArgument, a set of the value
 *              the policy already has is refused with kIOReturnNotPrivileged (this process is not root), and an
 *              unknown operation (2) with kIOReturnBadArgument; the only set it tries to make keeps the value as it is
 *   unchanged  the policy and debug.nvrm_boost_policy_sets (changes since boot) are as they were at the start
 * It makes no Metal device and no GPU work; it opens one user client and closes it.
 *   /usr/bin/xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -fobjc-arc -O2 tools/card/v301_boost_policy.m \
 *     -framework Foundation -framework Metal -framework IOKit -o <run>/bin/<id>/nvmtltest
 * It skips when it runs as root (a set would then be accepted), when the loaded NVRM.kext has no
 * debug.nvrm_boost_policy (a kext older than 3.0.1), and when neither NVAccel nor NVRM is there. It writes no file and
 * opens no window. Exit 0 pass, 1 a case failed, 2 setup only, 3 skip, 70 crash. Not yet run on a Mac. */
#import "plugin_common.h"
#include <IOKit/IOKitLib.h>
#include <mach/mach.h>
#include <sys/sysctl.h>
#include <time.h>

enum { SELECTOR = 13, OP_READ = 0, OP_SET = 1 };

static BOOL policy_sysctl(int *policy, int64_t *sets)
{
    size_t n = sizeof *policy;
    if (sysctlbyname("debug.nvrm_boost_policy", policy, &n, NULL, 0) || n != sizeof *policy) return NO;
    int64_t s = 0;
    n = sizeof s;
    if (sysctlbyname("debug.nvrm_boost_policy_sets", &s, &n, NULL, 0) || n != sizeof s) return NO;
    *sets = s;
    return YES;
}

static kern_return_t call(io_connect_t conn, uint64_t op, uint64_t value, uint64_t *out)
{
    uint64_t in[2] = {op, value}, o = UINT64_MAX;
    uint32_t n = 1;
    kern_return_t kr = IOConnectCallScalarMethod(conn, SELECTOR, in, 2, &o, &n);
    if (kr == KERN_SUCCESS && n != 1) return kIOReturnUnderrun;
    if (out) *out = o;
    return kr;
}

static double now_us(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1e6 + t.tv_nsec / 1e3;
}

int main(void)
{
    @autoreleasepool {
        nm_start(20);
        if (geteuid() == 0) nm_skip("runs as root: a set would be accepted, and a card check never runs as root");
        int p0 = -1;
        int64_t s0 = -1;
        if (!policy_sysctl(&p0, &s0))
            nm_skip("the loaded NVRM.kext has no debug.nvrm_boost_policy: its kext is older than release 3.0.1");
        io_connect_t conn = 0;
        const char *via = "NVAccel (type 0x4e560000)";
        io_service_t svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("NVAccel"));
        kern_return_t kr = svc ? IOServiceOpen(svc, mach_task_self(), 0x4E560000u, &conn) : KERN_FAILURE;
        if (svc) IOObjectRelease(svc);
        if (kr != KERN_SUCCESS) {
            nm_info("NVAccel: %s (0x%08x); NVRM with type 0 instead", svc ? "IOServiceOpen refused" : "not found", kr);
            via = "NVRM (type 0)";
            svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("NVRM"));
            kr = svc ? IOServiceOpen(svc, mach_task_self(), 0, &conn) : KERN_FAILURE;
            if (svc) IOObjectRelease(svc);
            if (!svc) nm_skip("neither NVAccel nor NVRM is in the I/O Registry");
        }
        if (kr != KERN_SUCCESS) {
            nm_setup("open", "IOServiceOpen of %s: 0x%08x", via, kr);
            return nm_finish();
        }
        nm_info("the user client of %s; at the start debug.nvrm_boost_policy %d, debug.nvrm_boost_policy_sets %lld",
                via, p0, (long long)s0);

        uint64_t got = UINT64_MAX;
        kr = call(conn, OP_READ, 0, &got);
        if (kr != KERN_SUCCESS)
            nm_fail("read", "a read answered 0x%08x", kr);
        else if (got != (uint64_t)p0)
            nm_fail("read", "a read answered %llu, debug.nvrm_boost_policy says %d", (unsigned long long)got, p0);
        else {
            unsigned same = 0;
            const double t0 = now_us();
            for (unsigned i = 0; i < 1000; i++) {
                uint64_t v = UINT64_MAX;
                if (call(conn, OP_READ, 0, &v) == KERN_SUCCESS && v == (uint64_t)p0) same++;
            }
            const double mean = (now_us() - t0) / 1000;
            nm_info("1000 reads: mean %.1f us each", mean);
            if (same == 1000)
                nm_pass("read", "a read answers %d, as debug.nvrm_boost_policy says, and 1000 more answer the same",
                        p0);
            else
                nm_fail("read", "%u of 1000 reads answered %d", same, p0);
        }

        unsigned refused = 0;
        kr = call(conn, OP_SET, 3, NULL);
        if (kr == kIOReturnBadArgument)
            refused++;
        else
            nm_fail("refused", "a set of the value 3 answered 0x%08x, not kIOReturnBadArgument (0x%08x)", kr,
                    kIOReturnBadArgument);
        kr = call(conn, OP_SET, (uint64_t)p0, NULL);
        if (kr == kIOReturnNotPrivileged)
            refused++;
        else
            nm_fail("refused",
                    "a set of the value %d by a process that is not root answered 0x%08x, not "
                    "kIOReturnNotPrivileged (0x%08x)",
                    p0, kr, kIOReturnNotPrivileged);
        kr = call(conn, 2, 0, NULL);
        if (kr == kIOReturnBadArgument)
            refused++;
        else
            nm_fail("refused", "operation 2 answered 0x%08x, not kIOReturnBadArgument (0x%08x)", kr,
                    kIOReturnBadArgument);
        if (refused == 3)
            nm_pass("refused", "a set of 3, a set without root and an unknown operation are refused with the "
                               "expected answers");
        IOServiceClose(conn);

        int p1 = -1;
        int64_t s1 = -1;
        if (!policy_sysctl(&p1, &s1))
            nm_fail("unchanged", "debug.nvrm_boost_policy could not be read at the end");
        else if (p1 != p0 || s1 != s0)
            nm_fail("unchanged",
                    "the policy went from %d to %d and its changes since boot from %lld to %lld during the "
                    "check (a set was accepted: this program's, or one of a root process such as NVMetrics)",
                    p0, p1, (long long)s0, (long long)s1);
        else
            nm_pass("unchanged", "the policy is still %d and its changes since boot still %lld", p1, (long long)s1);
        return nm_finish();
    }
}
