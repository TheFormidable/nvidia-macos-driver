#!/usr/bin/env python3
"""The CGL adapter reads and writes through private GLEngine offsets the source fork recorded on two macOS builds.
zink/nvk_cgl_guard.h holds the structural checks that nvk_cgl_prepare_gld runs before any such read or write, so that a
build whose layout differs keeps Apple's renderer instead of writing into the wrong memory. This test compiles those
checks for the host and feeds nvk_cgl_check_gld_layout synthetic heap blocks: one laid out as the record says (accepted)
and one per way the layout can differ (each refused with its own reason), with controls. It touches no GPU, loads no
Apple framework and installs nothing, so it is safe to run on any Mac; malloc_size has no Linux name, so a shim behind
the header's two macros uses malloc_usable_size on Linux and malloc_size on a Mac.
usage: test_cgl_gld_guard.py"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ZINK = ROOT / "zink"
fails = 0


def check(what, ok, detail=""):
    global fails
    print(("ok   " if ok else "FAIL ") + what + ("" if ok else f"\n     {detail}"))
    fails += 0 if ok else 1


# The recorded offsets and the SDK sizes, as zink/nvk_cgl.c passes them to the checker. The sizes follow from the public
# headers: struct _CGLContextObject is rend (8) + GLIFunctionDispatch (975 * 8) + priv (8) + stak (8); disp is at 8.
GLI_PROFILE = 0xC6A6
GLI_CGL_DISPATCH = 0xB2C8
GLI_SAVED_DISPATCH = 0xB2D0
DISP_OFFSET = 8
DISPATCH_TABLE_SIZE = 975 * 8          # sizeof(GLIFunctionDispatch)
CGL_CONTEXT_SIZE = 8 + DISPATCH_TABLE_SIZE + 8 + 8   # sizeof(struct _CGLContextObject)

PROGRAM = r"""
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef __APPLE__
#include <malloc/malloc.h>
#define USABLE(p) malloc_size(p)
#else
#include <malloc.h>
#define USABLE(p) malloc_usable_size((void *)(p))
#endif

/* A registry of the synthetic blocks this test allocated. It stands in for the
 * allocator's own knowledge: a block size only for the exact start of a block
 * (as malloc_size gives, and unlike malloc_usable_size on an interior pointer),
 * and a zone membership for any address inside a block. */
#define MAX_BLOCKS 64
static struct { const void *start; size_t size; } g_blocks[MAX_BLOCKS];
static int g_count;
static void reg(const void *p) { g_blocks[g_count].start = p; g_blocks[g_count].size = USABLE(p); g_count++; }
static void reset(void) { g_count = 0; }
static size_t shim_block_size(const void *p) {
   for (int i = 0; i < g_count; ++i) if (g_blocks[i].start == p) return g_blocks[i].size;
   return 0;
}
static int shim_in_zone(const void *p) {
   for (int i = 0; i < g_count; ++i) {
      const char *s = g_blocks[i].start;
      if ((const char *)p >= s && (const char *)p < s + g_blocks[i].size) return 1;
   }
   return 0;
}
#define NVK_CGL_BLOCK_SIZE(p) shim_block_size(p)
#define NVK_CGL_IN_ZONE(p) shim_in_zone(p)
#include "nvk_cgl_guard.h"

#define GLI_PROFILE 0xc6a6u
#define GLI_CGL_DISPATCH 0xb2c8u
#define GLI_SAVED_DISPATCH 0xb2d0u
#define DISP_OFFSET 8u
#define DISPATCH_TABLE_SIZE (975u * 8u)
#define CGL_CONTEXT_SIZE (8u + DISPATCH_TABLE_SIZE + 8u + 8u)
#define ENGINE_SIZE 0x10000u

static void put_ptr(void *base, size_t off, const void *value) { *(const void **)((char *)base + off) = value; }

static enum nvk_cgl_layout run(void *engine, unsigned char *profile, void **ctx, void ***back) {
   *profile = 0; *ctx = NULL; *back = NULL;
   return nvk_cgl_check_gld_layout(engine, GLI_PROFILE, GLI_CGL_DISPATCH, GLI_SAVED_DISPATCH,
                                   DISP_OFFSET, CGL_CONTEXT_SIZE, DISPATCH_TABLE_SIZE, profile, ctx, back);
}

