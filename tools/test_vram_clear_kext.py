#!/usr/bin/env python3
"""The kext side of the copy engine's clear of a new block of video memory (3.0.1): NVRM's clear in
kexts/NVRM/nvrm_pageoff.c, the answer it gives (nvrm_surfshare.cpp, NVRM.cpp) and the framebuffer's ask (nvrm-fb.cpp).
tools/test_vram_clear.c holds the pure rules of nvrm-vram-clear.h; this test holds the code around them:

  the progress  _nvrmCeReached, compiled from nvrm_pageoff.c with a CeUtils and a ceutilsUpdateProgress that do what
                the RM's do (ce_utils.c ceutilsUpdateProgress_IMPL: one read of the 32-bit semaphore, a value below the
                stored low word counted as a wrap): ordinary progress reaches the work; a failed read (0) or a read of
                all ones with work pending is not reached and leaves the stored progress as it was, and the next good
                read is reached; a real wrap of the 32-bit counter is still reached
  the clear     every wait of nvrm_pageoff.c asks through _nvrmCeReached; in _nvrmClearLocked "sent" is set after
                every check that refuses and directly before the memset, no copy-engine utility is made there and
                none is "nothing sent", and the wait is bounded by the clock at a constant of at most one second
  the answer    nvrm_surfshare.cpp maps the status to DONE, LOST or NOT_RUN with "sent" deciding the last two; NVRM.cpp
                presets NOT_RUN and runs the clear only while the GPU is running
  the ask       vramClear asks only while the clear is on (boot argument nvfbceclear=0 turns it off), takes only
                kIOReturnUnsupported for an older NVRM, and both callers keep a block it answers -1 for (neither handed
                out nor freed); nvrm-vram-clear.h compares with the NVRM_VRAM_CLEAR_* names (compiled against an ABI
                with other numbers) and does not compile without nvrm_vram_abi.h
  controls      the progress arms fail on copies of _nvrmCeReached that are wrong in the ways listed

Linux or macOS, a C compiler for the compiled arms, no GPU. usage: test_vram_clear_kext.py [driver checkout]"""
import pathlib, re, shutil, subprocess, sys, tempfile

R = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parents[1]
fails = 0


def arm(name, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + name + (f" ({str(detail).strip()[-300:]})" if detail and not ok else ""))
    fails += not ok


