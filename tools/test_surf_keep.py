#!/usr/bin/env python3
"""Since 3.0.7 a caller that presents into a surface's video memory may keep its import of that memory for more than
one frame. The kernel names the block it shared in the grant (the `pad` word of NVRM_ESC_SURF_VRAM) and, when a mark
(NVRM_ESC_SURF_DIRTY) names a block, marks only if the surface still has it; otherwise it answers NVRM_SS_MOVED and
records nothing. A mark taken for another block would tell WindowServer that the surface holds a frame that was
written somewhere else: the window would show stale or foreign pixels.

This test runs the two rules (kexts/NVRM/accel/nvaccel_surf_keep.h) and the lines of the escape that apply them, cut
out of kexts/NVRM/accel/nvrm-accel.cpp as written, on the host with stand-ins for what they call; it runs them again
with one rule broken each, which has to fail; and it reads where the escape writes `pad` on a grant.
usage: test_surf_keep.py"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACCEL = ROOT / "kexts" / "NVRM" / "accel"
CXX = os.environ.get("CXX", "c++")
fails = 0


def check(what, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + what + ("" if ok else f"\n     {detail}"))
    fails += 0 if ok else 1


HARNESS = r"""
#include <cstdio>
#include <cstring>
#define NVRM_SS_KERNEL_ABI 1
#include "nvrm_surfshare_abi.h"
#include "nvaccel_surf_keep.h"
typedef long long SInt64;
struct NVVidMemory { void *fRmHandle; unsigned long long fAllocationCookie; };
struct NVResource { int unused; };
static char trace[64];
static bool restoreAnswer; static unsigned dirtyAnswer; static SInt64 gCntSvMarkMoved;
static void OSAddAtomic64(SInt64 by, SInt64 *to) { *to += by; }
static bool nvAccelRestoreParkedForDirty(NVResource *, NVVidMemory *, unsigned) { strcat(trace, "R"); return restoreAnswer; }
static unsigned nvAccelSurfDirty(void *, NVResource *, unsigned) { strcat(trace, "D"); return dirtyAnswer; }
static int fails;
#define ARM(name, cond) do { const int ok_ = !!(cond); printf("%s  %s\n", ok_ ? "ok  " : "FAIL", name); if (!ok_) fails++; } while (0)

/* the mark, as the escape runs it */
static unsigned mark(unsigned pad, bool restore, bool haveCache, bool haveRes, NVVidMemory *vm, unsigned dirty = 0)
{
    struct NVRMSurfShareEsc esc; memset(&esc, 0, sizeof esc);
    struct NVRMSurfShareEsc *p = &esc;
    p->pad = pad; p->plane = 0; p->status = 0x12345678u;
    static int cacheObject; static NVResource resObject;
    void *cache = haveCache ? &cacheObject : nullptr;
    NVResource *res = haveRes ? &resObject : nullptr;
    trace[0] = 0; restoreAnswer = restore; dirtyAnswer = dirty;
    {
@LIFTED@
    }
    return p->status;
}

