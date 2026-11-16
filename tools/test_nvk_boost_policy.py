#!/usr/bin/env python3
"""Idle mode follows the kext's boost policy while apps run (nvk/nvk-boost-policy.patch).

NVK's boost lease (nvk/nvk-perf-boost-burst.patch) had its shape fixed when a process opened the device, so idle mode
reached only apps started after it was switched. Each renewal now reads NVRM.kext's policy (undecided, idle mode,
normal; kexts/NVRM/nvrm_boost_policy_abi.h) through user-client selector 13. This compiles the patch's renewal code with
a simulated clock, a simulated kext and a recording stand-in for the resource manager, runs it through switches of the
policy, and checks the kext's side of the selector in kexts/NVRM/NVRM.cpp. Linux or macOS, a C compiler, no GPU.

usage: test_nvk_boost_policy.py [driver checkout]"""
import pathlib, re, shutil, subprocess, sys, tempfile

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
# the text as it reads after the patch: context lines and added lines, not the removed ones
added = "\n".join(l[1:] for l in ptext.split("\n") if l[:1] in (" ", "+") and not l.startswith("+++"))
plus = [l[1:] for l in ptext.split("\n") if l.startswith("+") and not l.startswith("+++")]

# --- the chain and the kext ----------------------------------------------------------------------------------------
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
check("setting the policy checks for root on the caller's thread before it writes",
      "kauth_cred_issuser(kauth_cred_get())" in setpart and "kIOReturnNotPrivileged" in setpart)
check("a value past the three the policy knows is refused", "value >= NVRM_BP_COUNT" in setpart)
check("the selector makes no RM call and takes no gate",
      body is not None and not re.search(r"rm_|Gate|IOLockLock|fPowerGate|nvPmGate", b))
check("the policy and its change count are read-only sysctls",
      re.search(r"SYSCTL_INT\(_debug, OID_AUTO, nvrm_boost_policy, CTLFLAG_RD \| CTLFLAG_LOCKED,", k) is not None
      and re.search(r"SYSCTL_QUAD\(_debug, OID_AUTO, nvrm_boost_policy_sets, CTLFLAG_RD \| CTLFLAG_LOCKED,", k) is not None
      and "sysctl_register_oid(&sysctl__debug_nvrm_boost_policy)" in k
      and "sysctl_register_oid(&sysctl__debug_nvrm_boost_policy_sets)" in k)
check("the kext starts undecided", "static int gNvBoostPolicy = NVRM_BP_UNDECIDED;" in k)

# --- text arms on NVK's side ---------------------------------------------------------------------------------------
check("the device's NVRM_610_BOOST comes from the patch's own reader", "int bv = nvkmd_nvrm_boost_conf();" in added
      and not re.search(r'int bv = nvrm610_value\("BOOST"\)', added))
confm = re.search(r"static int\nnvkmd_nvrm_boost_conf\(void\)\n\{.*?\n\}\n", added, re.S)
check("the patch defines that reader", confm is not None)
conffn = confm.group(0) if confm else ""
check("the reader prints nothing", "printf" not in conffn and "nvmtl_cfg_getenv" not in conffn and "nvrm610_value" not in conffn)
check("teardown clears whatever this process still has asked for", "if (pdev->boostLive != NVKMD_NVRM_BOOST_OFF)" in added)
check("a refusal no longer switches the process's boost off for good",
      not any("boostMode, NVKMD_NVRM_BOOST_OFF" in l for l in plus) and "NVKMD_NVRM_BOOST_RETRY_NS" in added)
unguarded = []
post = [(l[:1] == "+", l[1:]) for l in ptext.split("\n") if l[:1] in (" ", "+") and not l.startswith("+++")]
for i, (new, l) in enumerate(post):
    if new and "fprintf(stderr" in l:   # a print the patch adds, with the line before it as it reads after the patch
        if "nvkmd_nvrm_diag_on()" not in post[i - 1][1] + l:
            unguarded.append(l.strip()[:60])
check("every line it prints is behind nvkmd_nvrm_diag_on()", not unguarded, "; ".join(unguarded))

m = re.search(r"#define NVKMD_NVRM_BOOST_LEASE_S.*?\nnvkmd_nvrm_boost_burst\(struct nvkmd_nvrm_pdev \*pdev\)\n\{.*?\n\}\n", added, re.S)
check("the patch defines the renewal, the policy read and the request", m is not None and "nvkmd_nvrm_boost_shape" in (m.group(0) if m else "")
      and "nvkmd_nvrm_boost_apply" in (m.group(0) if m else ""))
