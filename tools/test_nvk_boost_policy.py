#!/usr/bin/env python3
"""Idle mode follows the kext's boost policy while apps run (nvk/nvk-boost-policy.patch).

NVK's boost lease (nvk/nvk-perf-boost-burst.patch) had its shape fixed when a process opened the device, so idle mode
reached only apps started after it was switched. Each renewal now reads NVRM.kext's policy (undecided, idle mode,
normal; kexts/NVRM/nvrm_boost_policy_abi.h) through user-client selector 13.

The kext's side: sBoostPolicy is cut out of kexts/NVRM/NVRM.cpp with the two globals it uses and compiled (c++) against
stand-ins for the caller's credential, the argument block, LOG, the atomic counter and the two gates, then called as
root, as another user, with values out of range, with READ and with the same value twice; the return codes, the policy,
the change counter and the gates taken are checked.
NVK's side: the patch's renewal code, its conf reader, the create-path block, the clear in the device's destroy and the
real nvrm_xnu_boost_policy are cut out of the patch's text (context and added lines are the file after the patch) and
compiled (cc) with a simulated clock, a simulated IOConnectCallScalarMethod (the kext) and a recording stand-in for the
resource manager. It runs through switches of the policy; a second renewal from inside the first one's RM call; a renewal
that another one completes between its read of the deadline and its move of it; and eight threads that start each round
at a barrier after the clock was moved to the deadline.
Linux or macOS, cc and c++ on PATH, no GPU. Without a compiler: SKIP (exit 2).

usage: test_nvk_boost_policy.py [driver checkout]"""
import collections, os, pathlib, re, shutil, subprocess, sys, tempfile

root = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
patch = root / "nvk/nvk-boost-policy.patch"
abi = root / "kexts/NVRM/nvrm_boost_policy_abi.h"
kext = root / "kexts/NVRM/NVRM.cpp"
fails = 0