int main()
{
    /* the two rules */
    ARM("a grant that succeeded names its block by the low 32 bits of the cookie", nvaccel_keep_cookie(0, 0x0000000700000005ull) == 5u);
    ARM("a grant that was refused names none", nvaccel_keep_cookie(NVRM_SS_NO_VRAM, 5) == 0 && nvaccel_keep_cookie(NVRM_SS_BUSY, 5) == 0);
    ARM("a mark that names no block is never 'moved', with or without VRAM",
        !nvaccel_keep_moved(0, 1, 5) && !nvaccel_keep_moved(0, 0, 0) && !nvaccel_keep_moved(0, 1, 0));
    ARM("a mark for the block the surface has is not 'moved'", !nvaccel_keep_moved(5, 1, 5) && !nvaccel_keep_moved(5, 1, 0x0000000900000005ull));
    ARM("a mark for another block is", nvaccel_keep_moved(5, 1, 6) == 1 && nvaccel_keep_moved(6, 1, 5) == 1);
    ARM("a mark for a block when the surface has none is", nvaccel_keep_moved(5, 0, 0) == 1 && nvaccel_keep_moved(5, 0, 5) == 1);

    /* the escape's lines */
    static int handle;
    NVVidMemory wired = { &handle, 0x0000000100000042ull }, bare = { nullptr, 0 };
    ARM("a mark without a block's name is taken as before 3.0.7: the parked block put back, then the mark",
        mark(0, true, true, true, &wired) == 0 && !strcmp(trace, "RD") && gCntSvMarkMoved == 0);
    ARM("and answers what the mark answers", mark(0, true, true, true, &wired, NVRM_SS_BUSY) == NVRM_SS_BUSY);
    ARM("without a name and without VRAM: NVRM_SS_NO_CACHE, as before", mark(0, true, true, true, &bare) == NVRM_SS_NO_CACHE && !strcmp(trace, "R"));
    ARM("a mark that names the surface's block is taken", mark(0x42, true, true, true, &wired) == 0 && !strcmp(trace, "RD") && gCntSvMarkMoved == 0);
    ARM("a mark that names another block is refused with NVRM_SS_MOVED and nothing is marked; the parked block was put back first",
        mark(0x43, true, true, true, &wired) == NVRM_SS_MOVED && !strcmp(trace, "R") && gCntSvMarkMoved == 1);
    ARM("a mark that names a block when the surface has none is refused the same way",
        mark(0x42, true, true, true, &bare) == NVRM_SS_MOVED && !strcmp(trace, "R") && gCntSvMarkMoved == 2);
    ARM("a block that could not be put back as it was refuses any mark, named or not, and is not counted as moved",
        mark(0x42, false, true, true, &wired) == NVRM_SS_NO_CACHE && mark(0, false, true, true, &wired) == NVRM_SS_NO_CACHE &&
        !strcmp(trace, "R") && gCntSvMarkMoved == 2);
    ARM("no cache, no resource or no memory object: NVRM_SS_NO_CACHE, and nothing of them is read",
        mark(0x42, true, false, true, &wired) == NVRM_SS_NO_CACHE && mark(0x42, true, true, false, &wired) == NVRM_SS_NO_CACHE &&
        mark(0x42, true, true, true, nullptr) == NVRM_SS_NO_CACHE && mark(0, true, true, true, nullptr) == NVRM_SS_NO_CACHE &&
        gCntSvMarkMoved == 2);
    printf("%s\n", fails ? "FAILED" : "all arms passed");
    return fails ? 1 : 0;
}
"""


def main():
    if not shutil.which(CXX):
        print(f"SKIP: no C++ compiler ({CXX})")
        return 0
    source = (ACCEL / "nvrm-accel.cpp").read_text()
    escape_start = source.index("static IOReturn nvAccelSurfVram(NVAccel *owner, struct NVRMSurfShareEsc *p, task_t task, unsigned cmd)\n{")
    escape = source[escape_start:source.index("\n}\n", escape_start)]
    first, last = "        const bool sameBacking = nvAccelRestoreParkedForDirty(res, vm, p->plane);\n", \
                  "        if (p->status == 0) gNVSVDirty++; else gNVSVDirtyRefused++;\n"
    check("the escape's mark is where this test cuts it out", escape.count(first) == 1 and escape.count(last) == 1)
    if fails:
        return 1
    lifted = escape[escape.index(first):escape.index(last)]
    header = (ACCEL / "nvaccel_surf_keep.h").read_text()

    def run(lines, keep_header):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            (tmp / "nvaccel_surf_keep.h").write_text(keep_header)
            (tmp / "keep.cpp").write_text(HARNESS.replace("@LIFTED@", lines))
            built = subprocess.run([CXX, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(tmp), "-I", str(ROOT / "kexts" / "NVRM"),
                                    str(tmp / "keep.cpp"), "-o", str(tmp / "keep")], capture_output=True, text=True)
            if built.returncode:
                return None, built.stderr[-2500:]
            ran = subprocess.run([str(tmp / "keep")], capture_output=True, text=True, timeout=60)
            return ran.returncode, ran.stdout + ran.stderr

    status, out = run(lifted, header)
    check("the rules and the escape's mark, as written: every arm passes", status == 0 and "all arms passed" in out, out or "")

    def broken(what, text, old, new):
        if text.count(old) != 1:
            check(f"control '{what}': the line it breaks is in the source once", False, repr(old))
            return None
        return text.replace(old, new)

    controls = [
        ("a mark for another block is taken", "lines",
         "        else if (nvaccel_keep_moved(p->pad, vm->fRmHandle != nullptr, vm->fAllocationCookie)) {",
         "        else if (false && nvaccel_keep_moved(p->pad, vm->fRmHandle != nullptr, vm->fAllocationCookie)) {"),
        ("the block is compared against the whole cookie", "header",
         "return pad != 0 && (!haveVram || (unsigned)cookie != pad);", "return pad != 0 && (!haveVram || cookie != pad);"),
        ("a mark that names no block is compared too", "header",
         "return pad != 0 && (!haveVram || (unsigned)cookie != pad);", "return (!haveVram || (unsigned)cookie != pad);"),
        ("a surface without VRAM takes a named mark for NO_CACHE", "header",
         "return pad != 0 && (!haveVram || (unsigned)cookie != pad);", "return pad != 0 && haveVram && (unsigned)cookie != pad;"),
        ("a refused grant names a block", "header", "return status == 0 ? (unsigned)cookie : 0u;", "return status == 0 ? (unsigned)cookie : (unsigned)cookie;"),
        ("a block that could not be put back is marked", "lines",
         "        if (!sameBacking || !cache || !res || !vm) p->status = NVRM_SS_NO_CACHE;",
         "        if ((!sameBacking && false) || !cache || !res || !vm) p->status = NVRM_SS_NO_CACHE;"),
    ]
    for what, where, old, new in controls:
        changed = broken(what, lifted if where == "lines" else header, old, new)
        if changed is None:
            continue
        status, out = run(changed if where == "lines" else lifted, changed if where == "header" else header)
        check(f"control, one rule broken ({what}): the host program fails", status == 1, f"status {status}\n{(out or '')[-1200:]}")

    # where the grant writes the word: cleared ahead of the first refusal of the escape (the version), not only of the
    # refusals that follow the surface's lookup, and named once the share has answered
    spots = [escape.find(piece) for piece in ("if (cmd != NVRM_ESC_SURF_DIRTY) p->pad = 0;",
                                              "{ p->status = NVRM_SS_BAD_VERSION; return kIOReturnSuccess; }",
                                              "if (cmd == NVRM_ESC_SURF_DIRTY) {",
                                              "if (!cache || !res) p->status = NVRM_SS_NO_CACHE;", "p->status = g.status;",
                                              "p->pad = nvaccel_keep_cookie(g.status, vm->fAllocationCookie);")]
    before_clear = escape[:max(spots[0], 0)]
    check("a grant clears the word before every refusal and names the block only after the share answered",
          -1 not in spots and spots == sorted(spots) and escape.count("p->pad =") == 2, str(spots))
    check("the only return ahead of that is the completion mark's, which is a mark and not a grant",
          before_clear.count("return kIOReturnSuccess;") == 1 and "cmd == NVRM_ESC_SURF_DIRTY && (p->plane & NVRM_SS_PLANE_GPU_MARK)" in before_clear)
    check("the mark reads the word and does not write it", "p->pad" in lifted and "p->pad =" not in lifted)
    check("the refusal has its counter, registered with the others",
          source.count("sysctl_register_oid(&sysctl__debug_nvaccel_surf_mark_moved);") == 1
          and "NVACCEL_CNT(nvaccel_surf_mark_moved," in source)
    abi = (ROOT / "kexts" / "NVRM" / "nvrm_surfshare_abi.h").read_text()
    check("the interface says what the word is for in each escape", "#define NVRM_SS_MOVED         0x8000000Au" in abi
          and "NVRM_ESC_SURF_VRAM   [out]" in abi and "NVRM_ESC_SURF_DIRTY  [in]" in abi)

    print("all passed" if not fails else f"{fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