if not m:
    print(f"{fails} failed")
    sys.exit(1)
func = m.group(0)

# --- behaviour arms: the code against a simulated clock and kext -----------------------------------------------------
harness = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <stdbool.h>
#include "nvrm_boost_policy_abi.h"
typedef uint32_t NV_STATUS; typedef uint32_t NvU32; typedef uint32_t NvHandle;
#define NV_OK 0
#define NV2080_CTRL_PERF_BOOST_FLAGS_CMD_CLEAR 0
#define NV2080_CTRL_PERF_BOOST_FLAGS_CMD_BOOST_TO_MAX 2
#define NV2080_CTRL_PERF_BOOST_FLAGS_CUDA_YES 1
#define NV2080_CTRL_PERF_BOOST_DURATION_INFINITE 0xffffffffu
#define NV2080_CTRL_CMD_PERF_BOOST 0x2080200a
#define NVRM_XNU_NO_METHOD 1
#define NVRM_XNU_REFUSED 2
typedef struct { NvU32 flags; NvU32 duration; } NV2080_CTRL_PERF_BOOST_PARAMS;
struct NvRmApi { int fd; NvHandle hClient; const char *nodeName; };
enum { NVKMD_NVRM_BOOST_OFF, NVKMD_NVRM_BOOST_BURST, NVKMD_NVRM_BOOST_HOLD };
struct nvkmd_nvrm_pdev { int boostMode; uint64_t boostRenewNs; int boostLive; int boostAsk; uint64_t boostPolicy;
                         NvHandle hSubdevice, hClient; int ctlFd; char *devName; };