def check(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({detail})" if detail and not ok else ""))
    fails += 0 if ok else 1


if not patch.exists() or not abi.exists():
    check("nvk/nvk-boost-policy.patch and kexts/NVRM/nvrm_boost_policy_abi.h exist", False)
    sys.exit(1)
ptext = patch.read_text()


def after(text):
    """the text as it reads after the patch: context lines and added lines, not the removed ones"""
    return "\n".join(l[1:] for l in text.split("\n") if l[:1] in (" ", "+") and not l.startswith("+++"))


added = after(ptext)
plus = [l[1:] for l in ptext.split("\n") if l.startswith("+") and not l.startswith("+++")]


def section(path):
    s = ptext.split("diff --git a/" + path + " ")
    return after(s[1].split("\ndiff --git ")[0]) if len(s) > 1 else ""


# --- the chain and the kext's text ---------------------------------------------------------------------------------
build = (root / "build/build_nvk.sh").read_text()
m = re.search(r'^\s*3\.0\)\s*FIXES=\S+\s*\n(?:\s*#[^\n]*\n)*\s*CHAIN="([^"]+)"', build, re.M)
chain = m.group(1).split() if m else []
# 3.0.1 appends nvk-mesa-main-2 and nvk-nvrm-fixes after it (nvk-nvrm-fixes is made on top of this patch)
check("the 3.0 chain applies nvk-boost-policy, after nvk-stream-uncached",
      "nvk-boost-policy" in chain and "nvk-stream-uncached" in chain
      and chain.index("nvk-stream-uncached") < chain.index("nvk-boost-policy"), " ".join(chain[-4:]))
check("the patch puts its own copy of the shared header in NVK",
      "+++ b/src/nouveau/vulkan/nvkmd/nvrm/nvrm_boost_policy_abi.h" in ptext)
abitext = abi.read_text()
sel = re.search(r"#define NVRM_BOOST_POLICY_SELECTOR\s+(\d+)u", abitext)
k = kext.read_text()
tbl = re.search(r"static const IOExternalMethodDispatch tbl\[\] = \{(.*?)\n    \};", k, re.S)
rows = re.findall(r"\{ *(s\w+), *([^}]*)\}", tbl.group(1)) if tbl else []
check("the selector number is the row of sBoostPolicy in the kext's method table",
      sel is not None and len(rows) > int(sel.group(1)) and rows[int(sel.group(1))][0] == "sBoostPolicy",
      f"rows {len(rows)}")
check("the row takes two scalars and gives one back",
      sel is not None and len(rows) > int(sel.group(1)) and re.sub(r"\s", "", rows[int(sel.group(1))][1]) == "2,0,1,0")
body = re.search(r"IOReturn NVRMUserClient::sBoostPolicy\(.*?\n\}\n", k, re.S)
b = body.group(0) if body else ""
setpart = b[b.find("NVRM_BP_OP_SET"):b.find("__atomic_exchange_n")] if "__atomic_exchange_n" in b else ""
check("the kext defines sBoostPolicy", body is not None)
check("setting the policy checks for root on the caller's thread before it writes",
      "kauth_cred_issuser(kauth_cred_get())" in setpart and "kIOReturnNotPrivileged" in setpart)
# a gate or an RM call would make the selector wait through a GPU suspend (NvXnuPmUse is the power gate, Gate the
# connection's): the behaviour arms below count the stand-ins too, this catches a form they would not compile
gate = re.findall(r"rm_\w+|\bGate\b|fGate|IOLock\w*|IOCommandGate|fPowerGate|nvPmGate|NvXnuPmUse|nvState\(|IOSleep", b)
check("the selector's text makes no RM call and takes no gate", body is not None and not gate, ", ".join(gate))
check("the policy and its change count are read-only sysctls",
      re.search(r"SYSCTL_INT\(_debug, OID_AUTO, nvrm_boost_policy, CTLFLAG_RD \| CTLFLAG_LOCKED,", k) is not None
      and re.search(r"SYSCTL_QUAD\(_debug, OID_AUTO, nvrm_boost_policy_sets, CTLFLAG_RD \| CTLFLAG_LOCKED,", k) is not None
      and "sysctl_register_oid(&sysctl__debug_nvrm_boost_policy)" in k
      and "sysctl_register_oid(&sysctl__debug_nvrm_boost_policy_sets)" in k)
kglobals = re.findall(r"^static (?:volatile )?\w+ gNvBoostPolicy\w* = [^;\n]+;$", k, re.M)
check("the kext starts undecided", "static int gNvBoostPolicy = NVRM_BP_UNDECIDED;" in kglobals)
check("the kext defines its change counter", any("gNvBoostPolicySets" in g for g in kglobals), str(kglobals))

# --- text arms on NVK's side ---------------------------------------------------------------------------------------
check("the device's NVRM_610_BOOST comes from the patch's own reader", "int bv = nvkmd_nvrm_boost_conf();" in added
      and not re.search(r'int bv = nvrm610_value\("BOOST"\)', added))
confm = re.search(r"static int\nnvkmd_nvrm_boost_conf\(void\)\n\{.*?\n\}\n", added, re.S)
check("the patch defines that reader", confm is not None)
conffn = confm.group(0) if confm else ""
check("the reader prints nothing", "printf" not in conffn and "nvmtl_cfg_getenv" not in conffn and "nvrm610_value" not in conffn)
check("a refusal no longer switches the process's boost off for good",
      not any("boostMode, NVKMD_NVRM_BOOST_OFF" in l for l in plus) and "NVKMD_NVRM_BOOST_RETRY_NS" in added)
unguarded = []
post = [(l[:1] == "+", l[1:]) for l in ptext.split("\n") if l[:1] in (" ", "+") and not l.startswith("+++")]
for i, (new, l) in enumerate(post):
    if new and "fprintf(stderr" in l:   # a print the patch adds, with the line before it as it reads after the patch
        if "nvkmd_nvrm_diag_on()" not in post[i - 1][1] + l:
            unguarded.append(l.strip()[:60])
check("every line it prints is behind nvkmd_nvrm_diag_on()", not unguarded, "; ".join(unguarded))

fields = re.findall(r"^\s*((?:int|uint64_t) boost\w+;)", section("src/nouveau/vulkan/nvkmd/nvrm/nvkmd_nvrm.h"), re.M)
check("the device struct has the boost fields the renewal uses, boostBusy among them",
      "int boostBusy;" in fields and "int boostLive;" in fields, str(fields))
m = re.search(r"#define NVKMD_NVRM_BOOST_LEASE_S.*?\nnvkmd_nvrm_boost_burst\(struct nvkmd_nvrm_pdev \*pdev\)\n\{.*?\n\}\n", added, re.S)
func = m.group(0) if m else ""
check("the patch defines the renewal, the policy read, the clear and the request",
      all(f in func for f in ("nvkmd_nvrm_boost_shape(struct", "nvkmd_nvrm_boost_clear(struct", "nvkmd_nvrm_boost_apply(struct")))
pdev_c = section("src/nouveau/vulkan/nvkmd/nvrm/nvkmd_nvrm_pdev.c")
cm = re.search(r"\n(   /\* Nothing asks the GPU to raise its memory clock.*?)\n   /\* Power cap\.", pdev_c, re.S)
createblk = cm.group(1) if cm else ""
check("the create path's boost block can be cut out of the patch", "nvkmd_nvrm_boost_shape(pdev)" in createblk)
dm = re.search(r"\nnvkmd_nvrm_pdev_destroy\(struct nvkmd_pdev \*_pdev\)\n\{.*?nvkmd_nvrm_dev_api_ctl\(pdev, &rm\);\n(.*?)\n\s*nvRmApiFree\(&rm, pdev->hVaSpace\);",
               pdev_c, re.S)
destroyblk = dm.group(1) if dm else ""
check("the destroy's boost clear can be cut out of the patch", "boostLive" in destroyblk, destroyblk[:120])
xm = re.search(r"\nint nvrm_xnu_boost_policy\(int fd, uint32_t op, uint32_t value, uint64_t \*policy\)\n\{.*?\n\}\n",
               section("src/nouveau/vulkan/nvkmd/nvrm/nvrm_xnu.c"), re.S)
xnufn = xm.group(0) if xm else ""
check("the patch defines nvrm_xnu_boost_policy", bool(xnufn))
xdefs = re.findall(r"^#define NVRM_XNU_(?:NO_METHOD|REFUSED)\s+\d+$", section("src/nouveau/vulkan/nvkmd/nvrm/nvrm_xnu.h"), re.M)
check("nvrm_xnu.h names the two answers that are not a policy", len(xdefs) == 2, str(xdefs))
if not (func and createblk and destroyblk and xnufn and len(xdefs) == 2 and fields and body and kglobals):
    print(f"{fails} failed")
    sys.exit(1)

# --- the kext's selector, compiled from NVRM.cpp -----------------------------------------------------------------------
KHARNESS = r'''
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "nvrm_boost_policy_abi.h"
typedef int IOReturn; typedef int64_t SInt64;
#define kIOReturnSuccess      0
#define kIOReturnNotPrivileged ((IOReturn)0xe00002c1)
#define kIOReturnBadArgument   ((IOReturn)0xe00002c2)
#define kIOReturnNotAttached   ((IOReturn)0xe00002d8)
struct IOExternalMethodArguments { uint32_t version; uint32_t selector; const uint64_t *scalarInput;
                                   uint32_t scalarInputCount; uint64_t *scalarOutput; uint32_t scalarOutputCount; };
static int g_root, g_logs, g_gates, g_incs, g_creds; static char g_log[256];
struct ucred { int uid; }; typedef struct ucred *kauth_cred_t;
static struct ucred g_cred;
static kauth_cred_t kauth_cred_get(void) { g_creds++; g_cred.uid = g_root ? 0 : 501; return &g_cred; }
static int kauth_cred_issuser(kauth_cred_t c) { return c == &g_cred && c->uid == 0; }
static int proc_selfpid(void) { return 4242; }
static SInt64 OSIncrementAtomic64(volatile SInt64 *p) { g_incs++; return __atomic_fetch_add(p, 1, __ATOMIC_SEQ_CST); }
#define LOG(fmt, ...) do { g_logs++; snprintf(g_log, sizeof g_log, fmt, ##__VA_ARGS__); } while (0)
/* the gates a selector must not take: each counts when made */
struct NvXnuPmUse { template <class... A> NvXnuPmUse(A...) { g_gates++; } ~NvXnuPmUse() {} };
struct Gate { template <class... A> Gate(A...) { g_gates++; } ~Gate() {} };
static void IOLockLock(void *) { g_gates++; }
static void IOLockUnlock(void *) {}
class OSObject { public: virtual ~OSObject() {} };
class NVRMUserClient : public OSObject {
public:
    void *fGate = nullptr; void *fLock = nullptr; int fOwner = 0;
    static IOReturn sBoostPolicy(OSObject *target, void *reference, IOExternalMethodArguments *args);
};
#define OSDynamicCast(type, inst) dynamic_cast<type *>(inst)
@GLOBALS@
@BODY@
static NVRMUserClient g_uc; static OSObject g_other;
static void call(const char *tag, OSObject *t, int asRoot, uint64_t op, uint64_t value)
{
    uint64_t in[2] = { op, value }, out[1] = { 0xdeadull };
    IOExternalMethodArguments a; memset(&a, 0, sizeof a);
    a.selector = NVRM_BOOST_POLICY_SELECTOR; a.scalarInput = in; a.scalarInputCount = 2; a.scalarOutput = out; a.scalarOutputCount = 1;
    g_root = asRoot; g_log[0] = 0;
    const IOReturn rc = NVRMUserClient::sBoostPolicy(t, nullptr, &a);
    printf("k %s %u %d %llu %lld %d %d %d|%s\n", tag, (unsigned)rc, gNvBoostPolicy, (unsigned long long)out[0],
           (long long)gNvBoostPolicySets, g_incs, g_logs, g_gates, g_log);
}
int main()
{
    setvbuf(stdout, NULL, _IONBF, 0);   /* each answer is printed even if a later call crashes */
    call("read0", &g_uc, 0, NVRM_BP_OP_READ, 0);
    call("set_idle", &g_uc, 1, NVRM_BP_OP_SET, NVRM_BP_IDLE);
    call("set_idle_again", &g_uc, 1, NVRM_BP_OP_SET, NVRM_BP_IDLE);
    call("set_normal", &g_uc, 1, NVRM_BP_OP_SET, NVRM_BP_NORMAL);
    call("user_set", &g_uc, 0, NVRM_BP_OP_SET, NVRM_BP_UNDECIDED);
    call("user_set_same", &g_uc, 0, NVRM_BP_OP_SET, NVRM_BP_NORMAL);
    call("range3", &g_uc, 1, NVRM_BP_OP_SET, NVRM_BP_COUNT);
    call("range_wrap", &g_uc, 1, NVRM_BP_OP_SET, 0x100000001ull);
    call("range_huge", &g_uc, 1, NVRM_BP_OP_SET, ~0ull);
    call("user_read1", &g_uc, 0, NVRM_BP_OP_READ, NVRM_BP_IDLE);
    call("root_read0", &g_uc, 1, NVRM_BP_OP_READ, NVRM_BP_UNDECIDED);
    call("badop", &g_uc, 1, 2, NVRM_BP_IDLE);
    call("notattached", &g_other, 1, NVRM_BP_OP_SET, NVRM_BP_IDLE);
    call("set_undecided", &g_uc, 1, NVRM_BP_OP_SET, NVRM_BP_UNDECIDED);
    return 0;
}
'''

# --- NVK's side, compiled from the patch ---------------------------------------------------------------------------
HARNESS = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <sched.h>
#include <stdbool.h>
#include "nvrm_boost_policy_abi.h"
typedef uint32_t NV_STATUS; typedef uint32_t NvU32; typedef uint32_t NvHandle;
#define NV_OK 0
#define NV2080_CTRL_PERF_BOOST_FLAGS_CMD_CLEAR 0
#define NV2080_CTRL_PERF_BOOST_FLAGS_CMD_BOOST_TO_MAX 2
#define NV2080_CTRL_PERF_BOOST_FLAGS_CUDA_YES 1
#define NV2080_CTRL_PERF_BOOST_DURATION_INFINITE 0xffffffffu
#define NV2080_CTRL_CMD_PERF_BOOST 0x2080200a
@XDEFS@
typedef int kern_return_t; typedef unsigned io_connect_t;
#define KERN_SUCCESS 0
#define kIOReturnNotPrivileged ((kern_return_t)0xe00002c1)
#define kIOReturnBadArgument   ((kern_return_t)0xe00002c2)
#define kIOReturnUnsupported   ((kern_return_t)0xe00002c7)
#define kIOReturnNotResponding ((kern_return_t)0xe00002ed)
typedef struct { NvU32 flags; NvU32 duration; } NV2080_CTRL_PERF_BOOST_PARAMS;
struct NvRmApi { int fd; NvHandle hClient; const char *nodeName; };
enum { NVKMD_NVRM_BOOST_OFF, NVKMD_NVRM_BOOST_BURST, NVKMD_NVRM_BOOST_HOLD };
struct nvkmd_nvrm_pdev { @FIELDS@
                         NvHandle hSubdevice, hClient; int ctlFd; char *devName; };
static struct nvkmd_nvrm_pdev P;
static const uint64_t S = 1000000000ull, MS = 1000000ull;
static uint64_t g_now;
static uint64_t os_time_get_nano(void) { return __atomic_load_n(&g_now, __ATOMIC_SEQ_CST); }
static bool nvkmd_nvrm_diag_on(void) { return false; }
static void nvkmd_nvrm_dev_api_ctl(struct nvkmd_nvrm_pdev *p, struct NvRmApi *rm) { rm->fd = p->ctlFd; rm->hClient = p->hClient; }
void nvkmd_nvrm_boost_burst(struct nvkmd_nvrm_pdev *pdev);
static int g_calls, g_bad, g_asks, g_sim = 1; static NV_STATUS g_rc; static uint64_t g_policy; static kern_return_t g_kr;
static int g_leases, g_clears, g_cclears, g_holds, g_seq[64], g_nseq;
static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
/* a second renewal from inside the RM call: -2 off, -1 the kext's answer unchanged, else the policy it answers then */
static int g_reenter = -2, g_in_hook, g_inner_asks, g_inner_calls, g_inner_busy, g_inner_same;
static NV_STATUS nvRmApiControl(struct NvRmApi *rm, NvHandle h, NvU32 cmd, void *p, NvU32 n)
{
    NV2080_CTRL_PERF_BOOST_PARAMS *bp = p;
    pthread_mutex_lock(&g_mu);
    g_calls++;
    if (g_nseq < 64) g_seq[g_nseq++] = (int)bp->flags;
    if (cmd != NV2080_CTRL_CMD_PERF_BOOST || n != sizeof *bp || h != P.hSubdevice || rm->fd != P.ctlFd) g_bad++;
    if (bp->flags == 0 && bp->duration == 0) g_clears++;
    else if (bp->flags == 0x10 && bp->duration == 0) g_cclears++;
    else if (bp->flags == 2 && bp->duration == 3) g_leases++;
    else if (bp->flags == 0x12 && bp->duration == 0xffffffffu) g_holds++;
    else g_bad++;
    const NV_STATUS rc = g_rc;
    const int hook = g_reenter != -2 && !g_in_hook;
    pthread_mutex_unlock(&g_mu);
    if (hook) {   /* the RM call outlasts the deadline: 2 s later another submission renews */
        g_in_hook = 1;
        const int a0 = g_asks, c0 = g_calls; const struct nvkmd_nvrm_pdev was = P;
        g_now += 2 * S;
        if (g_reenter >= 0) g_policy = (uint64_t)g_reenter;
        g_inner_busy = P.boostBusy;
        nvkmd_nvrm_boost_burst(&P);
        g_inner_asks = g_asks - a0; g_inner_calls = g_calls - c0;
        g_inner_same = P.boostLive == was.boostLive && P.boostPolicy == was.boostPolicy && P.boostAsk == was.boostAsk
                       && P.boostMode == was.boostMode && P.boostBusy == was.boostBusy;
        g_reenter = -2; g_in_hook = 0;
    }
    return rc;
}
/* the kext behind selector 13: g_kr is its answer (a 3.0 kext: kIOReturnBadArgument), g_policy the policy it holds */
static unsigned g_sel, g_nin, g_noutin, g_nout = 1; static uint64_t g_in[2]; static int g_xcalls;
static io_connect_t conn_of(int fd) { return fd > 0 ? (io_connect_t)fd + 100u : 0; }
static kern_return_t IOConnectCallScalarMethod(io_connect_t c, uint32_t sel, const uint64_t *in, uint32_t nin,
                                               uint64_t *out, uint32_t *nout)
{
    pthread_mutex_lock(&g_mu);
    g_asks++; g_xcalls++; g_sel = sel; g_nin = nin; g_noutin = *nout; g_in[0] = in[0]; g_in[1] = nin > 1 ? in[1] : 99;
    if (c != (io_connect_t)P.ctlFd + 100u && g_sim) g_bad++;
    if (g_sim && (sel != NVRM_BOOST_POLICY_SELECTOR || nin != 2 || *nout != 1 || in[0] != NVRM_BP_OP_READ || in[1] != 0)) g_bad++;
    const kern_return_t kr = g_kr;
    if (kr == KERN_SUCCESS) { *out = g_policy; *nout = g_nout; }
    pthread_mutex_unlock(&g_mu);
    return kr;
}
@XNUFN@
/* a renewal that another thread completes between this one's read of the deadline and its move of it (g_interleave),
 * or a yield there (g_yield, the threads arm) */
static int g_interleave, g_yield;
static void load_hook(const volatile void *p)
{
    if (p != (const volatile void *)&P.boostRenewNs) return;
    if (g_interleave) { g_interleave = 0; nvkmd_nvrm_boost_burst(&P); }
    else if (g_yield) sched_yield();
}
#define __atomic_load_n(p, m) __extension__ ({ __typeof__(*(p)) v_ = __atomic_load_n((p), (m)); \
                                              load_hook((const volatile void *)(p)); v_; })