def uncomment(text):
    """C or C++ without its comments (string and character literals kept), lines kept where they were."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1]); i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("\n" * text.count("\n", i, j)); i = j
        else:
            out.append(c); i += 1
    return "".join(out)


def func(text, start):
    """The definition that begins with `start` (not a declaration of it), up to its closing brace at the start of a
    line, or ""."""
    a = text.find(start)
    while a >= 0 and ";" in text[a:text.find("{", a)]:
        a = text.find(start, a + 1)
    b = text.find("\n}\n", a)
    return text[a:b + 3] if a >= 0 and b > a else ""


PAGEOFF, SURF = R / "kexts/NVRM/nvrm_pageoff.c", R / "kexts/NVRM/nvrm_surfshare.cpp"
NVRM, FB, HDR = R / "kexts/NVRM/NVRM.cpp", R / "kexts/NVRMFB/fb/nvrm-fb.cpp", R / "kexts/NVRMFB/fb/nvrm-vram-clear.h"
ABI = R / "kexts/NVRMFB/nvrm_vram_abi.h"
missing = [str(p.relative_to(R)) for p in (PAGEOFF, SURF, NVRM, FB, HDR, ABI) if not p.is_file()]
if missing:
    print("SKIP: not in this checkout: " + ", ".join(missing))
    sys.exit(2)
po = uncomment(PAGEOFF.read_text())
reached = func(po, "static NvBool _nvrmCeReached(struct CeUtils *pCe, NvU64 workId)")
clear = func(po, "static NV_STATUS _nvrmClearLocked(")
outer = func(po, "NV_STATUS nvrm_ce_clear_vidmem(nv_state_t *nv,")

# --- the clear, as text ---------------------------------------------------------------------------------------------
arm("_nvrmCeReached, _nvrmClearLocked and nvrm_ce_clear_vidmem are in nvrm_pageoff.c", bool(reached and clear and outer))
arm("ceutilsUpdateProgress is called in _nvrmCeReached once and nowhere else in nvrm_pageoff.c",
    reached.count("ceutilsUpdateProgress(") == 1 and po.count("ceutilsUpdateProgress(") == 1)
arm("all three waits for the engine (the clear and the page-off's two) wait on _nvrmCeReached",
    len(re.findall(r"while \(!_nvrmCeReached\(pMemoryManager->pCeUtils, [\w.]+\)\)", po)) == 3
    and po.count("_nvrmCeReached(") == 4)
sent = clear.find("*pSent = 1;")
memset = clear.find("ceutilsMemset(")
before = clear[:sent] if sent >= 0 else clear
arm('in _nvrmClearLocked "*pSent = 1;" is set once, directly before the memset, the one call that sends',
    clear.count("*pSent = 1;") == 1 and clear.count("ceutilsMemset(") == 1 and clear.count("pSent") == 2
    and re.search(r"\*pSent = 1;\s*st = ceutilsMemset\(pMemoryManager->pCeUtils, &p\);", clear) is not None)
# every check that refuses returns before "sent": after it, only the memset's own error, the timeout and NV_OK return
rets_after = re.findall(r"return ([^;]+);", clear[sent:]) if sent >= 0 else []
arm('every check of _nvrmClearLocked that returns comes before "*pSent = 1;" (after it: the memset\'s error, the '
    'timeout, NV_OK)', sent > 0 and before.count("return ") > 0 and rets_after == ["st", "NV_ERR_TIMEOUT", "NV_OK"],
    rets_after)
arm("nvrm_ce_clear_vidmem sets *pSent to 0 before it can return", re.search(r"\{\s*THREAD_STATE_NODE[^}]*?\*pSent = 0;", outer, re.S)
    is not None and outer.find("*pSent = 0;") < outer.find("return "))
nul = re.search(r"if \(pMemoryManager->pCeUtils == NULL\) return (NV_ERR_\w+);", clear)
arm("_nvrmClearLocked makes no copy-engine utility: without the RM's own it returns an error before anything is sent",
    "memmgrInitCeUtils" not in clear and nul is not None and nul.start() < sent and "pCeUtils = " not in clear)
wait = re.search(r"const NvU64 until = osGetMonotonicTimeNs\(\) \+ NVRM_CE_CLEAR_WAIT_NS;\s*"
                 r"while \(!_nvrmCeReached\(pMemoryManager->pCeUtils, p\.submittedWorkId\)\) \{\s*"
                 r"if \(osGetMonotonicTimeNs\(\) >= until\) return NV_ERR_TIMEOUT;\s*osDelayUs\(\d+\);\s*\}", clear)
arm("the clear's wait is bounded by osGetMonotonicTimeNs() against NVRM_CE_CLEAR_WAIT_NS, not by a count of polls",
    wait is not None and "spins" not in clear and wait.start() > memset)
dm = re.findall(r"(?m)^#define NVRM_CE_CLEAR_WAIT_NS ([^\n]+)$", po)
expr = re.sub(r"(?<=\d)(ull|ULL|ul|UL|u|U|ll|LL)\b", "", dm[0]) if len(dm) == 1 else ""
ns = eval(expr, {"__builtins__": {}}) if expr and re.fullmatch(r"[\d\s*+()]+", expr) else -1
arm("NVRM_CE_CLEAR_WAIT_NS is defined once and is at most one second", 0 < ns <= 1_000_000_000, (dm, ns))

# --- the answer -----------------------------------------------------------------------------------------------------
ss = func(uncomment(SURF.read_text()), 'extern "C" void nvrm_vidmem_clear(struct NVRMVidmemClear *q, void *nv)')
arm("nvrm_surfshare.cpp answers DONE for NV_OK, and else LOST when work was sent and NOT_RUN when nothing was",
    re.search(r"NvU32 sent = 0;\s*const NV_STATUS st = nvrm_ce_clear_vidmem\(nv, c, m, q->bytes, &cleared, &sent, &us\);"
              r"[^}]*?q->result = st == NV_OK \? NVRM_VRAM_CLEAR_DONE : sent \? NVRM_VRAM_CLEAR_LOST : NVRM_VRAM_CLEAR_NOT_RUN;",
              ss, re.S) is not None and ss.count("q->result") == 1)
arm("nvrm_surfshare.cpp's early returns (handles not found) come before the clear and leave the answer as it was set",
    all(ss.find(r) < ss.find("nvrm_ce_clear_vidmem(") for r in re.findall(r"return;", ss)) and ss.count("return;") == 2)
nv = uncomment(NVRM.read_text())
a = nv.find("if (fn && fn->isEqualTo(NVRM_VRAM_FN_CLEAR)) {")
br = nv[a:nv.find("if (fn && fn->isEqualTo(NVRM_VRAM_FN_CLEARED))", a)] if a >= 0 else ""
pre = br.find("q->result = NVRM_VRAM_CLEAR_NOT_RUN;")
run = re.search(r"if \(OSAddAtomic\(0, &fGpuPm\) == kGpuPmRunning\) \{\s*nvrm_vidmem_clear\(q, &fNv\);", br)
arm("NVRM.cpp sets the answer to NOT_RUN before the clear and runs it only while the GPU is running",
    pre > 0 and run is not None and pre < run.start() and nv.count("nvrm_vidmem_clear(q, ") == 1
    and br.find("nv_xnu_pm_try_enter(") < run.start())

# --- the ask --------------------------------------------------------------------------------------------------------
fb = uncomment(FB.read_text())
vc = func(fb, "int NVRMNVDAFramebuffer::vramClear(")
gate = vc.find("if (OSAddAtomic(0, &gVramCeClear) == NVRM_VC_ON && ")
arm("vramClear asks NVRM only while the clear is NVRM_VC_ON",
    gate > 0 and vc.count("callPlatformFunction(") == 1 and vc.find("callPlatformFunction(") > gate
    and vc.count("gVramCeClear") == 2)
arm("the clear starts on, and boot argument nvfbceclear=0 turns it off",
    "static volatile SInt32 gVramCeClear = NVRM_VC_ON;" in fb
    and re.search(r'PE_parse_boot_argn\("nvfbceclear", &(\w+), sizeof \1\) && !\1 && '
                  r'OSCompareAndSwap\(NVRM_VC_ON, NVRM_VC_OFF, \(volatile UInt32 \*\)&gVramCeClear\)', fb) is not None)
arm('vramClear takes only kIOReturnUnsupported for "an older NVRM"',
    re.search(r"nvrm_vc_answer\(&st, ret != kIOReturnUnsupported, q\.result, q\.cleared, bytes\)", vc) is not None)
arm("vramClear presets the answer to NOT_RUN and returns -1 exactly when the block is to be kept",
    "q.result = NVRM_VRAM_CLEAR_NOT_RUN;" in vc and re.search(r"if \(what == NVRM_VC_KEEP\) return -1;", vc) is not None
    and vc.count("return -1;") == 1)
calls = [m.start() for m in re.finditer(r"\bvramClear\(", fb)]
grant = re.search(r"else if \(vramClear\(mem, kva, want, false\) < 0\) return false;\s*"
                  r"const UInt64 allocationCookie = gAllocations\.insert\(mem, want\);", fb)
worker = re.search(r"const int zero = fill \? vramClear\(\(struct NvKmsKapiMemory \*\)r\.handle, r\.kva, r\.actualSize, true\) : 1;"
                   r"\s*if \(zero < 0\) return true;\s*const bool whole = zero > 0;", fb)
arm("vramClear has two callers, the grant and the worker", len(calls) == 4, len(calls))
arm("the grant refuses a block vramClear keeps before it is recorded or handed out, and does not free it", grant is not None)
arm("the worker leaves a block vramClear keeps before it is stored or given back", worker is not None)

hdr = uncomment(HDR.read_text())
arm("nvrm-vram-clear.h compares NVRM's answer with the NVRM_VRAM_CLEAR_* names, not numbers",
    "result == NVRM_VRAM_CLEAR_NOT_RUN" in hdr and "result != NVRM_VRAM_CLEAR_DONE" in hdr
    and not re.search(r"result\s*[=!<>]=?\s*\d", hdr))
arm("nvrm-vram-clear.h stops the build without nvrm_vram_abi.h",
    re.search(r'#ifndef NVRM_VRAM_CLEAR_DONE\s*#error "[^"\n]*nvrm_vram_abi\.h[^"\n]*"\s*#endif', hdr) is not None)

# --- compiled -------------------------------------------------------------------------------------------------------
PROGRESS = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
typedef uint64_t NvU64;
typedef uint32_t NvU32;
typedef unsigned char NvBool;
#define NV_TRUE ((NvBool)1)
#define NV_FALSE ((NvBool)0)
struct CeUtils { NvU64 lastCompletedPayload, lastSubmittedPayload; NvU32 *pSema; };
/* What the RM's ceutilsUpdateProgress_IMPL (ce_utils.c) does: one read of the 32-bit semaphore. */
static NvU64 ceutilsUpdateProgress(struct CeUtils *pCeUtils)
{
   NvU32 hw = *pCeUtils->pSema;
   NvU64 sw = pCeUtils->lastCompletedPayload;
   if (hw == (NvU32)sw) return sw;
   if (hw < (NvU32)sw) sw += 0x100000000ULL;
   sw &= 0xFFFFFFFF00000000ULL;
   sw |= (NvU64)hw;
   pCeUtils->lastCompletedPayload = sw;
   return sw;
}
/* ---- nvrm_pageoff.c ---- */
%s
/* ---- end ---- */
int main(int argc, char **argv)
{
   /* completed submitted workId, then the semaphore's reads: prints "reached:completed" for each read */
   if (argc < 5) return 2;
   NvU32 sema = 0;
   struct CeUtils ce = { strtoull(argv[1], NULL, 0), strtoull(argv[2], NULL, 0), &sema };
   const NvU64 work = strtoull(argv[3], NULL, 0);
   for (int i = 4; i < argc; i++) {
      sema = (NvU32)strtoul(argv[i], NULL, 0);
      const NvBool r = _nvrmCeReached(&ce, work);
      printf("%%s%%d:%%#llx", i > 4 ? " " : "", (int)r, (unsigned long long)ce.lastCompletedPayload);
   }
   printf("\n");
   return 0;
}
'''
W = 1 << 32
PROGRESS_CASES = [
    # (what the arm holds, completed, submitted, work, reads, what comes out)
    ("ordinary progress reaches the work id, and not before", 40, 42, 42, [41, 42], f"0:{41:#x} 1:{42:#x}"),
    ("a failed read (0) with work pending is not reached and leaves the progress as it was; the next good read is reached",
     41, 42, 42, [0, 42], f"0:{41:#x} 1:{42:#x}"),
    ("a read of 0xFFFFFFFF (a GPU off the bus) with work pending is not reached and leaves the progress as it was; "
     "the next good read is reached", 41, 42, 42, [0xFFFFFFFF, 42], f"0:{41:#x} 1:{42:#x}"),
    ("two failed reads in a row change nothing, and the work is reached when it is done", 41, 42, 42, [0, 0, 41, 42],
     f"0:{41:#x} 0:{41:#x} 0:{41:#x} 1:{42:#x}"),
    ("a real wrap of the 32-bit counter (work sent beyond 2^32) is counted and still reached",
     W - 16, W + 5, W + 5, [0xFFFFFFFE, 3, 5], f"0:{W - 2:#x} 0:{W + 3:#x} 1:{W + 5:#x}"),
    ("work below 2^32 is reached by a read after the counter wrapped", W - 16, W + 5, W - 1, [1], f"1:{W + 1:#x}"),
]


def run_progress(src, d, tag):
    c, exe = d / f"{tag}.c", d / tag
    c.write_text(PROGRESS % src)
    r = subprocess.run([cc, "-O1", "-Wall", "-o", str(exe), str(c)], capture_output=True, text=True)
    if r.returncode:
        return "does not compile: " + r.stderr[-400:]
    out = []
    for what, done, sub, work, reads, want in PROGRESS_CASES:
        o = subprocess.run([str(exe), hex(done), hex(sub), hex(work)] + [hex(x) for x in reads],
                           capture_output=True, text=True, timeout=30)
        out.append((what, o.stdout.strip() if not o.returncode else f"exit {o.returncode}", want))
    return out


def caught(res):
    """A control holds when the wrong copy compiles and runs, and an arm then fails on it."""
    return not isinstance(res, str) and any(g != w for _, g, w in res)


cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
if not cc:
    print("SKIP: no C compiler on PATH: _nvrmCeReached and nvrm-vram-clear.h were not compiled"
          + (" (and a text arm FAILED)" if fails else ""))
    sys.exit(1 if fails else 2)
with tempfile.TemporaryDirectory() as tmp:
    d = pathlib.Path(tmp)
    res = run_progress(reached, d, "reached")
    for what, got, want in (res if not isinstance(res, str) else [("_nvrmCeReached compiles", res, "")]):
        arm(what, got == want, f"{got} != {want}")
    muts = [("does not put the progress back", "{ pCe->lastCompletedPayload = before; return NV_FALSE; }", "{ return NV_FALSE; }"),
            ("believes any progress", "if (now > pCe->lastSubmittedPayload)", "if (0)"),
            ("takes the last work sent as too far", "now > pCe->lastSubmittedPayload", "now >= pCe->lastSubmittedPayload"),
            ("compares the low 32 bits of the work sent", "now > pCe->lastSubmittedPayload", "now > (NvU32)pCe->lastSubmittedPayload"),
            ("reports a wrong progress as reached", "pCe->lastCompletedPayload = before; return NV_FALSE;",
             "pCe->lastCompletedPayload = before; return NV_TRUE;")]
    for i, (what, old, new) in enumerate(muts):
        arm(f"control: an _nvrmCeReached that {what} fails", reached.count(old) == 1 and caught(run_progress(reached.replace(old, new), d, f"m{i}")))

    # the header against an ABI whose answers have other numbers: only names can give the right answers
    prog = d / "hdr.c"
    prog.write_text(r'''
#include <stdio.h>
#define NVRM_VRAM_CLEAR_DONE 7u
#define NVRM_VRAM_CLEAR_NOT_RUN 5u
#define NVRM_VRAM_CLEAR_LOST 6u
#include "nvrm-vram-clear.h"
int main(void)
{
   int s1 = NVRM_VC_ON, s2 = NVRM_VC_ON, s3 = NVRM_VC_ON;
   const int a1 = nvrm_vc_answer(&s1, 1, NVRM_VRAM_CLEAR_DONE, 64, 64);
   const int a2 = nvrm_vc_answer(&s2, 1, NVRM_VRAM_CLEAR_NOT_RUN, 0, 64);
   const int a3 = nvrm_vc_answer(&s3, 1, NVRM_VRAM_CLEAR_LOST, 0, 64);
   printf("%d %d %d %d %d %d\n", a1, s1, a2, s2, a3, s3);
   return 0;
}
''')
    r = subprocess.run([cc, "-Wall", "-I", str(HDR.parent), "-o", str(d / "hdr"), str(prog)], capture_output=True, text=True)
    o = subprocess.run([str(d / "hdr")], capture_output=True, text=True, timeout=30) if not r.returncode else None
    want = "3 1 1 1 2 2"          # DONE: probe, still on; NOT_RUN: fill, still on; LOST: keep, stood down
    arm("nvrm-vram-clear.h gives DONE, NOT_RUN and LOST their answers whatever numbers the ABI gives them",
        o is not None and o.stdout.strip() == want, r.stderr[-300:] if r.returncode else (o.stdout if o else ""))
    alone = d / "alone.c"
    alone.write_text('#include "nvrm-vram-clear.h"\nint main(void) { return 0; }\n')
    r = subprocess.run([cc, "-fsyntax-only", "-I", str(HDR.parent), str(alone)], capture_output=True, text=True)
    arm("nvrm-vram-clear.h does not compile without nvrm_vram_abi.h, and says so",
        r.returncode != 0 and "nvrm_vram_abi.h" in r.stderr, r.stderr)
    ok = d / "ok.c"
    ok.write_text('#include "nvrm_vram_abi.h"\n#include "nvrm-vram-clear.h"\nint main(void) { return 0; }\n')
    r = subprocess.run([cc, "-fsyntax-only", "-Wall", "-I", str(ABI.parent), "-I", str(HDR.parent), str(ok)],
                       capture_output=True, text=True)
    arm("nvrm-vram-clear.h compiles after the framebuffer's nvrm_vram_abi.h", r.returncode == 0, r.stderr)

print("FAILED" if fails else "PASS")
sys.exit(1 if fails else 0)