static uint64_t g_now;
static uint64_t os_time_get_nano(void) { return g_now; }
static bool nvkmd_nvrm_diag_on(void) { return false; }
static void nvkmd_nvrm_dev_api_ctl(struct nvkmd_nvrm_pdev *p, struct NvRmApi *rm) { rm->fd = p->ctlFd; rm->hClient = p->hClient; }
static int g_calls, g_bad, g_asks, g_kr; static unsigned g_flags, g_dur; static NV_STATUS g_rc; static uint64_t g_policy;
static int g_leases, g_clears, g_holds;
static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static NV_STATUS nvRmApiControl(struct NvRmApi *rm, NvHandle h, NvU32 cmd, void *p, NvU32 n)
{
    NV2080_CTRL_PERF_BOOST_PARAMS *bp = p;
    pthread_mutex_lock(&g_mu);
    g_calls++; g_flags = bp->flags; g_dur = bp->duration;
    if (cmd != NV2080_CTRL_CMD_PERF_BOOST || n != sizeof *bp) g_bad++;
    if (bp->flags == 0) g_clears++;
    else if (bp->flags == 2 && bp->duration == 3) g_leases++;
    else if (bp->flags == 0x12 && bp->duration == 0xffffffffu) g_holds++;
    else g_bad++;
    pthread_mutex_unlock(&g_mu);
    return g_rc;
}
/* the kext: g_kr 0 answers g_policy, NVRM_XNU_NO_METHOD is a 3.0 kext, -1 a failed call */
static int nvrm_xnu_boost_policy(int fd, uint32_t op, uint32_t value, uint64_t *policy)
{
    pthread_mutex_lock(&g_mu); g_asks++; pthread_mutex_unlock(&g_mu);
    if (op != NVRM_BP_OP_READ || value != 0) { g_bad++; return -1; }
    if (g_kr) return g_kr;
    *policy = g_policy;
    return 0;
}
''' + conffn + func + r'''
static struct nvkmd_nvrm_pdev P;
static const uint64_t S = 1000000000ull, MS = 1000000ull;
static void reset(int mode, int kr, uint64_t policy, int live)
{
    memset(&P, 0, sizeof P); P.boostMode = mode; P.boostLive = live; P.boostPolicy = NVRM_BP_UNDECIDED;
    g_calls = g_bad = g_asks = g_leases = g_clears = g_holds = 0; g_rc = NV_OK; g_kr = kr; g_policy = policy;
    g_now = 1000000000ull;
}
/* submissions every millisecond for `secs` seconds */
static void work(int secs) { for (int i = 0; i < secs * 1000; i++) { g_now += MS; nvkmd_nvrm_boost_burst(&P); } }
static void *hammer(void *a) { for (int i = 0; i < 20000; i++) nvkmd_nvrm_boost_burst(&P); return a; }
#define SHOW(tag) printf("%s %d %d %d %d %d %d\n", tag, g_leases, g_clears, g_holds, g_asks, g_bad, P.boostLive)
int main(void)
{
    /* a 3.0 kext: the policy is asked once, the process boosts per burst as in 3.0 */
    reset(NVKMD_NVRM_BOOST_BURST, NVRM_XNU_NO_METHOD, 0, 0); work(10); SHOW("oldkext");
    /* undecided: as 3.0 */
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_UNDECIDED, 0); work(10); SHOW("undecided");
    /* idle mode from the start: nothing asked */
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_IDLE, 0); work(10); SHOW("idle");
    /* normal for 5 s, idle mode switched on: one clear within a second, then nothing; normal again: leases again */
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_NORMAL, 0); work(5); SHOW("on5");
    g_policy = NVRM_BP_IDLE; uint64_t t0 = g_now; int c0 = g_clears;
    while (g_clears == c0 && g_now < t0 + 5 * S) { g_now += MS; nvkmd_nvrm_boost_burst(&P); }
    printf("clear_after_ms %llu\n", (unsigned long long)((g_now - t0) / MS));
    work(10); SHOW("idle10");
    g_policy = NVRM_BP_NORMAL; t0 = g_now; int l0 = g_leases;
    while (g_leases == l0 && g_now < t0 + 5 * S) { g_now += MS; nvkmd_nvrm_boost_burst(&P); }
    printf("lease_after_ms %llu\n", (unsigned long long)((g_now - t0) / MS));
    /* started with NVRM_610_BOOST=0 (cold idle mode); the policy says normal: it boosts */
    reset(NVKMD_NVRM_BOOST_OFF, 0, NVRM_BP_NORMAL, 0); work(10); SHOW("conf0normal");
    reset(NVKMD_NVRM_BOOST_OFF, 0, NVRM_BP_UNDECIDED, 0); work(10); SHOW("conf0undecided");
    /* hold (asked at create, so live): nothing per submission; idle mode clears it once; normal asks it once again */
    reset(NVKMD_NVRM_BOOST_HOLD, 0, NVRM_BP_UNDECIDED, NVKMD_NVRM_BOOST_HOLD); work(5); SHOW("hold");
    g_policy = NVRM_BP_IDLE; work(5); SHOW("holdidle");
    g_policy = NVRM_BP_NORMAL; work(5); SHOW("holdnormal");
    /* the RM refuses the lease: asked again 30 s later, not every second and not never */
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_UNDECIDED, 0); g_rc = 0x56;
    for (int i = 0; i < 31; i++) { g_now += 2 * S; nvkmd_nvrm_boost_burst(&P); }
    printf("refused %d %d\n", g_calls, P.boostLive);
    /* a call to the kext that fails after it answered idle mode keeps idle mode */
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_IDLE, 0); work(2); g_kr = -1; work(5); SHOW("flaky");
    /* eight threads at the same instant: one renewal, one read of the policy */
    reset(NVKMD_NVRM_BOOST_BURST, 0, NVRM_BP_NORMAL, 0); pthread_t t[8];
    for (int i = 0; i < 8; i++) pthread_create(&t[i], 0, hammer, 0);
    for (int i = 0; i < 8; i++) pthread_join(t[i], 0);
    SHOW("threads");
    /* every pair of policy and NVRM_610_BOOST */
    for (int pol = 0; pol < 4; pol++) for (int conf = 0; conf < 3; conf++) printf("shape %d %d %d\n", pol, conf, nvrm_bp_shape((uint64_t)pol, conf));
    /* the conf reader on the file the test wrote (argv[1] picks the case) */
    return 0;
}
int conf_main(void) { int v = nvkmd_nvrm_boost_conf(); printf("conf %d\n", v); return 0; }
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

cc = shutil.which("clang") or shutil.which("cc")
if not cc:
    print("skip: no C compiler")
    sys.exit(0 if not fails else 1)