static int failed;
static void expect(const char *name, enum nvk_cgl_layout got, enum nvk_cgl_layout want) {
   int ok = got == want;
   printf("%s %s got=%d want=%d\n", ok ? "ok  " : "FAIL", name, (int)got, (int)want);
   if (!ok) failed = 1;
}

/* Build an engine that passes every check: a big engine block, a CGL context
 * block whose first word is the engine, a saved dispatch block, and the two
 * dispatch pointers wired as the record says. The caller then breaks one thing. */
static void build(void **engine_out, void **context_out, void ***back_out, unsigned char profile) {
   reset();
   char *engine = calloc(1, ENGINE_SIZE);
   char *context = calloc(1, CGL_CONTEXT_SIZE);
   void **back = calloc(1, DISPATCH_TABLE_SIZE);
   reg(engine); reg(context); reg(back);
   engine[GLI_PROFILE] = (char)profile;
   put_ptr(context, 0, engine);                        /* rend */
   put_ptr(engine, GLI_CGL_DISPATCH, context + DISP_OFFSET);
   put_ptr(engine, GLI_SAVED_DISPATCH, back);
   *engine_out = engine; *context_out = context; *back_out = back;
}

int main(void) {
   void *engine, *context; void **back; unsigned char profile; void *octx; void **oback;
   enum nvk_cgl_layout r;

   /* accepted: the recorded layout, core and legacy */
   build(&engine, &context, &back, 1);
   r = run(engine, &profile, &octx, &oback);
   expect("accept_core", r, NVK_CGL_LAYOUT_OK);
   if (r == NVK_CGL_LAYOUT_OK && (profile != 1 || octx != context || oback != back)) {
      printf("FAIL accept_core_outputs profile=%d ctx=%d back=%d\n", profile, octx == context, oback == back);
      failed = 1;
   }
   build(&engine, &context, &back, 0);
   r = run(engine, &profile, &octx, &oback);
   expect("accept_legacy", r, NVK_CGL_LAYOUT_OK);
   if (r == NVK_CGL_LAYOUT_OK && profile != 0) { printf("FAIL accept_legacy_profile\n"); failed = 1; }

   /* refused: the engine is not a heap block at all */
   build(&engine, &context, &back, 1);
   reset(); reg(context); reg(back);   /* engine no longer registered */
   expect("engine_not_block", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_ENGINE_BLOCK);

   /* refused: the engine block is too small to span the recorded reads */
   reset();
   { char *small = calloc(1, 0x100); reg(small);
     expect("engine_too_small", run(small, &profile, &octx, &oback), NVK_CGL_LAYOUT_ENGINE_BLOCK); free(small); }

   /* refused: the profile byte is out of range */
   build(&engine, &context, &back, 2);
   expect("profile_out_of_range", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_PROFILE);

   /* refused: a recorded dispatch pointer is null */
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_CGL_DISPATCH, NULL);
   expect("cgl_dispatch_null", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_NULL);
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, NULL);
   expect("saved_dispatch_null", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_NULL);

   /* refused: the derived context is an interior pointer, not a block start */
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_CGL_DISPATCH, (char *)engine + 0x2000 + DISP_OFFSET);
   expect("context_interior", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_BLOCK);

   /* refused: the context block is too small for the whole struct */
   reset();
   { char *engine2 = calloc(1, ENGINE_SIZE); char *small = calloc(1, 0x100); void **back2 = calloc(1, DISPATCH_TABLE_SIZE);
     reg(engine2); reg(small); reg(back2);
     engine2[GLI_PROFILE] = 1; put_ptr(small, 0, engine2);
     put_ptr(engine2, GLI_CGL_DISPATCH, small + DISP_OFFSET); put_ptr(engine2, GLI_SAVED_DISPATCH, back2);
     expect("context_too_small", run(engine2, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_BLOCK);
     free(engine2); free(small); free(back2); }

   /* refused: the context's first word is not this engine */
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, (char *)engine + 0x40);
   expect("rend_mismatch", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_REND);

   /* refused: the saved table is an interior pointer, not a block start */
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, (char *)back + 16);
   expect("saved_interior", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);

   /* refused: the saved table block is too small */
   reset();
   { char *engine2 = calloc(1, ENGINE_SIZE); char *context2 = calloc(1, CGL_CONTEXT_SIZE); void **small = calloc(1, 0x40);
     reg(engine2); reg(context2); reg(small);
     engine2[GLI_PROFILE] = 1; put_ptr(context2, 0, engine2);
     put_ptr(engine2, GLI_CGL_DISPATCH, context2 + DISP_OFFSET); put_ptr(engine2, GLI_SAVED_DISPATCH, small);
     expect("saved_too_small", run(engine2, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
     free(engine2); free(context2); free(small); }

   printf("%s\n", failed ? "SOME CASES FAILED" : "ALL CASES PASSED");
   return failed ? 1 : 0;
}
"""


def main():
    cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        check("a C compiler is available (CC, cc, gcc or clang)", False, "none found")
        print(f"{fails} failed")
        return 1
    with tempfile.TemporaryDirectory(prefix="cglguard-") as t:
        src = Path(t) / "guard.c"
        src.write_text(PROGRAM)
        binary = Path(t) / "guard"
        build = [cc, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", f"-I{ZINK}", str(src), "-o", str(binary)]
        cp = subprocess.run(build, capture_output=True, text=True)
        check("the guard checks compile for the host with -Wall -Wextra -Werror", cp.returncode == 0,
              cp.stderr.strip())
        if cp.returncode != 0:
            print(f"{fails} failed")
            return 1
        rp = subprocess.run([str(binary)], capture_output=True, text=True)
        print(rp.stdout, end="")
        if rp.stderr.strip():
            print(rp.stderr, end="")
        out = rp.stdout
        cases = {ln.split()[1] for ln in out.splitlines() if ln.startswith(("ok", "FAIL")) and len(ln.split()) > 1}
        expected = {"accept_core", "accept_legacy", "engine_not_block", "engine_too_small", "profile_out_of_range",
                    "cgl_dispatch_null", "saved_dispatch_null", "context_interior", "context_too_small",
                    "rend_mismatch", "saved_interior", "saved_too_small"}
        check("the recorded layout is accepted and each mismatch has its own case",
              expected.issubset(cases), sorted(expected - cases))
        bad = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
        check("every synthetic layout yields the expected verdict", not bad, "\n".join(bad))
        check("the C program reports all cases passed", "ALL CASES PASSED" in out and rp.returncode == 0)

    # the source wires the checks: prepare_gld refuses on any nonzero verdict and never reads or writes first
    cgl = (ZINK / "nvk_cgl.c").read_text()
    check("nvk_cgl.c runs nvk_cgl_check_gld_layout in prepare_gld and returns 0 on a mismatch",
          "nvk_cgl_check_gld_layout(engine" in cgl
          and re.search(r"if \(layout != NVK_CGL_LAYOUT_OK\).*?return 0;", cgl, re.S) is not None)
    check("nvk_cgl.c reads neither the profile byte nor the dispatch pointers before the check",
          "*(unsigned char *)((char *)engine + GLI_PROFILE)" not in cgl
          and "*(void **)((char *)engine + GLI_CGL_DISPATCH)" not in cgl)
    m = re.search(r"EXPORT int nvk_cgl_prepare_gld\(.*?\n\}\n", cgl, re.S)
    body = m.group(0) if m else ""
    check("prepare_gld proves the layout before it makes the Zink screen, so a refusal opens no NVK device",
          0 <= body.find("nvk_cgl_check_gld_layout(") < body.find("nvk_zink_screen_ready()")
          and body.count("nvk_zink_screen_ready()") == 1, body[:200])
    check("integer_dispatch_slot checks the CGL private object and engine list are heap blocks",
          cgl.count("nvk_cgl_block_spans(meta") == 1 and cgl.count("nvk_cgl_block_spans(items") == 1)
    check("a refusal is logged once per process on stderr",
          "atomic_flag" in cgl and "layout_refused_once" in cgl)
    guard = (ZINK / "nvk_cgl_guard.h").read_text()
    check("the guard header derives the block-span check from the two run-time facts the includer supplies",
          "#error" in guard and "NVK_CGL_BLOCK_SIZE(p)" in guard and "NVK_CGL_IN_ZONE(p)" in guard)

    print(f"{fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