@CONF@
@FUNC@
static void create_part(struct nvkmd_nvrm_pdev *pdev)
{
   struct NvRmApi rm;
   nvkmd_nvrm_dev_api_ctl(pdev, &rm);
@CREATE@
}
static void destroy_part(struct nvkmd_nvrm_pdev *pdev)
{
   struct NvRmApi rm;
   nvkmd_nvrm_dev_api_ctl(pdev, &rm);
@DESTROY@
}
#undef __atomic_load_n
static void reset(int mode, kern_return_t kr, uint64_t policy, int live)
{
    memset(&P, 0, sizeof P); P.boostMode = mode; P.boostLive = live; P.boostPolicy = NVRM_BP_UNDECIDED;
    P.ctlFd = 7; P.hSubdevice = 0x5c000002; P.hClient = 0xc1d00001;
    g_calls = g_bad = g_asks = g_leases = g_clears = g_cclears = g_holds = g_nseq = 0; g_rc = NV_OK; g_kr = kr; g_policy = policy;
    g_now = 1000000000ull; g_reenter = -2; g_interleave = 0; g_yield = 0;
}
/* submissions every millisecond for `secs` seconds */
static void work(int secs) { for (int i = 0; i < secs * 1000; i++) { g_now += MS; nvkmd_nvrm_boost_burst(&P); } }
/* SHOW: leases clears holds asks bad live busy cudaclears */
#define SHOW(tag) printf("%s %d %d %d %d %d %d %d %d\n", tag, g_leases, g_clears, g_holds, g_asks, g_bad, P.boostLive, P.boostBusy, g_cclears)
static void SEQ(const char *tag) { printf("seq %s %d", tag, g_nseq); for (int i = 0; i < g_nseq; i++) printf(" %d", g_seq[i]); printf("\n"); }
/* threads: each round starts at a barrier after the clock was moved onto the deadline */
enum { NT = 8, ROUNDS = 100, CALLS = 20 };
static int g_bar_n, g_bar_gen;
static void barrier(void)
{
    const int gen = __atomic_load_n(&g_bar_gen, __ATOMIC_ACQUIRE);
    if (__atomic_add_fetch(&g_bar_n, 1, __ATOMIC_ACQ_REL) == NT) {
        __atomic_store_n(&g_bar_n, 0, __ATOMIC_RELAXED);
        __atomic_store_n(&g_bar_gen, gen + 1, __ATOMIC_RELEASE);
    } else
        while (__atomic_load_n(&g_bar_gen, __ATOMIC_ACQUIRE) == gen) sched_yield();
}
static void *hammer(void *a)
{
    const int me = (int)(intptr_t)a;
    for (int r = 0; r < ROUNDS; r++) {
        barrier();
        if (me == 0) __atomic_store_n(&g_now, g_now + S, __ATOMIC_SEQ_CST);
        barrier();
        for (int i = 0; i < CALLS; i++) nvkmd_nvrm_boost_burst(&P);
    }
    return a;
}
static void create(const char *tag, const char *env, kern_return_t kr, uint64_t policy, NV_STATUS rc)
{
    reset(NVKMD_NVRM_BOOST_BURST, kr, policy, NVKMD_NVRM_BOOST_OFF);
    P.boostLive = 77; P.boostAsk = 77; P.boostBusy = 77; P.boostPolicy = 77;   /* create sets each of them */
    if (env) setenv("NVRM_610_BOOST", env, 1); else unsetenv("NVRM_610_BOOST");
    g_rc = rc;
    create_part(&P);
    printf("c %s %d %d %d %d %llu %llu %d %d %d\n", tag, P.boostMode, P.boostLive, P.boostAsk, P.boostBusy,
           (unsigned long long)P.boostPolicy, (unsigned long long)P.boostRenewNs, g_holds, g_calls, g_asks);
    SEQ(tag);
}
static void xcall(const char *tag, int fd, uint32_t op, uint32_t value, kern_return_t kr, unsigned nout)
{
    g_sim = 0; g_kr = kr; g_nout = nout; g_policy = 2; g_xcalls = 0; g_sel = g_nin = g_noutin = 0; g_in[0] = g_in[1] = 99;
    uint64_t pol = 77;
    const int r = nvrm_xnu_boost_policy(fd, op, value, &pol);
    printf("x %s %d %llu %d %u %u %u %llu %llu\n", tag, r, (unsigned long long)pol, g_xcalls, g_sel, g_nin, g_noutin,
           (unsigned long long)g_in[0], (unsigned long long)g_in[1]);
    g_sim = 1; g_nout = 1;
}
int sim_main(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    unsetenv("NVRM_610_BOOST");
    /* a 3.0 kext (selector 13 past its table): the policy is asked once, the process boosts per burst as in 3.0 */
    reset(NVKMD_NVRM_BOOST_BURST, kIOReturnBadArgument, 0, 0); work(10); SHOW("oldkext");
    /* undecided: as 3.0 */
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_UNDECIDED, 0); work(10); SHOW("undecided");
    /* idle mode from the start: nothing asked */
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_IDLE, 0); work(10); SHOW("idle");
    /* normal for 5 s, idle mode switched on: one clear within a second, then nothing; normal again: leases again */
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_NORMAL, 0); work(5); SHOW("on5");
    g_policy = NVRM_BP_IDLE; uint64_t t0 = g_now; int c0 = g_clears; g_nseq = 0;
    while (g_clears == c0 && g_now < t0 + 5 * S) { g_now += MS; nvkmd_nvrm_boost_burst(&P); }
    printf("clear_after_ms %llu\n", (unsigned long long)((g_now - t0) / MS));
    SEQ("leaseidle");
    work(10); SHOW("idle10");
    g_policy = NVRM_BP_NORMAL; t0 = g_now; int l0 = g_leases;
    while (g_leases == l0 && g_now < t0 + 5 * S) { g_now += MS; nvkmd_nvrm_boost_burst(&P); }
    printf("lease_after_ms %llu\n", (unsigned long long)((g_now - t0) / MS));
    /* started with NVRM_610_BOOST=0 (cold idle mode); the policy says normal: it boosts */
    reset(NVKMD_NVRM_BOOST_OFF, 0, NVRM_BP_NORMAL, 0); work(10); SHOW("conf0normal");
    reset(NVKMD_NVRM_BOOST_OFF, 0, NVRM_BP_UNDECIDED, 0); work(10); SHOW("conf0undecided");
    /* hold (asked at create, so live): nothing per submission; idle mode clears it once, with the CUDA bit and then
     * without; normal asks it once again */
    reset(NVKMD_NVRM_BOOST_HOLD, 0, NVRM_BP_UNDECIDED, NVKMD_NVRM_BOOST_HOLD); work(5); SHOW("hold");
    g_policy = NVRM_BP_IDLE; g_nseq = 0; work(5); SHOW("holdidle"); SEQ("holdidle");
    g_policy = NVRM_BP_NORMAL; work(5); SHOW("holdnormal");
    /* the RM refuses the lease: asked again 30 s later, not every second and not never */
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_UNDECIDED, 0); g_rc = 0x56;
    for (int i = 0; i < 31; i++) { g_now += 2 * S; nvkmd_nvrm_boost_burst(&P); }
    printf("refused %d %d %d\n", g_calls, P.boostLive, P.boostBusy);
    /* a call to the kext that fails after it answered idle mode keeps idle mode */
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_IDLE, 0); work(2); g_kr = kIOReturnNotResponding; work(5); SHOW("flaky");
    /* a second renewal 2 s later, from inside the first one's RM call */
    reset(NVKMD_NVRM_BOOST_HOLD, 0, NVRM_BP_NORMAL, 0); g_reenter = -1; nvkmd_nvrm_boost_burst(&P);
    SHOW("rehold"); printf("inner_rehold %d %d %d %d\n", g_inner_asks, g_inner_calls, g_inner_busy, g_inner_same);
    reset(NVKMD_NVRM_BOOST_HOLD, 0, NVRM_BP_NORMAL, 0); g_reenter = NVRM_BP_IDLE; nvkmd_nvrm_boost_burst(&P);
    SHOW("reidle"); printf("inner_reidle %d %d %d %d\n", g_inner_asks, g_inner_calls, g_inner_busy, g_inner_same);
    g_nseq = 0; work(3); SHOW("reidle3"); SEQ("reidle3");
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_NORMAL, 0); g_reenter = -1; nvkmd_nvrm_boost_burst(&P);
    SHOW("release"); printf("inner_release %d %d %d %d\n", g_inner_asks, g_inner_calls, g_inner_busy, g_inner_same);
    /* another renewal completes between this one's read of the deadline and its move of it */
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_NORMAL, 0); nvkmd_nvrm_boost_burst(&P);
    g_now += S; g_interleave = 1; nvkmd_nvrm_boost_burst(&P); SHOW("interleave");
    /* idle mode clears a live lease with the plain clear only */
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_IDLE, NVKMD_NVRM_BOOST_BURST); nvkmd_nvrm_boost_burst(&P); SHOW("leaseclear"); SEQ("leaseclear");
    /* the device's destroy clears what is live: a hold as it was asked and then plain, a lease plain, nothing: nothing */
    reset(NVKMD_NVRM_BOOST_HOLD, 0, 0, NVKMD_NVRM_BOOST_HOLD); destroy_part(&P); SEQ("dhold");
    reset(NVKMD_NVRM_BOOST_BURST, 0, 0, NVKMD_NVRM_BOOST_BURST); destroy_part(&P); SEQ("dlease");
    reset(NVKMD_NVRM_BOOST_HOLD, 0, 0, NVKMD_NVRM_BOOST_OFF); destroy_part(&P); SEQ("doff");
    /* the create path */
    create("chold", "hold", 0, NVRM_BP_UNDECIDED, NV_OK);
    create("cholduser", "h", 0, NVRM_BP_NORMAL, NV_OK);
    create("choldidle", "hold", 0, NVRM_BP_IDLE, NV_OK);
    create("choldold", "hold", kIOReturnBadArgument, 0, NV_OK);
    create("cburst", NULL, 0, NVRM_BP_NORMAL, NV_OK);
    create("cburst1", "1", 0, NVRM_BP_UNDECIDED, NV_OK);
    create("coff", "0", 0, NVRM_BP_NORMAL, NV_OK);
    create("crefused", "hold", 0, NVRM_BP_UNDECIDED, 0x56);
    g_rc = NV_OK; g_now += MS; nvkmd_nvrm_boost_burst(&P); SHOW("crefused_then");
    unsetenv("NVRM_610_BOOST");
    /* eight threads, each round started at a barrier with the clock moved onto the deadline: one renewal a round */
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_NORMAL, 0); g_now = 0; g_yield = 1;
    pthread_t t[NT];
    for (int i = 0; i < NT; i++) pthread_create(&t[i], 0, hammer, (void *)(intptr_t)i);
    for (int i = 0; i < NT; i++) pthread_join(t[i], 0);
    g_yield = 0; SHOW("threads");
    /* every pair of policy and NVRM_610_BOOST */
    for (int pol = 0; pol < 4; pol++) for (int conf = 0; conf < 3; conf++) printf("shape %d %d %d\n", pol, conf, nvrm_bp_shape((uint64_t)pol, conf));
    /* the real nvrm_xnu_boost_policy against the simulated kext */
    xcall("read_ok", 7, NVRM_BP_OP_READ, 0, KERN_SUCCESS, 1);
    xcall("set_ok", 7, NVRM_BP_OP_SET, 1, KERN_SUCCESS, 1);
    xcall("read_old", 7, NVRM_BP_OP_READ, 0, kIOReturnBadArgument, 1);
    xcall("set_old", 7, NVRM_BP_OP_SET, 1, kIOReturnBadArgument, 1);
    xcall("set_user", 7, NVRM_BP_OP_SET, 1, kIOReturnNotPrivileged, 1);
    xcall("read_unsup", 7, NVRM_BP_OP_READ, 0, kIOReturnUnsupported, 1);
    xcall("read_fail", 7, NVRM_BP_OP_READ, 0, kIOReturnNotResponding, 1);
    xcall("read_short", 7, NVRM_BP_OP_READ, 0, KERN_SUCCESS, 0);
    xcall("read_noconn", -1, NVRM_BP_OP_READ, 0, KERN_SUCCESS, 1);
    return 0;
}
int conf_main(void) { int v = nvkmd_nvrm_boost_conf(); printf("conf %d\n", v); return 0; }
int main(int c, char **v) { (void)v; return c > 1 ? conf_main() : sim_main(); }
'''
CONF_CASES = [   # (name, environment value or None, file text or None, the value NVK must take)
    ("no file and no variable: not set (per burst)", None, None, -1),
    ("NVMetrics' line, NVRM_610_BOOST=0, is seen without the Metal plugin", None,
     "# shipped\nNVRM_ASYNC=1\n# --- my settings, kept on update ---\nNVRM_610_BOOST=0\n# --- end my settings ---\n", ord("0")),
    ("the last full-name line wins", None, "NVRM_610_BOOST=0\nNVRM_610_BOOST=1\n", ord("1")),
    ("the full name hides 2.9's short line, wherever each sits", None, "NVRM_610_BOOST=h\nBOOST=0\n", ord("h")),
    ("2.9's short BOOST= line still works on its own", None, "NVK_DIAG=0\nBOOST=0\n", ord("0")),
    ("the short line is found after a full name elsewhere in a comment", None, "# NVRM_610_BOOST=1 was here\nBOOST=0\n", ord("0")),
    ("a commented line does not count", None, "# NVRM_610_BOOST=0\n", -1),
    ("a name that only ends in BOOST is not it", None, "NVK_XBOOST=0\nMYBOOST=0\n", -1),
    ("the environment beats the file", "1", "NVRM_610_BOOST=0\n", ord("1")),
    ("a last line without a newline is read", None, "NVK_DIAG=0\nNVRM_610_BOOST=0", ord("0")),
]

cc, cxx = shutil.which("cc"), shutil.which("c++")
if not cc or not cxx:
    print("SKIP: no C compiler on PATH: the renewal code was not run" if not cc else
          "SKIP: no C++ compiler on PATH: the kext's selector was not run")
    sys.exit(2 if not fails else 1)
with tempfile.TemporaryDirectory() as d:
    d = pathlib.Path(d)
    # the kext's selector
    (d / "k.cpp").write_text(KHARNESS.replace("@GLOBALS@", "\n".join(kglobals)).replace("@BODY@", b))
    r = subprocess.run([cxx, "-std=c++17", "-O1", "-Wall", "-Wno-unused-function", "-I", str(abi.parent),
                        "-o", str(d / "k"), str(d / "k.cpp")], capture_output=True, text=True, errors="replace")
    check("the kext's sBoostPolicy compiles against the stand-ins", r.returncode == 0, r.stderr.strip()[-400:])
    if r.returncode == 0:
        kv, klog = {}, {}
        o = subprocess.run([str(d / "k")], capture_output=True, text=True, errors="replace", timeout=60)
        check("the kext's selector runs through every call", o.returncode == 0 and "k set_undecided " in o.stdout,
              f"exit {o.returncode}, last line {o.stdout.strip().split(chr(10))[-1][:80]!r}")
        for l in o.stdout.strip().split("\n"):
            head, _, logtext = l.partition("|")
            f = head.split()
            if len(f) == 9 and f[0] == "k":
                kv[f[1]] = [int(x) for x in f[2:]]
                klog[f[1]] = logtext
        kv = collections.defaultdict(lambda: [-9] * 7, kv)
        klog = collections.defaultdict(str, klog)
        OK, PRIV, BAD, DETACHED = 0, 0xe00002c1, 0xe00002c2, 0xe00002d8
        # k: rc policy out sets increments logs gates
        rd = kv["read0"]
        check("the kext's policy starts undecided and READ answers it", rd[:3] == [OK, 0, 0], str(rd))
        si = kv["set_idle"]
        check("root sets idle mode: accepted, the answer is the new policy", si[:3] == [OK, 1, 1], str(si))
        check("a change counts once and is logged once", si[3:6] == [1, 1, 1], str(si))
        check("the log line names the change and the caller",
              klog["set_idle"] == "boost policy undecided -> idle mode (pid 4242)", klog["set_idle"])
        sa = kv["set_idle_again"]
        check("setting the value it already has is accepted and neither counts nor logs",
              sa[:3] == [OK, 1, 1] and sa[3:6] == [1, 1, 1], str(sa))
        sn = kv["set_normal"]
        check("the next change counts again", sn[:3] == [OK, 2, 2] and sn[3:6] == [2, 2, 2], str(sn))
        us = kv["user_set"]
        check("a process that is not root cannot set the policy (kIOReturnNotPrivileged, nothing changes)",
              us[0] == PRIV and us[1] == 2 and us[2] == 0xdead and us[3] == 2, str(us))
        check("a process that is not root cannot set even the value it already has",
              kv["user_set_same"][0] == PRIV and kv["user_set_same"][3] == 2, str(kv["user_set_same"]))
        for tag, what in (("range3", "3"), ("range_wrap", "2^32+1 (1 as an int)"), ("range_huge", "2^64-1")):
            x = kv[tag]
            check(f"root's value {what} is refused (kIOReturnBadArgument) and the policy stays",
                  x[0] == BAD and x[1] == 2 and x[2] == 0xdead and x[3] == 2, str(x))
        ur = kv["user_read1"]
        check("any process reads the policy, and a READ with a value writes nothing",
              ur[:3] == [OK, 2, 2] and ur[3] == 2, str(ur))
        check("root's READ with a value writes nothing either", kv["root_read0"][:3] == [OK, 2, 2], str(kv["root_read0"]))
        bo = kv["badop"]
        check("an op that is neither READ nor SET is refused and writes nothing", bo[0] == BAD and bo[1] == 2 and bo[2] == 0xdead, str(bo))
        na = kv["notattached"]
        check("a target that is not an NVRM user client is refused", na[0] == DETACHED and na[1] == 2, str(na))
        su = kv["set_undecided"]
        check("root can set undecided again", su[:3] == [OK, 0, 0] and su[3] == 3, str(su))
        check("the counter moves only through OSIncrementAtomic64", len(kv) == 14 and all(x[3] == x[4] for x in kv.values()), str(kv))
        check("the selector takes neither the power gate nor the connection's gate nor a lock",
              len(kv) == 14 and all(x[6] == 0 for x in kv.values()), str({t: x[6] for t, x in kv.items() if x[6]}))

    # NVK's side
    conf = d / "nvrm610.conf"
    src = (HARNESS.replace("@XDEFS@", "\n".join(xdefs)).replace("@FIELDS@", " ".join(fields))
           .replace("@XNUFN@", xnufn).replace("@CONF@", conffn).replace("@FUNC@", func)
           .replace("@CREATE@", createblk).replace("@DESTROY@", destroyblk))
    (d / "h.c").write_text(src)
    r = subprocess.run([cc, "-O1", "-Wall", "-Wno-unused-function", "-I", str(abi.parent), f'-DNVMTL_CFG_PATH="{conf}"',
                        "-o", str(d / "h"), str(d / "h.c"), "-lpthread"], capture_output=True, text=True, errors="replace")
    check("the patch's code compiles", r.returncode == 0, r.stderr.strip()[-400:])
    if r.returncode == 0:
        for name, envv, text, want in CONF_CASES:
            if text is None:
                conf.unlink(missing_ok=True)
            else:
                conf.write_text(text)
            env = {k: x for k, x in os.environ.items() if k != "NVRM_610_BOOST"}
            if envv is not None:
                env["NVRM_610_BOOST"] = envv
            o = subprocess.run([str(d / "h"), "conf"], capture_output=True, text=True, errors="replace", timeout=30, env=env)
            got = int(o.stdout.split()[1]) if o.stdout.startswith("conf ") else None
            check("conf: " + name, got == want and o.stderr == "", f"got {got}, want {want}, stderr {o.stderr!r}")
        conf.unlink(missing_ok=True)
        o = subprocess.run([str(d / "h")], capture_output=True, text=True, errors="replace", timeout=120)
        check("the simulation runs to its end", o.returncode == 0 and "x read_noconn" in o.stdout,
              f"exit {o.returncode} {o.stderr[-200:]}")
        v, shapes, seq, cr, xr = {}, {}, {}, {}, {}
        for l in o.stdout.strip().split("\n"):
            f = l.split()
            if not f:
                continue
            if f[0] == "shape":
                shapes[(int(f[1]), int(f[2]))] = int(f[3])
            elif f[0] == "seq":
                seq[f[1]] = [int(x) for x in f[3:]]
            elif f[0] == "c":
                cr[f[1]] = [int(x) for x in f[2:]]
            elif f[0] == "x":
                xr[f[1]] = [int(x) for x in f[2:]]
            else:
                v[f[0]] = [int(x) for x in f[1:]]
        g = lambda t: v.get(t, [-9] * 8)
        # SHOW: leases clears holds asks bad live busy cudaclears
        o_ = g("oldkext")
        check("with a 3.0 kext the policy is asked once and the process boosts per burst as before",
              10 <= o_[0] <= 11 and o_[1] == 0 and o_[3] == 1 and o_[4] == 0, str(o_))
        u = g("undecided")
        check("undecided: per burst, as in 3.0 (10 or 11 leases in 10 s)", 10 <= u[0] <= 11 and u[1] == 0 and u[4] == 0, str(u))
        check("undecided: the policy is read once per renewal, not per submission", u[3] == u[0], str(u))
        i = g("idle")
        check("idle mode from the start asks the GPU for nothing", i[0] == 0 and i[1] == 0 and i[2] == 0, str(i))
        check("switching idle mode on clears this process's lease within a second", g("clear_after_ms")[0] <= 1000,
              f"{g('clear_after_ms')[0]} ms")
        i10 = g("idle10")
        check("then nothing more is asked or cleared", i10[1] == 1 and i10[0] == g("on5")[0] and i10[5] == 0, str(i10))
        check("switching it off again asks for a lease within a second", g("lease_after_ms")[0] <= 1000,
              f"{g('lease_after_ms')[0]} ms")
        c = g("conf0normal")
        check("an app started with NVRM_610_BOOST=0 boosts once the policy says normal", 10 <= c[0] <= 11, str(c))
        check("with NVRM_610_BOOST=0 and no policy it asks for nothing", g("conf0undecided")[:3] == [0, 0, 0], str(g("conf0undecided")))
        h = g("hold")
        check("a held boost is not asked again per submission", h[:3] == [0, 0, 0] and h[5] == 2, str(h))
        hi = g("holdidle")
        check("idle mode clears a held boost once", hi[1] == 1 and hi[7] == 1 and hi[2] == 0 and hi[5] == 0, str(hi))
        check("idle mode clears a live hold as it was asked (flags 0x10) and then plainly (0x00)",
              seq.get("holdidle") == [0x10, 0x00], str(seq.get("holdidle")))
        check("idle mode clears a live lease with the plain clear (0x00) only",
              seq.get("leaseidle") == [0x00] and seq.get("leaseclear") == [0x00] and g("leaseclear")[5] == 0,
              f"{seq.get('leaseidle')} {seq.get('leaseclear')}")
        hn = g("holdnormal")
        check("normal asks for the hold again once, never a lease", hn[2] == 1 and hn[0] == 0 and hn[5] == 2, str(hn))
        rf = g("refused")
        check("a refused lease is asked again after 30 s, not every second (3 calls in 62 s)", rf[:2] == [3, 0], str(rf))
        fl = g("flaky")
        check("a failed read of the policy keeps the last answer (idle mode stays)", fl[0] == 0 and fl[1] == 0, str(fl))
        # one renewal at a time (boostBusy)
        for tag, what in (("rehold", "a hold"), ("reidle", "a hold while idle mode is switched on"), ("release", "a lease")):
            inner = v.get("inner_" + tag, [-9] * 4)
            check(f"a renewal 2 s into the RM call of one that asks {what} reads nothing, sends nothing and changes nothing",
                  inner == [0, 0, 1, 1], f"asks {inner[0]} calls {inner[1]} busy-seen {inner[2]} same {inner[3]}")
        rh = g("rehold")
        check("the hold is asked once, not twice, and is live", rh[2] == 1 and rh[5] == 2 and rh[6] == 0, str(rh))
        check("a lease is asked once, not twice", g("release")[0] == 1, str(g("release")))
        ri3 = g("reidle3")
        check("the hold that landed as idle mode came on is cleared at the next renewal, as it was asked",
              seq.get("reidle3") == [0x10, 0x00] and ri3[5] == 0, f"{seq.get('reidle3')} {ri3}")
        busy = {t: g(t)[6] for t in ("undecided", "idle", "hold", "holdidle", "holdnormal", "flaky", "rehold", "reidle3",
                                     "release", "interleave", "leaseclear", "threads", "crefused_then")}
        busy["refused"] = rf[2] if len(rf) > 2 else -9
        check("boostBusy is 0 again after every way out of a renewal (granted, refused, nothing to do, a clear)",
              all(x == 0 for x in busy.values()), str({t: x for t, x in busy.items() if x}))
        check("a renewal that another one completed between its read of the deadline and its move of it sends nothing",
              g("interleave")[0] == 2, str(g("interleave")))
        t = g("threads")
        check("eight threads at a barrier, the clock moved onto the deadline 100 times: 100 renewals, 100 policy reads",
              t[0] == 100 and t[3] == 100 and t[4] == 0, str(t))
        # the device's destroy
        check("destroy clears a live hold as it was asked (0x10) and then plainly (0x00)", seq.get("dhold") == [0x10, 0x00],
              str(seq.get("dhold")))
        check("destroy clears a live lease plainly (0x00) only", seq.get("dlease") == [0x00], str(seq.get("dlease")))
        check("destroy sends nothing when nothing is live", seq.get("doff") == [], str(seq.get("doff")))
        # the create path; c: mode live ask busy policy renew holds calls asks
        ch = cr.get("chold", [-9] * 9)
        check("create with NVRM_610_BOOST=hold and no policy asks the hold (0x12, infinite) once and marks it live",
              ch[:4] == [2, 2, 1, 0] and ch[6:9] == [1, 1, 1] and seq.get("chold") == [0x12], f"{ch} {seq.get('chold')}")
        check("create with a hold under normal asks the hold", cr.get("cholduser", [-9] * 9)[1] == 2 and seq.get("cholduser") == [0x12],
              str(cr.get("cholduser")))
        check("create with a hold in idle mode asks nothing and has nothing live",
              cr.get("choldidle", [-9] * 9)[1:3] == [0, 1] and seq.get("choldidle") == [], str(cr.get("choldidle")))
        check("create with a hold and a 3.0 kext asks the hold as 3.0 did, and the kext not again",
              cr.get("choldold", [-9] * 9)[1:3] == [2, -1] and seq.get("choldold") == [0x12], str(cr.get("choldold")))
        check("create per burst asks nothing (the lease waits for the first submission)",
              all(cr.get(x, [9, 9])[:2] == [m_, 0] and seq.get(x) == [] for x, m_ in (("cburst", 1), ("cburst1", 1), ("coff", 0))),
              str({x: cr.get(x) for x in ("cburst", "cburst1", "coff")}))
        crf = cr.get("crefused", [-9] * 9)
        check("a hold refused at create is not live, and the first submission asks it again",
              crf[1:2] == [0] and g("crefused_then")[2] == 2 and g("crefused_then")[5] == 2, f"{crf} {g('crefused_then')}")
        check("create starts boostBusy at 0 and reads the policy once", len(cr) == 8 and all(x[3] == 0 and x[8] == 1 for x in cr.values()), str(cr))
        # the real nvrm_xnu_boost_policy; x: r policy calls sel nin noutin in0 in1
        NO, REF = (int(re.search(r"\d+$", x).group(0)) for x in xdefs)
        X = lambda t: xr.get(t, [-9] * 8)
        check("nvrm_xnu_boost_policy calls selector 13 with op and value, asks one scalar back and returns the policy",
              X("read_ok") == [0, 2, 1, 13, 2, 1, 0, 0] and X("set_ok")[:1] == [0] and X("set_ok")[6:] == [1, 1],
              f"{X('read_ok')} {X('set_ok')}")
        check("a 3.0 kext's kIOReturnBadArgument on READ is NVRM_XNU_NO_METHOD", X("read_old")[0] == NO, str(X("read_old")))
        check("kIOReturnBadArgument on SET is NVRM_XNU_REFUSED (a bad value)", X("set_old")[0] == REF, str(X("set_old")))
        check("kIOReturnNotPrivileged is NVRM_XNU_REFUSED", X("set_user")[0] == REF, str(X("set_user")))
        check("kIOReturnUnsupported is NVRM_XNU_NO_METHOD", X("read_unsup")[0] == NO, str(X("read_unsup")))
        check("another failure is -1 and leaves *policy alone", X("read_fail")[:2] == [-1, 77], str(X("read_fail")))
        check("an answer without its scalar is -1 and leaves *policy alone", X("read_short")[:2] == [-1, 77], str(X("read_short")))
        check("no connection is -1 without a call", X("read_noconn")[:3] == [-1, 77, 0], str(X("read_noconn")))
        want = {(0, 0): 0, (0, 1): 1, (0, 2): 2, (1, 0): 0, (1, 1): 0, (1, 2): 0, (2, 0): 1, (2, 1): 1, (2, 2): 2,
                (3, 0): 0, (3, 1): 1, (3, 2): 2}
        check("the shape for every policy and NVRM_610_BOOST (an unknown policy is the process's own setting)",
              shapes == want, str({k_: x for k_, x in shapes.items() if want.get(k_) != x}))
        check("no request had a shape other than a lease, a hold or a clear, and every kext call was a READ",
              v and all(x[4] == 0 for x in v.values() if len(x) == 8), str({t_: x for t_, x in v.items() if len(x) == 8 and x[4]}))

print(f"{fails} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