with tempfile.TemporaryDirectory() as d:
    d = pathlib.Path(d)
    conf = d / "nvrm610.conf"
    (d / "h.c").write_text(harness.replace("int main(void)", "int sim_main(void)") +
                           "\nint main(int c, char **v) { return c > 1 ? conf_main() : sim_main(); }\n")
    r = subprocess.run([cc, "-O1", "-Wall", "-Wno-unused-function", "-I", str(abi.parent), f'-DNVMTL_CFG_PATH="{conf}"',
                        "-o", str(d / "h"), str(d / "h.c"), "-lpthread"], capture_output=True, text=True)
    check("the patch's code compiles", r.returncode == 0, r.stderr.strip()[-400:])
    if r.returncode == 0:
        import os
        for name, envv, text, want in CONF_CASES:
            if text is None:
                conf.unlink(missing_ok=True)
            else:
                conf.write_text(text)
            env = {k: x for k, x in os.environ.items() if k != "NVRM_610_BOOST"}
            if envv is not None:
                env["NVRM_610_BOOST"] = envv
            o = subprocess.run([str(d / "h"), "conf"], capture_output=True, text=True, timeout=30, env=env)
            got = int(o.stdout.split()[1]) if o.stdout.startswith("conf ") else None
            check("conf: " + name, got == want and o.stderr == "", f"got {got}, want {want}, stderr {o.stderr!r}")
        conf.unlink(missing_ok=True)
        out = subprocess.run([str(d / "h")], capture_output=True, text=True, timeout=60).stdout
        v, shapes = {}, {}
        for l in out.strip().split("\n"):
            f = l.split()
            if f[0] == "shape":
                shapes[(int(f[1]), int(f[2]))] = int(f[3])
            else:
                v[f[0]] = [int(x) for x in f[1:]]
        # SHOW: leases clears holds asks bad live
        o = v["oldkext"]
        check("with a 3.0 kext the policy is asked once and the process boosts per burst as before",
              10 <= o[0] <= 11 and o[1] == 0 and o[3] == 1 and o[4] == 0, str(o))
        u = v["undecided"]
        check("undecided: per burst, as in 3.0 (10 or 11 leases in 10 s)", 10 <= u[0] <= 11 and u[1] == 0 and u[4] == 0, str(u))
        check("undecided: the policy is read once per renewal, not per submission", u[3] == u[0], str(u))
        i = v["idle"]
        check("idle mode from the start asks the GPU for nothing", i[0] == 0 and i[1] == 0 and i[2] == 0, str(i))
        check("switching idle mode on clears this process's lease within a second", v["clear_after_ms"][0] <= 1000,
              f"{v['clear_after_ms'][0]} ms")
        i10 = v["idle10"]
        check("then nothing more is asked or cleared", i10[1] == 1 and i10[0] == v["on5"][0] and i10[5] == 0, str(i10))
        check("switching it off again asks for a lease within a second", v["lease_after_ms"][0] <= 1000,
              f"{v['lease_after_ms'][0]} ms")
        c = v["conf0normal"]
        check("an app started with NVRM_610_BOOST=0 boosts once the policy says normal", 10 <= c[0] <= 11, str(c))
        check("with NVRM_610_BOOST=0 and no policy it asks for nothing", v["conf0undecided"][:3] == [0, 0, 0], str(v["conf0undecided"]))
        h = v["hold"]
        check("a held boost is not asked again per submission", h[:3] == [0, 0, 0] and h[5] == 2, str(h))
        hi = v["holdidle"]
        check("idle mode clears a held boost once", hi[1] == 1 and hi[2] == 0 and hi[5] == 0, str(hi))
        hn = v["holdnormal"]
        check("normal asks for the hold again once, never a lease", hn[2] == 1 and hn[0] == 0 and hn[5] == 2, str(hn))
        rf = v["refused"]
        check("a refused lease is asked again after 30 s, not every second (3 calls in 62 s)", rf == [3, 0], str(rf))
        fl = v["flaky"]
        check("a failed read of the policy keeps the last answer (idle mode stays)", fl[0] == 0 and fl[1] == 0, str(fl))
        t = v["threads"]
        check("eight threads submitting at once renew once and read the policy once", t[0] == 1 and t[3] == 1, str(t))
        want = {(0, 0): 0, (0, 1): 1, (0, 2): 2, (1, 0): 0, (1, 1): 0, (1, 2): 0, (2, 0): 1, (2, 1): 1, (2, 2): 2,
                (3, 0): 0, (3, 1): 1, (3, 2): 2}
        check("the shape for every policy and NVRM_610_BOOST (an unknown policy is the process's own setting)",
              shapes == want, str({k: x for k, x in shapes.items() if want.get(k) != x}))
        check("no request had a shape other than a lease, a hold or a clear", all(x[4] == 0 for x in v.values() if len(x) == 6))

print(f"{fails} failed" if fails else "all passed")
sys.exit(1 if fails else 0)
