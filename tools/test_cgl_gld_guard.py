#!/usr/bin/env python3
"""The CGL adapter reads and writes through private GLEngine offsets the source fork recorded on two macOS builds.
zink/nvk_cgl_guard.h holds the structural checks that nvk_cgl_prepare_gld runs before any such read or write, so that a
build whose layout differs keeps Apple's renderer instead of writing into the wrong memory. This test compiles those
checks for the host and feeds nvk_cgl_check_gld_layout synthetic heap blocks and VM regions: layouts the record allows
(accepted: the CGL context's rend null, this engine, or no heap object in heap or unmapped memory; the saved table its
own block, past the context's public part or a span of heap memory at no block start that reaches neither block) and
one per way the layout can differ (each refused, naming what the memory held), with controls. It also feeds
nvk_cgl_room, the question integer_dispatch_slot asks of the CGL private object and of its engine list,
nvk_cgl_renderer_reason, the text of the line bridge_current prints when it drops a call, and nvk_cgl_further_engine,
which says whether an engine is a later engine of a context the adapter adopted or left unadopted. zink/nvk_cgl.c
compiles only against the macOS SDK, so what it does around those checks (what prepare reads before the check, where
each refusal returns, what is printed and how often, what bridge_current does with a call that came with another
engine) is checked by reading its text. It touches no GPU, loads no Apple framework and installs nothing, so it is
safe to run on any Mac; malloc_size has no Linux name, so a shim behind the header's macros uses malloc_usable_size
on Linux and malloc_size on a Mac, and a table of synthetic regions stands in for the VM map.
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


def braced(text, header):
    """The body of the brace block that header (ending in its opening brace) starts in text, or "" if absent."""
    start = text.find(header)
    if start < 0 or not header.endswith("{"):
        return ""
    depth, i = 1, start + len(header)
    while i < len(text) and depth:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    return text[start + len(header):i - 1] if depth == 0 else ""


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
static int g_count, g_region_count;
static void reg(const void *p) { g_blocks[g_count].start = p; g_blocks[g_count].size = USABLE(p); g_count++; }
/* A block of size bytes at p, carved out of a larger allocation the registry does not hold, so that the bytes
 * before it are heap memory that starts no block. */
static void reg_span(const void *p, size_t size) { g_blocks[g_count].start = p; g_blocks[g_count].size = size; g_count++; }
static void reset(void) { g_count = 0; g_region_count = 0; }
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
/* A table of synthetic VM regions stands in for the task's VM map; an address
 * in none of them is unmapped. */
struct nvk_cgl_vm;
static int shim_vm_region(const void *p, struct nvk_cgl_vm *v);
#define NVK_CGL_BLOCK_SIZE(p) shim_block_size(p)
#define NVK_CGL_IN_ZONE(p) shim_in_zone(p)
#define NVK_CGL_VM_REGION(p, v) shim_vm_region((p), (v))
#include "nvk_cgl_guard.h"
#define MAX_REGIONS 16
static struct { const char *start; size_t size; unsigned tag; int prot; } g_regions[MAX_REGIONS];
static void region(const void *start, size_t size, unsigned tag, int prot) {
   g_regions[g_region_count].start = start; g_regions[g_region_count].size = size;
   g_regions[g_region_count].tag = tag; g_regions[g_region_count].prot = prot; g_region_count++;
}
static int shim_vm_region(const void *p, struct nvk_cgl_vm *v) {
   for (int i = 0; i < g_region_count; ++i) {
      const char *s = g_regions[i].start;
      if ((const char *)p >= s && (const char *)p < s + g_regions[i].size) {
         v->tag = g_regions[i].tag; v->prot = g_regions[i].prot;
         v->avail = (size_t)(s + g_regions[i].size - (const char *)p); return 1;
      }
   }
   return 0;
}
#define TAG_MALLOC_SMALL 2u   /* VM_MEMORY_MALLOC_SMALL */
#define TAG_ANALYSIS_TOOL 10u /* VM_MEMORY_ANALYSIS_TOOL, between the malloc tags and not one */
#define TAG_NONE 0u           /* an image's data or text */
#define TAG_STACK 30u         /* VM_MEMORY_STACK */
#define PROT_RW 3

#define GLI_PROFILE 0xc6a6u
#define GLI_CGL_DISPATCH 0xb2c8u
#define GLI_SAVED_DISPATCH 0xb2d0u
#define DISP_OFFSET 8u
#define DISPATCH_TABLE_SIZE (975u * 8u)
#define CGL_CONTEXT_SIZE (8u + DISPATCH_TABLE_SIZE + 8u + 8u)
#define ENGINE_SIZE 0x10000u


static void put_ptr(void *base, size_t off, const void *value) { *(const void **)((char *)base + off) = value; }

static struct nvk_cgl_layout_report g_report;
static enum nvk_cgl_layout run(void *engine, unsigned char *profile, void **ctx, void ***back) {
   *profile = 0xee; *ctx = NULL; *back = NULL;
   return nvk_cgl_check_gld_layout(engine, GLI_PROFILE, GLI_CGL_DISPATCH, GLI_SAVED_DISPATCH,
                                   DISP_OFFSET, CGL_CONTEXT_SIZE, DISPATCH_TABLE_SIZE, profile, ctx, back,
                                   &g_report);
}

static int failed;
static void expect(const char *name, enum nvk_cgl_layout got, enum nvk_cgl_layout want) {
   int ok = got == want && g_report.layout == got;
   printf("%s %s got=%d want=%d\n", ok ? "ok  " : "FAIL", name, (int)got, (int)want);
   if (!ok) failed = 1;
}
/* The outputs are set exactly on acceptance and untouched on refusal. */
static void expect_out(const char *name, enum nvk_cgl_layout got, unsigned char profile, void *ctx, void **back,
                       unsigned char want_profile, void *want_ctx, void **want_back) {
   int ok = got == NVK_CGL_LAYOUT_OK ? profile == want_profile && ctx == want_ctx && back == want_back
                                     : profile == 0xee && !ctx && !back;
   printf("%s %s\n", ok ? "ok  " : "FAIL", name);
   if (!ok) failed = 1;
}
/* The refusal text holds want (a fragment) and no parenthesis. */
static void expect_reason(const char *name, const char *want) {
   char why[768];
   nvk_cgl_layout_reason(why, sizeof(why), &g_report);
   int ok = strstr(why, want) && !strchr(why, '(') && !strchr(why, ')');
   printf("%s %s why=%s\n", ok ? "ok  " : "FAIL", name, why);
   if (!ok) failed = 1;
}

static size_t usable(const void *p) { return shim_block_size(p); }
/* The refusal text is exactly want. */
static void expect_reason_is(const char *name, const char *want) {
   char why[768];
   nvk_cgl_layout_reason(why, sizeof(why), &g_report);
   int ok = !strcmp(why, want);
   printf("%s %s why=%s\n", ok ? "ok  " : "FAIL", name, why);
   if (!ok) { printf("     want=%s\n", want); failed = 1; }
}
static void expect_text(const char *name, const char *got, const char *want) {
   int ok = !strcmp(got, want) && !strchr(got, '(') && !strchr(got, ')');
   printf("%s %s got=%s\n", ok ? "ok  " : "FAIL", name, got);
   if (!ok) { printf("     want=%s\n", want); failed = 1; }
}
static void expect_room(const char *name, size_t got, size_t want, const void *block, const void *want_block,
                        size_t block_size, size_t want_block_size) {
   int ok = got == want && block == want_block && block_size == want_block_size;
   printf("%s %s got=0x%zx want=0x%zx\n", ok ? "ok  " : "FAIL", name, got, want);
   if (!ok) failed = 1;
}
static void expect_int(const char *name, int got, int want) {
   printf("%s %s got=%d want=%d\n", got == want ? "ok  " : "FAIL", name, got, want);
   if (got != want) failed = 1;
}
static long g_static_words[1024];

/* Build an engine that passes every check: a big engine block, a CGL context
 * block of context_size bytes whose first word is rend, a saved dispatch block,
 * and the two dispatch pointers wired as the record says. */
static void build_ex(void **engine_out, void **context_out, void ***back_out, unsigned char profile,
                     size_t context_size, int rend_is_engine) {
   reset();
   char *engine = calloc(1, ENGINE_SIZE);
   char *context = calloc(1, context_size);
   void **back = calloc(1, DISPATCH_TABLE_SIZE);
   reg(engine); reg(context); reg(back);
   engine[GLI_PROFILE] = (char)profile;
   put_ptr(context, 0, rend_is_engine ? engine : NULL);   /* rend */
   put_ptr(engine, GLI_CGL_DISPATCH, context + DISP_OFFSET);
   put_ptr(engine, GLI_SAVED_DISPATCH, back);
   *engine_out = engine; *context_out = context; *back_out = back;
}
static void build(void **e, void **c, void ***b, unsigned char profile) {
   build_ex(e, c, b, profile, CGL_CONTEXT_SIZE, 1);
}
static void build_null(void **e, void **c, void ***b, unsigned char profile) {
   build_ex(e, c, b, profile, CGL_CONTEXT_SIZE, 0);
}
/* Another engine-sized block; wired = its CGL dispatch pointer is this context's. */
static void *other_engine(void *context, int wired) {
   char *e2 = calloc(1, ENGINE_SIZE); reg(e2);
   if (wired) put_ptr(e2, GLI_CGL_DISPATCH, (char *)context + DISP_OFFSET);
   return e2;
}

int main(void) {
   void *engine, *context; void **back; unsigned char profile; void *octx; void **oback;
   enum nvk_cgl_layout r;

   /* accepted: rend is this engine (core, legacy) or null (core, legacy) */
   build(&engine, &context, &back, 1);
   r = run(engine, &profile, &octx, &oback);
   expect("accept_core", r, NVK_CGL_LAYOUT_OK);
   expect_out("accept_core_outputs", r, profile, octx, oback, 1, context, back);
   build(&engine, &context, &back, 0);
   r = run(engine, &profile, &octx, &oback);
   expect("accept_legacy", r, NVK_CGL_LAYOUT_OK);
   expect_out("accept_legacy_outputs", r, profile, octx, oback, 0, context, back);
   build_null(&engine, &context, &back, 1);
   r = run(engine, &profile, &octx, &oback);
   expect("accept_rend_null", r, NVK_CGL_LAYOUT_OK);
   expect_out("accept_rend_null_outputs", r, profile, octx, oback, 1, context, back);
   build_null(&engine, &context, &back, 0);
   r = run(engine, &profile, &octx, &oback);
   expect("accept_rend_null_legacy", r, NVK_CGL_LAYOUT_OK);
   expect_out("accept_rend_null_legacy_outputs", r, profile, octx, oback, 0, context, back);

   /* accepted: rend is left over in a block CGL has not cleared, and is no heap object; it lies in no mapped
    * region or in heap memory */
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, g_static_words);
   expect("accept_rend_loose", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_OK);
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, (char *)back + 16);   /* inside a block, at no block start: a free-list link's shape */
   expect("accept_rend_interior_other", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_OK);
   build(&engine, &context, &back, 1);
   region(back, DISPATCH_TABLE_SIZE, TAG_MALLOC_SMALL, PROT_RW);
   put_ptr(context, 0, (char *)back + 16);
   expect("accept_rend_heap_interior", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_OK);
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, (void *)(uintptr_t)0x8);   /* aligned, below the first page */
   expect("accept_rend_low_aligned", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_OK);
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, (void *)(uintptr_t)0x4141414141414141ull);
   expect("accept_rend_wild", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_OK);
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, (void *)(uintptr_t)1);
   expect("accept_rend_one", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_OK);
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, (void *)(uintptr_t)0xaaaaaaaaaaaaaaaaull);   /* MallocPreScribble's fill of a new block */
   expect("accept_rend_prescribble", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_OK);

   /* accepted: the saved table past the context's public part, inside a block this check cannot see, or static */
   build_ex(&engine, &context, &back, 1, CGL_CONTEXT_SIZE + DISPATCH_TABLE_SIZE + 0x40, 0);
   put_ptr(engine, GLI_SAVED_DISPATCH, (char *)context + CGL_CONTEXT_SIZE);
   r = run(engine, &profile, &octx, &oback);
   expect("accept_saved_past_context_public", r, NVK_CGL_LAYOUT_OK);
   expect_out("accept_saved_past_context_public_outputs", r, profile, octx, oback, 1, context,
              (void **)((char *)context + CGL_CONTEXT_SIZE));
   build(&engine, &context, &back, 1);
   { char *priv = calloc(1, 0x4000); reg(priv); region(priv, 0x4000, TAG_MALLOC_SMALL, PROT_RW);
     put_ptr(engine, GLI_SAVED_DISPATCH, priv + 0x100);
     r = run(engine, &profile, &octx, &oback);
     expect("accept_saved_heap_interior", r, NVK_CGL_LAYOUT_OK);
     expect_out("accept_saved_heap_interior_outputs", r, profile, octx, oback, 1, context, (void **)(priv + 0x100));
     free(priv); }

   /* refused: the engine is not a heap block at all, or too small */
   build(&engine, &context, &back, 1);
   reset(); reg(context); reg(back);   /* engine not registered */
   r = run(engine, &profile, &octx, &oback);
   expect("engine_not_block", r, NVK_CGL_LAYOUT_ENGINE_BLOCK);
   expect_out("engine_not_block_outputs", r, profile, octx, oback, 0, NULL, NULL);
   expect_reason("reason_engine_not_block", "this GLI engine starts no heap block");
   reset();
   { char *small = calloc(1, 0x100); reg(small);
     expect("engine_too_small", run(small, &profile, &octx, &oback), NVK_CGL_LAYOUT_ENGINE_BLOCK);
     expect_reason("reason_engine_too_small", "too small for the recorded reads"); free(small); }

   /* refused: the profile byte is out of range */
   build(&engine, &context, &back, 2);
   expect("profile_out_of_range", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_PROFILE);
   expect_reason("reason_profile", "the GLI profile byte is 2, neither 0 for legacy nor 1 for core");

   /* refused: a recorded dispatch pointer is null */
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_CGL_DISPATCH, NULL);
   expect("cgl_dispatch_null", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_NULL);
   expect_reason("reason_cgl_dispatch_null", "the GLI engine's CGL dispatch pointer is null");
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_CGL_DISPATCH, NULL); put_ptr(engine, GLI_SAVED_DISPATCH, NULL);
   expect("both_dispatch_null", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_NULL);
   expect_reason("reason_both_dispatch_null", "the GLI engine's CGL and saved dispatch pointers are null");
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, NULL);
   expect("saved_dispatch_null", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_NULL);
   expect_reason("reason_saved_dispatch_null", "the GLI engine's saved dispatch pointer is null");

   /* refused: the derived context is the engine, inside it, too small or no heap block */
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_CGL_DISPATCH, (char *)engine + 0x2000 + DISP_OFFSET);
   expect("context_interior", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_BLOCK);
   expect_reason("reason_context_interior",
                 "the CGL context derived from the GLI engine's CGL dispatch pointer is inside this GLI engine at "
                 "+0x2000, not a heap block of its own");
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_CGL_DISPATCH, (char *)engine + DISP_OFFSET);
   expect("context_is_engine", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_BLOCK);
   expect_reason("reason_context_is_engine",
                 "the CGL context derived from the GLI engine's CGL dispatch pointer is this GLI engine, not a heap");
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_CGL_DISPATCH, (char *)g_static_words + DISP_OFFSET);
   expect("context_not_heap", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_BLOCK);
   expect_reason("reason_context_not_heap", "which starts no heap block");
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_CGL_DISPATCH, (char *)context + DISP_OFFSET + 4);
   expect("context_wild", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_BLOCK);
   expect_reason("reason_context_wild", "which is no aligned user-space address, not a heap block of its own");
   reset();
   { char *engine2 = calloc(1, ENGINE_SIZE); char *small = calloc(1, 0x100);
     void **back2 = calloc(1, DISPATCH_TABLE_SIZE);
     reg(engine2); reg(small); reg(back2);
     engine2[GLI_PROFILE] = 1; put_ptr(small, 0, engine2);
     put_ptr(engine2, GLI_CGL_DISPATCH, small + DISP_OFFSET); put_ptr(engine2, GLI_SAVED_DISPATCH, back2);
     r = run(engine2, &profile, &octx, &oback);
     expect("context_too_small", r, NVK_CGL_LAYOUT_CONTEXT_BLOCK);
     expect_out("context_too_small_outputs", r, profile, octx, oback, 0, NULL, NULL);
     { char want[160];
       snprintf(want, sizeof(want), "the CGL context derived from the GLI engine's CGL dispatch pointer is a heap "
                "block of 0x%zx bytes, not a heap block of its own", usable(small));
       expect_reason("reason_context_too_small", want); }
     free(engine2); free(small); free(back2); }

   /* refused: rend is a heap object other than this engine */
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, (char *)engine + 0x40);
   r = run(engine, &profile, &octx, &oback);
   expect("rend_mismatch", r, NVK_CGL_LAYOUT_CONTEXT_REND);
   expect_out("rend_mismatch_outputs", r, profile, octx, oback, 0, NULL, NULL);
   expect_reason("reason_rend_engine_interior", "the CGL context's renderer word is inside this GLI engine at +0x40; "
                 "the saved dispatch table is a heap block of 0x");
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, other_engine(context, 1));
   expect("rend_sibling", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_REND);
   expect_reason("reason_rend_sibling",
                 "the CGL context's renderer word is another GLI engine of this CGL context, a heap block of 0x");
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, other_engine(context, 0));
   expect("rend_engine_sized_block", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_REND);
   expect_reason("reason_rend_engine_sized_block", "the CGL context's renderer word is a heap block of 0x");
   build(&engine, &context, &back, 1);
   { char *small = calloc(1, 0x100); reg(small); put_ptr(context, 0, small);
     expect("rend_small_block", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_REND); free(small); }
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, context);
   expect("rend_context", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_REND);
   expect_reason("reason_rend_context", "the CGL context's renderer word is inside the CGL context at +0x0;");
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, (char *)context + 0x10);
   expect("rend_context_interior", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_REND);
   /* rend points into an image's data or a stack: a vtable or class pointer of a foreign object */
   build(&engine, &context, &back, 1);
   region(g_static_words, sizeof(g_static_words), TAG_NONE, 1);
   put_ptr(context, 0, g_static_words + 4);
   expect("rend_image", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_REND);
   expect_reason("reason_rend_image", "and lies in a region of VM tag 0 with protection 1, 0x");
   build(&engine, &context, &back, 1);
   region(g_static_words, sizeof(g_static_words), TAG_STACK, PROT_RW);
   put_ptr(context, 0, g_static_words);
   expect("rend_stack", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_REND);
   /* a rend refusal names where the saved table is too, here a place the saved check refuses */
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, (char *)engine + 0x40);
   put_ptr(engine, GLI_SAVED_DISPATCH, (char *)context + DISP_OFFSET);
   expect("rend_and_saved_bad", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_REND);
   expect_reason("reason_rend_and_saved_bad", "the saved dispatch table is inside the CGL context at +0x8;");

   /* refused: the saved table is where the record rules a GLIFunctionDispatch out */
   reset();
   { char *engine2 = calloc(1, ENGINE_SIZE); char *context2 = calloc(1, CGL_CONTEXT_SIZE);
     void **small = calloc(1, 0x40);
     reg(engine2); reg(context2); reg(small);
     engine2[GLI_PROFILE] = 1; put_ptr(context2, 0, engine2);
     put_ptr(engine2, GLI_CGL_DISPATCH, context2 + DISP_OFFSET); put_ptr(engine2, GLI_SAVED_DISPATCH, small);
     r = run(engine2, &profile, &octx, &oback);
     expect("saved_too_small", r, NVK_CGL_LAYOUT_SAVED_BLOCK);
     expect_out("saved_too_small_outputs", r, profile, octx, oback, 0, NULL, NULL);
     expect_reason("reason_saved_too_small", "the saved dispatch table is a heap block of 0x");
     free(engine2); free(context2); free(small); }
   build_null(&engine, &context, &back, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, context);
   expect("saved_is_context", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   build_null(&engine, &context, &back, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, (char *)context + DISP_OFFSET);
   expect("saved_is_context_disp", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   expect_reason("reason_saved_is_context_disp",
                 "the saved dispatch table is inside the CGL context at +0x8, where the record rules out a "
                 "GLIFunctionDispatch of 0x1e78 bytes; the CGL context's renderer word is null");
   build_ex(&engine, &context, &back, 1, CGL_CONTEXT_SIZE + DISPATCH_TABLE_SIZE + 0x40, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, (char *)context + ((usable(context) - 0x100) & ~(size_t)7));
   expect("saved_past_context_end", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, engine);
   expect("saved_is_engine", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   expect_reason("reason_saved_is_engine", "the saved dispatch table is this GLI engine,");
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, (char *)engine + 0x100);
   expect("saved_in_engine", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   expect_reason("reason_saved_in_engine", "the saved dispatch table is inside this GLI engine at +0x100,");
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, other_engine(context, 1));
   expect("saved_sibling", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, (char *)back + 4);
   expect("saved_unaligned", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   expect_reason("reason_saved_unaligned", "which is no aligned user-space address, where the record rules out");
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, (void *)(uintptr_t)0x4141414141414140ull);
   expect("saved_wild", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   /* refused: a span at no block start that is not readable, writable heap memory to its end */
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, g_static_words);
   r = run(engine, &profile, &octx, &oback);
   expect("saved_unmapped", r, NVK_CGL_LAYOUT_SAVED_BLOCK);
   expect_out("saved_unmapped_outputs", r, profile, octx, oback, 0, NULL, NULL);
   expect_reason("reason_saved_unmapped", "and lies in no mapped region, where the record rules out");
   build(&engine, &context, &back, 1);
   region(g_static_words, sizeof(g_static_words), TAG_NONE, PROT_RW);
   put_ptr(engine, GLI_SAVED_DISPATCH, g_static_words);
   expect("saved_image_data", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   expect_reason("reason_saved_image_data", "lies in a region of VM tag 0 with protection 3, 0x2000 bytes before");
   build(&engine, &context, &back, 1);
   region(g_static_words, sizeof(g_static_words), TAG_ANALYSIS_TOOL, PROT_RW);
   put_ptr(engine, GLI_SAVED_DISPATCH, g_static_words);
   expect("saved_analysis_tool", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   build(&engine, &context, &back, 1);
   region(g_static_words, sizeof(g_static_words), TAG_MALLOC_SMALL, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, g_static_words);
   expect("saved_heap_readonly", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   build(&engine, &context, &back, 1);
   region(g_static_words, sizeof(g_static_words), TAG_MALLOC_SMALL, PROT_RW);
   put_ptr(engine, GLI_SAVED_DISPATCH, (char *)g_static_words + sizeof(g_static_words) - DISPATCH_TABLE_SIZE + 8);
   expect("saved_heap_short", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   expect_reason("reason_saved_heap_short", "lies in a region of VM tag 2 with protection 3, 0x1e70 bytes before");
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, (char *)g_static_words + sizeof(g_static_words) - DISPATCH_TABLE_SIZE);
   region(g_static_words, sizeof(g_static_words), TAG_MALLOC_SMALL, PROT_RW);
   expect("accept_saved_heap_exact", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_OK);

   /* refused: a span of heap memory at no block start that would reach the start of the GLI engine or of the CGL
    * context before a GLIFunctionDispatch ends; accepted when it ends exactly where that block starts */
   reset();
   { char *arena = calloc(1, 0x40000); char *e = arena + 0x8000; char *c = arena + 0x20000;
     void **b2 = calloc(1, DISPATCH_TABLE_SIZE);
     reg_span(e, ENGINE_SIZE); reg_span(c, CGL_CONTEXT_SIZE); reg(b2);
     region(arena, 0x40000, TAG_MALLOC_SMALL, PROT_RW);
     e[GLI_PROFILE] = 1; put_ptr(e, GLI_CGL_DISPATCH, c + DISP_OFFSET);
     put_ptr(e, GLI_SAVED_DISPATCH, e - 0x1000);
     r = run(e, &profile, &octx, &oback);
     expect("saved_runs_into_engine", r, NVK_CGL_LAYOUT_SAVED_BLOCK);
     expect_out("saved_runs_into_engine_outputs", r, profile, octx, oback, 0, NULL, NULL);
     expect_reason("reason_saved_runs_into_engine",
                   "GLIFunctionDispatch of 0x1e78 bytes, which would run into this GLI engine; the CGL context's");
     put_ptr(e, GLI_SAVED_DISPATCH, e - 8);
     expect("saved_runs_into_engine_last_word", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
     put_ptr(e, GLI_SAVED_DISPATCH, c - 0x1000);
     expect("saved_runs_into_context", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
     expect_reason("reason_saved_runs_into_context",
                   "GLIFunctionDispatch of 0x1e78 bytes, which would run into the CGL context; the CGL context's");
     put_ptr(e, GLI_SAVED_DISPATCH, e - DISPATCH_TABLE_SIZE);
     r = run(e, &profile, &octx, &oback);
     expect("accept_saved_ends_at_engine", r, NVK_CGL_LAYOUT_OK);
     expect_out("accept_saved_ends_at_engine_outputs", r, profile, octx, oback, 1, c, (void **)(e - DISPATCH_TABLE_SIZE));
     put_ptr(e, GLI_SAVED_DISPATCH, c - DISPATCH_TABLE_SIZE);
     expect("accept_saved_ends_at_context", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_OK);
     put_ptr(e, GLI_SAVED_DISPATCH, c + CGL_CONTEXT_SIZE);   /* after the context's block, inside neither */
     expect("accept_saved_after_context_block", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_OK);
     put_ptr(e, GLI_SAVED_DISPATCH, e - 0x1000);             /* a refusal of rend names the overlap of the table too */
     put_ptr(c, 0, e + 0x40);
     expect("rend_bad_and_saved_runs_into_engine", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_REND);
     free(arena); free(b2); }

   /* the sizes a refusal prints are those of the blocks: rend, the engine, the context and the saved table are
    * blocks of four different sizes (the table's block here is 0x400 bytes larger than a GLIFunctionDispatch, so
    * that an allocator with 512-byte size classes does not round it to the context's size) */
   build(&engine, &context, &back, 1);
   { char *big = calloc(1, ENGINE_SIZE + 0x400); reg(big); put_ptr(context, 0, big);
     void **wide = calloc(1, DISPATCH_TABLE_SIZE + 0x400); reg(wide); put_ptr(engine, GLI_SAVED_DISPATCH, wide);
     char want[512];
     expect("rend_block_sizes", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_REND);
     snprintf(want, sizeof(want), "the CGL context's renderer word is a heap block of 0x%zx bytes; the saved dispatch "
              "table is a heap block of 0x%zx bytes; this GLI engine is a heap block of 0x%zx bytes and the CGL context "
              "one of 0x%zx bytes", usable(big), usable(wide), usable(engine), usable(context));
     expect_reason_is("reason_rend_block_sizes", want);
     int distinct = usable(big) != usable(engine) && usable(engine) != usable(context) && usable(context) != usable(wide)
                    && usable(wide) != usable(big) && usable(wide) != usable(engine) && usable(big) != usable(context);
     printf("%s reason_sizes_distinct\n", distinct ? "ok  " : "FAIL"); if (!distinct) failed = 1;
     put_ptr(context, 0, engine);
     { void **small = calloc(1, 0x40); reg(small); put_ptr(engine, GLI_SAVED_DISPATCH, small);
       expect("saved_small_sizes", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
       snprintf(want, sizeof(want), "the saved dispatch table is a heap block of 0x%zx bytes, where the record rules "
                "out a GLIFunctionDispatch of 0x1e78 bytes; the CGL context's renderer word is this GLI engine; this "
                "GLI engine is a heap block of 0x%zx bytes and the CGL context one of 0x%zx bytes", usable(small),
                usable(engine), usable(context));
       expect_reason_is("reason_saved_small_sizes", want); free(small); }
     free(big); free(wide); }

   /* a dispatch word of 8 derives a null context, said as null */
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_CGL_DISPATCH, (void *)(uintptr_t)DISP_OFFSET);
   expect("context_null_from_dispatch_8", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_BLOCK);
   expect_reason_is("reason_context_null_from_dispatch_8", "the CGL context derived from the GLI engine's CGL dispatch "
                    "pointer is null, not a heap block of its own holding struct _CGLContextObject");

   /* an unaligned rend inside the engine or the context is a pointer into that block, refused */
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, (char *)engine + 4);
   expect("rend_engine_unaligned", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_REND);
   expect_reason("reason_rend_engine_unaligned", "the CGL context's renderer word is inside this GLI engine at +0x4;");
   build(&engine, &context, &back, 1);
   put_ptr(context, 0, (char *)context + 4);
   expect("rend_context_unaligned", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_REND);
   expect_reason("reason_rend_context_unaligned", "the CGL context's renderer word is inside the CGL context at +0x4;");

   /* where no aligned user address begins and ends: 0xff8 and the top of user space are none, 0x1000 and the
    * word below the top are addresses in no mapped region */
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, (void *)(uintptr_t)0xff8);
   expect("saved_below_first_page", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   expect_reason("reason_saved_below_first_page", "the saved dispatch table is 0xff8, which is no aligned user-space");
   put_ptr(engine, GLI_SAVED_DISPATCH, (void *)(uintptr_t)0x1000);
   expect("saved_first_page", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   expect_reason("reason_saved_first_page", "the saved dispatch table is 0x1000, which starts no heap block,");
   put_ptr(engine, GLI_SAVED_DISPATCH, (void *)(uintptr_t)0x00007fffffe00000ull);
   expect("saved_user_top", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   expect_reason("reason_saved_user_top", "the saved dispatch table is 0x7fffffe00000, which is no aligned user-space");
   put_ptr(engine, GLI_SAVED_DISPATCH, (void *)(uintptr_t)0x00007fffffdffff8ull);
   expect("saved_below_user_top", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   expect_reason("reason_saved_below_user_top", "the saved dispatch table is 0x7fffffdffff8, which starts no heap block,");
   put_ptr(engine, GLI_SAVED_DISPATCH, (void *)(uintptr_t)0x4141414141414140ull);
   expect("saved_wild_reason", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
   expect_reason("reason_saved_wild", "the saved dispatch table is 0x4141414141414140, which is no aligned user-space");

   /* blocks carved at exact sizes: the context's block ends exactly where the table past its public part ends
    * (accepted) or one word earlier (refused); an unaligned table there is refused; a block that starts where the
    * engine's block ends is a block of its own */
   reset();
   { char *arena = calloc(1, 0x40000); char *e = arena + 0x8000; char *c = arena + 0x20000;
     reg_span(e, ENGINE_SIZE); reg_span(c, CGL_CONTEXT_SIZE + DISPATCH_TABLE_SIZE);
     e[GLI_PROFILE] = 1; put_ptr(e, GLI_CGL_DISPATCH, c + DISP_OFFSET);
     put_ptr(e, GLI_SAVED_DISPATCH, c + CGL_CONTEXT_SIZE);
     r = run(e, &profile, &octx, &oback);
     expect("accept_saved_fills_context_tail", r, NVK_CGL_LAYOUT_OK);
     expect_out("accept_saved_fills_context_tail_outputs", r, profile, octx, oback, 1, c, (void **)(c + CGL_CONTEXT_SIZE));
     put_ptr(e, GLI_SAVED_DISPATCH, c + CGL_CONTEXT_SIZE + 8);
     expect("saved_context_tail_one_word_short", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
     put_ptr(e, GLI_SAVED_DISPATCH, c + CGL_CONTEXT_SIZE - 8);
     expect("saved_context_tail_over_public", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
     reset(); reg_span(e, ENGINE_SIZE); reg_span(c, CGL_CONTEXT_SIZE + DISPATCH_TABLE_SIZE + 0x40);
     put_ptr(e, GLI_SAVED_DISPATCH, c + CGL_CONTEXT_SIZE + 3);
     r = run(e, &profile, &octx, &oback);
     expect("saved_context_tail_unaligned", r, NVK_CGL_LAYOUT_SAVED_BLOCK);
     expect_out("saved_context_tail_unaligned_outputs", r, profile, octx, oback, 0, NULL, NULL);
     expect_reason("reason_saved_context_tail_unaligned", "the saved dispatch table is inside the CGL context at +0x1e93,");
     put_ptr(e, GLI_SAVED_DISPATCH, c + CGL_CONTEXT_SIZE + 4);   /* aligned for an int, not for a pointer */
     expect("saved_context_tail_half_aligned", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
     reg_span(e + ENGINE_SIZE, DISPATCH_TABLE_SIZE);
     put_ptr(e, GLI_SAVED_DISPATCH, e + ENGINE_SIZE);
     r = run(e, &profile, &octx, &oback);
     expect("accept_saved_block_after_engine", r, NVK_CGL_LAYOUT_OK);
     expect_out("accept_saved_block_after_engine_outputs", r, profile, octx, oback, 1, c, (void **)(e + ENGINE_SIZE));
     put_ptr(c, 0, e + ENGINE_SIZE);   /* and as rend that block is another heap block, refused */
     expect("rend_block_after_engine", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_REND);
     put_ptr(c, 0, NULL);
     put_ptr(e, GLI_SAVED_DISPATCH, e - DISPATCH_TABLE_SIZE + 8);   /* its last word would be the engine's first */
     region(arena, 0x40000, TAG_MALLOC_SMALL, PROT_RW);
     expect("saved_runs_into_engine_one_word", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
     expect_reason("reason_saved_runs_into_engine_one_word", "which would run into this GLI engine;");
     put_ptr(e, GLI_SAVED_DISPATCH, c - DISPATCH_TABLE_SIZE + 8);
     expect("saved_runs_into_context_one_word", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
     expect_reason("reason_saved_runs_into_context_one_word", "which would run into the CGL context;");
     /* a small heap block that ends where the engine starts is refused for its size, and only for that */
     reg_span(e - 0x40, 0x40);
     put_ptr(e, GLI_SAVED_DISPATCH, e - 0x40);
     expect("saved_small_below_engine", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_SAVED_BLOCK);
     { char want[512];
       snprintf(want, sizeof(want), "the saved dispatch table is a heap block of 0x40 bytes, where the record rules "
                "out a GLIFunctionDispatch of 0x1e78 bytes; the CGL context's renderer word is null; this GLI engine "
                "is a heap block of 0x%zx bytes and the CGL context one of 0x%zx bytes", usable(e), usable(c));
       expect_reason_is("reason_saved_small_below_engine", want); }
     free(arena); }

   /* the engine's block holds the profile byte, the highest recorded read, exactly (accepted) or ends one byte
    * before it (refused); a derived context that is itself as large as an engine is a block like any other */
   reset();
   { char *arena = calloc(1, 0x40000); char *e = arena + 0x8000; char *c = calloc(1, ENGINE_SIZE);
     void **b2 = calloc(1, DISPATCH_TABLE_SIZE);
     reg_span(e, GLI_PROFILE + 1); reg(c); reg(b2);
     e[GLI_PROFILE] = 1; put_ptr(e, GLI_CGL_DISPATCH, c + DISP_OFFSET); put_ptr(e, GLI_SAVED_DISPATCH, b2);
     expect("accept_engine_exact", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_OK);
     put_ptr(c, GLI_CGL_DISPATCH, (void *)(uintptr_t)DISP_OFFSET);
     expect("accept_context_engine_sized", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_OK);
     reset(); reg_span(e, GLI_PROFILE); reg(c); reg(b2);
     expect("engine_one_byte_short", run(e, &profile, &octx, &oback), NVK_CGL_LAYOUT_ENGINE_BLOCK);
     expect_reason_is("reason_engine_one_byte_short",
                      "this GLI engine is a heap block of 0xc6a6 bytes, too small for the recorded reads");
     free(arena); free(c); free(b2); }

   /* every VM tag from 0 to 31: a span of readable, writable memory at no block start is a saved table only under
    * the tags malloc gives its regions (1 to 9 and 11 to 13), and a rend there is refused under the others */
   for (unsigned tag = 0; tag < 32; ++tag) {
      char name[48]; int heap = (tag >= 1 && tag <= 9) || (tag >= 11 && tag <= 13);
      build(&engine, &context, &back, 1);
      region(g_static_words, sizeof(g_static_words), tag, PROT_RW);
      put_ptr(engine, GLI_SAVED_DISPATCH, g_static_words);
      snprintf(name, sizeof(name), "saved_tag_%u", tag);
      expect(name, run(engine, &profile, &octx, &oback), heap ? NVK_CGL_LAYOUT_OK : NVK_CGL_LAYOUT_SAVED_BLOCK);
      put_ptr(engine, GLI_SAVED_DISPATCH, back); put_ptr(context, 0, g_static_words + 8);
      snprintf(name, sizeof(name), "rend_tag_%u", tag);
      expect(name, run(engine, &profile, &octx, &oback), heap ? NVK_CGL_LAYOUT_OK : NVK_CGL_LAYOUT_CONTEXT_REND);
   }
   /* and under a malloc tag only when the region is both readable and writable */
   for (int prot = 0; prot < 8; ++prot) {
      char name[48];
      build(&engine, &context, &back, 1);
      region(g_static_words, sizeof(g_static_words), TAG_MALLOC_SMALL, prot);
      put_ptr(engine, GLI_SAVED_DISPATCH, g_static_words);
      snprintf(name, sizeof(name), "saved_prot_%d", prot);
      expect(name, run(engine, &profile, &octx, &oback), (prot & 3) == 3 ? NVK_CGL_LAYOUT_OK : NVK_CGL_LAYOUT_SAVED_BLOCK);
   }

   /* nvk_cgl_renderer_reason: what bridge_current says of a call that does not come with the adopted engine */
   build(&engine, &context, &back, 1);
   { char why[768], want[768]; void *e2 = other_engine(context, 1);
     char *plain = calloc(1, 0x300), *twin = calloc(1, 0x300); reg(plain); reg(twin);
#define RENDERER_REASON(call, rend) \
     nvk_cgl_renderer_reason(why, sizeof(why), (call), (rend), engine, usable(engine), context, usable(context), \
                             GLI_PROFILE, GLI_CGL_DISPATCH, DISP_OFFSET)
     RENDERER_REASON(e2, e2);
     snprintf(want, sizeof(want), "the call came with another GLI engine of this CGL context, a heap block of 0x%zx "
              "bytes and the CGL context's renderer word is another GLI engine of this CGL context, a heap block of "
              "0x%zx bytes, the same value; this GLI engine is a heap block of 0x%zx bytes and the CGL context one of "
              "0x%zx bytes", usable(e2), usable(e2), usable(engine), usable(context));
     expect_text("renderer_reason_sibling", why, want);
     RENDERER_REASON(plain, engine);
     snprintf(want, sizeof(want), "the call came with a heap block of 0x%zx bytes and the CGL context's renderer word "
              "is this GLI engine, a different value; this GLI engine is a heap block of 0x%zx bytes and the CGL "
              "context one of 0x%zx bytes", usable(plain), usable(engine), usable(context));
     expect_text("renderer_reason_other_block", why, want);
     RENDERER_REASON(NULL, NULL);
     snprintf(want, sizeof(want), "the call came with null and the CGL context's renderer word is null, the same "
              "value; this GLI engine is a heap block of 0x%zx bytes and the CGL context one of 0x%zx bytes",
              usable(engine), usable(context));
     expect_text("renderer_reason_null", why, want);
     /* two heap blocks of one size read alike; the clause tells one value from two */
     RENDERER_REASON(plain, twin);
     snprintf(want, sizeof(want), "the call came with a heap block of 0x%zx bytes and the CGL context's renderer word "
              "is a heap block of 0x%zx bytes, a different value; this GLI engine is a heap block of 0x%zx bytes and "
              "the CGL context one of 0x%zx bytes", usable(plain), usable(twin), usable(engine), usable(context));
     expect_text("renderer_reason_twin_blocks", why, want);
     RENDERER_REASON(plain, plain);
     snprintf(want, sizeof(want), "the call came with a heap block of 0x%zx bytes and the CGL context's renderer word "
              "is a heap block of 0x%zx bytes, the same value; this GLI engine is a heap block of 0x%zx bytes and "
              "the CGL context one of 0x%zx bytes", usable(plain), usable(plain), usable(engine), usable(context));
     expect_text("renderer_reason_one_block", why, want);
     /* each word is classified against the adopted engine's block and the context's block, in both positions */
     RENDERER_REASON((char *)engine + 0x40, (char *)context + 0x10);
     snprintf(want, sizeof(want), "the call came with inside this GLI engine at +0x40 and the CGL context's renderer "
              "word is inside the CGL context at +0x10, a different value; this GLI engine is a heap block of 0x%zx "
              "bytes and the CGL context one of 0x%zx bytes", usable(engine), usable(context));
     expect_text("renderer_reason_interior", why, want);
     RENDERER_REASON((char *)context + 0x10, (char *)engine + 0x40);
     snprintf(want, sizeof(want), "the call came with inside the CGL context at +0x10 and the CGL context's renderer "
              "word is inside this GLI engine at +0x40, a different value; this GLI engine is a heap block of 0x%zx "
              "bytes and the CGL context one of 0x%zx bytes", usable(engine), usable(context));
     expect_text("renderer_reason_interior_swapped", why, want);
#undef RENDERER_REASON
     free(plain); free(twin); }

   /* nvk_cgl_layout_has_context: the verdicts whose report names the CGL context */
   expect_int("has_context_ok", nvk_cgl_layout_has_context(NVK_CGL_LAYOUT_OK), 1);
   expect_int("has_context_engine_block", nvk_cgl_layout_has_context(NVK_CGL_LAYOUT_ENGINE_BLOCK), 0);
   expect_int("has_context_profile", nvk_cgl_layout_has_context(NVK_CGL_LAYOUT_PROFILE), 0);
   expect_int("has_context_context_null", nvk_cgl_layout_has_context(NVK_CGL_LAYOUT_CONTEXT_NULL), 0);
   expect_int("has_context_context_block", nvk_cgl_layout_has_context(NVK_CGL_LAYOUT_CONTEXT_BLOCK), 0);
   expect_int("has_context_context_rend", nvk_cgl_layout_has_context(NVK_CGL_LAYOUT_CONTEXT_REND), 1);
   expect_int("has_context_saved_block", nvk_cgl_layout_has_context(NVK_CGL_LAYOUT_SAVED_BLOCK), 1);

   /* nvk_cgl_further_engine, on the verdicts of real checks of an engine of a context that has another engine */
   build_null(&engine, &context, &back, 1);
   { void *e1 = other_engine(context, 1);
     char *plain = calloc(1, 0x300); void **small = calloc(1, 0x40); reg(plain); reg(small);
#define FURTHER(adopted, unadopted) (int)nvk_cgl_further_engine(r, (adopted), (unadopted))
     r = run(engine, &profile, &octx, &oback);                       /* rend unset: the check passes */
     expect("further_base_ok", r, NVK_CGL_LAYOUT_OK);
     expect_reason_is("reason_accepted", "the layout matches the record");
     expect_int("further_ok_adopted", FURTHER(1, 0), NVK_CGL_FURTHER_ADOPTED);
     expect_int("further_ok_both", FURTHER(1, 1), NVK_CGL_FURTHER_ADOPTED);
     expect_int("further_ok_none", FURTHER(0, 0), NVK_CGL_FURTHER_NO);
     expect_int("further_ok_unadopted", FURTHER(0, 1), NVK_CGL_FURTHER_UNADOPTED);
     put_ptr(engine, GLI_SAVED_DISPATCH, small);                     /* rend unset, its own saved table refused */
     r = run(engine, &profile, &octx, &oback);
     expect("further_base_saved", r, NVK_CGL_LAYOUT_SAVED_BLOCK);
     expect_int("further_saved_adopted", FURTHER(1, 0), NVK_CGL_FURTHER_ADOPTED);
     expect_int("further_saved_none", FURTHER(0, 0), NVK_CGL_FURTHER_NO);
     expect_int("further_saved_unadopted", FURTHER(0, 1), NVK_CGL_FURTHER_UNADOPTED);
     put_ptr(engine, GLI_SAVED_DISPATCH, back); put_ptr(context, 0, e1);   /* rend is e1, an engine of the context */
     r = run(engine, &profile, &octx, &oback);
     expect("further_base_rend_sibling", r, NVK_CGL_LAYOUT_CONTEXT_REND);
     expect_int("further_rend_sibling_adopted", FURTHER(1, 0), NVK_CGL_FURTHER_ADOPTED);
     expect_int("further_rend_sibling_both", FURTHER(1, 1), NVK_CGL_FURTHER_ADOPTED);
     expect_int("further_rend_sibling_unadopted", FURTHER(0, 1), NVK_CGL_FURTHER_UNADOPTED);
     expect_int("further_rend_sibling_none", FURTHER(0, 0), NVK_CGL_FURTHER_NO);
     put_ptr(context, 0, plain);                                     /* rend is a heap block that is no engine */
     r = run(engine, &profile, &octx, &oback);
     expect("further_base_rend_block", r, NVK_CGL_LAYOUT_CONTEXT_REND);
     expect_int("further_rend_block_adopted", FURTHER(1, 0), NVK_CGL_FURTHER_ADOPTED);
     expect_int("further_rend_block_unadopted", FURTHER(0, 1), NVK_CGL_FURTHER_UNADOPTED);
     expect_int("further_rend_block_none", FURTHER(0, 0), NVK_CGL_FURTHER_NO);
     put_ptr(context, 0, NULL); ((char *)engine)[GLI_PROFILE] = 2;   /* a check before the context's refuses */
     r = run(engine, &profile, &octx, &oback);
     expect("further_base_profile", r, NVK_CGL_LAYOUT_PROFILE);
     expect_int("further_profile_both", FURTHER(1, 1), NVK_CGL_FURTHER_NO);
     expect_int("further_profile_adopted", FURTHER(1, 0), NVK_CGL_FURTHER_NO);
     expect_int("further_profile_unadopted", FURTHER(0, 1), NVK_CGL_FURTHER_NO);
     ((char *)engine)[GLI_PROFILE] = 1; put_ptr(engine, GLI_SAVED_DISPATCH, NULL);
     r = run(engine, &profile, &octx, &oback);
     expect("further_base_context_null", r, NVK_CGL_LAYOUT_CONTEXT_NULL);
     expect_int("further_context_null_both", FURTHER(1, 1), NVK_CGL_FURTHER_NO);
     put_ptr(engine, GLI_SAVED_DISPATCH, back); put_ptr(engine, GLI_CGL_DISPATCH, (char *)engine + DISP_OFFSET);
     r = run(engine, &profile, &octx, &oback);
     expect("further_base_context_block", r, NVK_CGL_LAYOUT_CONTEXT_BLOCK);
     expect_int("further_context_block_both", FURTHER(1, 1), NVK_CGL_FURTHER_NO);
     expect_int("further_engine_block_both",
                (int)nvk_cgl_further_engine(NVK_CGL_LAYOUT_ENGINE_BLOCK, 1, 1), NVK_CGL_FURTHER_NO);
#undef FURTHER
     free(plain); free(small); }

   /* nvk_cgl_room: what integer_dispatch_slot asks of the CGL private object and of its engine list */
   reset();
   { char *outer = calloc(1, 0x4000); char *own = calloc(1, 0x200); reg(outer); reg(own);
     size_t outer_size = usable(outer), own_size = usable(own); const void *blk; size_t blk_size;
     size_t got = nvk_cgl_room(own, outer, outer_size, 0x1e90, &blk, &blk_size);
     expect_room("room_own_block", got, own_size, blk, own, blk_size, own_size);
     got = nvk_cgl_room(outer + 0x1e90, outer, outer_size, 0x1e90, &blk, &blk_size);
     expect_room("room_inside_outer_at_min", got, outer_size - 0x1e90, blk, outer, blk_size, outer_size);
     got = nvk_cgl_room(outer + outer_size - 8, outer, outer_size, 0x1e90, &blk, &blk_size);
     expect_room("room_inside_outer_last_word", got, 8, blk, outer, blk_size, outer_size);
     got = nvk_cgl_room(outer + 0x1e88, outer, outer_size, 0x1e90, &blk, &blk_size);
     expect_room("room_below_min", got, 0, blk, NULL, blk_size, 0);
     got = nvk_cgl_room(outer + 0x1e94, outer, outer_size, 0x1e90, &blk, &blk_size);
     expect_room("room_unaligned", got, 0, blk, NULL, blk_size, 0);
     got = nvk_cgl_room(outer + outer_size, outer, outer_size, 0x1e90, &blk, &blk_size);
     expect_room("room_at_outer_end", got, 0, blk, NULL, blk_size, 0);
     got = nvk_cgl_room(outer - 0x10, outer, outer_size, 0, &blk, &blk_size);
     expect_room("room_before_outer", got, 0, blk, NULL, blk_size, 0);
     got = nvk_cgl_room(outer - 8, outer, outer_size, 0, &blk, &blk_size);   /* the offset wraps to the top word */
     expect_room("room_one_word_before_outer", got, 0, blk, NULL, blk_size, 0);
     got = nvk_cgl_room(g_static_words, outer, outer_size, 0, &blk, &blk_size);
     expect_room("room_elsewhere", got, 0, blk, NULL, blk_size, 0);
     got = nvk_cgl_room(NULL, outer, outer_size, 0, &blk, &blk_size);
     expect_room("room_null", got, 0, blk, NULL, blk_size, 0);
     got = nvk_cgl_room(outer + 0x2000, NULL, 0, 0, &blk, &blk_size);
     expect_room("room_no_outer", got, 0, blk, NULL, blk_size, 0);
     got = nvk_cgl_room((void *)(uintptr_t)0x2000, NULL, 0x4000, 0, &blk, &blk_size);
     expect_room("room_null_outer_sized", got, 0, blk, NULL, blk_size, 0);
     got = nvk_cgl_room(outer + 0x2000, outer, 0, 0, &blk, &blk_size);   /* outer starts no heap block */
     expect_room("room_outer_no_block", got, 0, blk, NULL, blk_size, 0);
     /* the engine list inside the private object's block, past the count: as integer_dispatch_slot passes it */
     got = nvk_cgl_room(own + 0x88, own, own_size, 0x84, &blk, &blk_size);
     expect_room("room_list_past_head", got, own_size - 0x88, blk, own, blk_size, own_size);
     got = nvk_cgl_room(own + 0x80, own, own_size, 0x84, &blk, &blk_size);
     expect_room("room_list_over_head", got, 0, blk, NULL, blk_size, 0);
     free(outer); free(own); }

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
        expected = {"accept_core", "accept_core_outputs", "accept_legacy", "accept_legacy_outputs",
                    "accept_rend_interior_other", "accept_rend_loose", "accept_rend_null", "accept_rend_null_legacy",
                    "accept_rend_null_legacy_outputs", "accept_rend_null_outputs", "accept_rend_one",
                    "accept_rend_prescribble",
                    "accept_rend_wild", "accept_rend_heap_interior", "accept_rend_low_aligned",
                    "accept_saved_heap_interior", "accept_saved_heap_interior_outputs", "accept_saved_heap_exact",
                    "accept_saved_past_context_public", "accept_saved_past_context_public_outputs",
                    "both_dispatch_null", "reason_both_dispatch_null", "context_wild", "reason_context_wild",
                    "rend_image", "reason_rend_image", "rend_stack", "saved_unmapped", "saved_unmapped_outputs",
                    "reason_saved_unmapped", "saved_image_data", "reason_saved_image_data", "saved_analysis_tool",
                    "saved_heap_readonly", "saved_heap_short", "reason_saved_heap_short", "cgl_dispatch_null",
                    "context_interior", "context_is_engine", "context_not_heap", "context_too_small",
                    "context_too_small_outputs", "engine_not_block", "engine_not_block_outputs", "engine_too_small",
                    "profile_out_of_range", "reason_cgl_dispatch_null", "reason_context_interior",
                    "reason_context_is_engine", "reason_context_not_heap", "reason_context_too_small",
                    "reason_engine_not_block", "reason_engine_too_small", "reason_profile",
                    "reason_rend_and_saved_bad", "reason_rend_context", "reason_rend_engine_interior",
                    "reason_rend_engine_sized_block", "reason_rend_sibling", "reason_saved_dispatch_null",
                    "reason_saved_in_engine", "reason_saved_is_context_disp", "reason_saved_is_engine",
                    "reason_saved_too_small", "reason_saved_unaligned", "rend_and_saved_bad", "rend_context",
                    "rend_context_interior", "rend_engine_sized_block", "rend_mismatch", "rend_mismatch_outputs",
                    "rend_sibling", "rend_small_block", "saved_dispatch_null", "saved_in_engine", "saved_is_context",
                    "saved_is_context_disp", "saved_is_engine", "saved_past_context_end", "saved_sibling",
                    "saved_too_small", "saved_too_small_outputs", "saved_unaligned", "saved_wild",
                    "saved_runs_into_engine", "saved_runs_into_engine_outputs", "reason_saved_runs_into_engine",
                    "saved_runs_into_engine_last_word", "saved_runs_into_context", "reason_saved_runs_into_context",
                    "accept_saved_ends_at_engine", "accept_saved_ends_at_engine_outputs",
                    "accept_saved_ends_at_context", "accept_saved_after_context_block",
                    "rend_bad_and_saved_runs_into_engine", "room_own_block", "room_inside_outer_at_min",
                    "room_inside_outer_last_word", "room_below_min", "room_at_outer_end", "room_before_outer",
                    "room_elsewhere", "room_null", "room_no_outer", "room_null_outer_sized", "room_outer_no_block",
                    "room_list_past_head", "room_list_over_head", "room_unaligned",
                    "rend_block_sizes", "reason_rend_block_sizes", "reason_sizes_distinct", "saved_small_sizes",
                    "reason_saved_small_sizes", "context_null_from_dispatch_8", "reason_context_null_from_dispatch_8",
                    "rend_engine_unaligned", "reason_rend_engine_unaligned", "rend_context_unaligned",
                    "reason_rend_context_unaligned", "saved_below_first_page", "reason_saved_below_first_page",
                    "saved_first_page", "reason_saved_first_page", "saved_user_top", "reason_saved_user_top",
                    "saved_below_user_top", "reason_saved_below_user_top", "saved_wild_reason", "reason_saved_wild",
                    "accept_saved_fills_context_tail", "accept_saved_fills_context_tail_outputs",
                    "saved_context_tail_one_word_short", "saved_context_tail_over_public",
                    "saved_context_tail_unaligned", "saved_context_tail_unaligned_outputs",
                    "reason_saved_context_tail_unaligned", "accept_saved_block_after_engine",
                    "accept_saved_block_after_engine_outputs", "rend_block_after_engine",
                    "saved_runs_into_engine_one_word", "reason_saved_runs_into_engine_one_word",
                    "saved_runs_into_context_one_word", "reason_saved_runs_into_context_one_word",
                    "renderer_reason_sibling", "renderer_reason_other_block", "renderer_reason_null",
                    "accept_engine_exact", "accept_context_engine_sized", "engine_one_byte_short",
                    "reason_engine_one_byte_short", "saved_context_tail_half_aligned", "saved_small_below_engine",
                    "reason_saved_small_below_engine", "renderer_reason_twin_blocks", "renderer_reason_one_block",
                    "renderer_reason_interior", "renderer_reason_interior_swapped",
                    "has_context_ok", "has_context_engine_block", "has_context_profile", "has_context_context_null",
                    "has_context_context_block", "has_context_context_rend", "has_context_saved_block",
                    "further_base_ok", "reason_accepted", "further_ok_adopted", "further_ok_both", "further_ok_none",
                    "further_ok_unadopted", "further_base_saved", "further_saved_adopted", "further_saved_none",
                    "further_saved_unadopted", "further_base_rend_sibling", "further_rend_sibling_adopted",
                    "further_rend_sibling_both", "further_rend_sibling_unadopted", "further_rend_sibling_none",
                    "further_base_rend_block", "further_rend_block_adopted", "further_rend_block_unadopted",
                    "further_rend_block_none", "further_base_profile", "further_profile_both",
                    "further_profile_adopted", "further_profile_unadopted", "further_base_context_null",
                    "further_context_null_both", "further_base_context_block", "further_context_block_both",
                    "further_engine_block_both", "room_one_word_before_outer"}
        expected |= {f"saved_tag_{t}" for t in range(32)} | {f"rend_tag_{t}" for t in range(32)}
        expected |= {f"saved_prot_{p}" for p in range(8)}
        check("the recorded layout is accepted and each mismatch has its own case, and no case runs unlisted",
              expected == cases, sorted(expected ^ cases))
        bad = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
        check("every synthetic layout yields the expected verdict", not bad, "\n".join(bad))
        check("the C program reports all cases passed", "ALL CASES PASSED" in out and rp.returncode == 0)

    # the source wires the checks: prepare_gld refuses on any nonzero verdict and never reads or writes first
    def flat(text):
        """text with every run of white space as one space, so that a statement compares whatever its line breaks"""
        return re.sub(r"\s+", " ", text)

    def uncommented(text):
        """text without its C comments"""
        return re.sub(r"/\*.*?\*/", "", text, flags=re.S)

    def function(text, header):
        """The definition whose first line is header (a regular expression), to its closing brace, or ""."""
        m = re.search("^" + header + r"\n\{.*?\n\}\n", text, re.S | re.M)
        return m.group(0) if m else ""

    cgl = (ZINK / "nvk_cgl.c").read_text()
    body = function(cgl, r"EXPORT int nvk_cgl_prepare_gld\(void \*engine, void \*driver\)")
    further_text = ("enum nvk_cgl_further further = NVK_CGL_FURTHER_NO; "
                    "if (nvk_cgl_layout_has_context(layout)) { "
                    "pthread_mutex_lock(&bridges_lock); "
                    "struct nvk_cgl_bridge *other = find_bridge((CGLContextObj)(void *)report.context.value); "
                    "further = nvk_cgl_further_engine(layout, other != NULL, "
                    "unadopted.context == (void *)report.context.value); "
                    "pthread_mutex_unlock(&bridges_lock); } "
                    'if (further == NVK_CGL_FURTHER_ADOPTED) return fail("The CGL context already has a GLI engine '
                    'served by Zink"); '
                    "if (further == NVK_CGL_FURTHER_UNADOPTED) return 0; "
                    "if (layout != NVK_CGL_LAYOUT_OK) {")
    at = {k: body.find(v) for k, v in {
        "check": "nvk_cgl_check_gld_layout(engine, GLI_PROFILE, GLI_CGL_DISPATCH, GLI_SAVED_DISPATCH,",
        "further": "enum nvk_cgl_further further = NVK_CGL_FURTHER_NO;",
        "refuse": "if (layout != NVK_CGL_LAYOUT_OK) {",
        "screen": "if (!nvk_zink_screen_ready()) return leave_unadopted(context_ptr, driver);",
        "alloc": "if (!b) return leave_unadopted(context_ptr, driver);",
        "copy": "memcpy(b->original, context, PREFIX_BYTES);",
        "write": "back[BACK_INDEX(entries[i].offset)] = entries[i].adapter;",
        "redirect": "*(void ***)((char *)engine + GLI_SAVED_DISPATCH) = b->apple_back;"}.items()}
    refusal = braced(body, "if (layout != NVK_CGL_LAYOUT_OK) {")
    refusal_text = ("if (first_time(&context_refusal_said)) { char why[768]; "
                    "nvk_cgl_layout_reason(why, sizeof(why), &report); "
                    'say("OpenGL stays Apple\'s; " LAYOUT_DIFFERS, why); } return 0;')
    check("prepare_gld runs nvk_cgl_check_gld_layout and its refusal block ends in return 0",
          at["check"] >= 0 and refusal.rstrip().endswith("return 0;") and refusal.count("return") == 1, refusal)
    # Everything prepare does up to the allocation of the bridge, statement by statement: a return or a write to the
    # reason added anywhere in it would leave a refusal without its line or the line without its reason.
    head_text = ("EXPORT int nvk_cgl_prepare_gld(void *engine, void *driver) { "
                 "if (!engine || !driver) return 0; "
                 "if (nvk_cgl_obs_compatibility_enabled()) return 0; "
                 "if (native_surface_copy_context()) return 0; "
                 "unsigned char profile = 0; void *context_ptr = NULL; void **back = NULL; "
                 "struct nvk_cgl_layout_report report; "
                 "enum nvk_cgl_layout layout = "
                 "nvk_cgl_check_gld_layout(engine, GLI_PROFILE, GLI_CGL_DISPATCH, GLI_SAVED_DISPATCH, "
                 "offsetof(struct _CGLContextObject, disp), sizeof(struct _CGLContextObject), "
                 "sizeof(GLIFunctionDispatch), &profile, &context_ptr, &back, &report); "
                 + further_text + " " + refusal_text + " } "
                 "if (!nvk_zink_screen_ready()) return leave_unadopted(context_ptr, driver); "
                 "CGLContextObj context = (CGLContextObj)context_ptr; "
                 "struct nvk_cgl_bridge *b = calloc(1, sizeof(*b)); "
                 "if (!b) return leave_unadopted(context_ptr, driver); ")
    check("prepare_gld does nothing else between its first statement and the allocation of the bridge: no other "
          "return, and no other write to the reason of the refusal line",
          flat(uncommented(body)).startswith(head_text) and flat(uncommented(refusal)).strip() == refusal_text,
          flat(uncommented(body))[:len(head_text)])
    check("prepare_gld leaves a call without an engine or without a GLD context alone",
          flat(body).startswith("EXPORT int nvk_cgl_prepare_gld(void *engine, void *driver) { "
                                "if (!engine || !driver) return 0; "), body[:120])
    check("prepare_gld reads or writes through no recorded offset before the check, and through one only after "
          "every refusal",
          at["check"] >= 0 and re.search(r"\+\s*GLI_|\[\s*GLI_", body[:at["check"]]) is None
          and len(re.findall(r"\+\s*GLI_", body)) == 1 and 0 <= at["refuse"] < at["screen"] < at["redirect"],
          body[:200])
    check("prepare_gld checks the layout before it makes the Zink screen, so a refusal opens no NVK device",
          0 <= at["check"] < at["refuse"] < at["screen"] and body.count("nvk_zink_screen_ready()") == 1, body[:200])
    check("prepare_gld prints the guard's own reason text once per process, and only when it refuses",
          "if (first_time(&context_refusal_said)) {" in refusal and "nvk_cgl_layout_reason(why, sizeof(why), &report);"
          in refusal and 'say("OpenGL stays Apple\'s; " LAYOUT_DIFFERS, why);' in refusal
          and cgl.count("context_refusal_said") == 2 and body.count("say(") == 1, refusal)
    check("prepare_gld asks nvk_cgl_further_engine about the context's bridge and about an engine left unadopted, "
          "and a further engine returns before the refusal line, the screen and any write",
          further_text in flat(body)
          and all(len(re.findall(rf"\b{w}\b", re.sub(r"/\*.*?\*/", "", body, flags=re.S)))
                  == len(re.findall(rf"\b{w}\b", further_text)) for w in ("further", "other"))
          and 0 <= at["check"] < at["further"] < at["refuse"] < at["screen"] < at["alloc"] < at["copy"] < at["write"],
          str(at))
    leave = function(cgl, r"static int leave_unadopted\(void \*context, void \*driver\)")
    destroy = function(cgl, r"EXPORT void nvk_cgl_destroy_gld\(void \*driver\)")
    check("an engine that passed the check and is not adopted is remembered with its context, under the lock, and "
          "forgotten when its GLD context is destroyed",
          flat(leave) == "static int leave_unadopted(void *context, void *driver) { "
          "pthread_mutex_lock(&bridges_lock); unadopted.context = context; unadopted.driver = driver; "
          "pthread_mutex_unlock(&bridges_lock); return 0; } "
          and "static struct { void *context, *driver; } unadopted;" in cgl
          and body.count("return leave_unadopted(context_ptr, driver);") == 2
          and flat(destroy).startswith("EXPORT void nvk_cgl_destroy_gld(void *driver) { "
                                       "pthread_mutex_lock(&bridges_lock); "
                                       "if (unadopted.driver == driver) unadopted.context = unadopted.driver = NULL; ")
          # the declaration and six member uses: nothing else names the slot, so nothing else writes it
          and len(re.findall(r"\bunadopted\b", uncommented(cgl))) == 7
          and len(re.findall(r"\bunadopted\.", cgl)) == 6 and len(re.findall(r"\bleave_unadopted\(", cgl)) == 3,
          leave)
    current = function(cgl, r"static struct nvk_cgl_bridge \*bridge_current\(void \*engine\)")
    dropped = braced(current, "if (thread_bridge && thread_bridge->engine != engine) {")
    dropped_line = ("a call through a Zink context was dropped; it did not come with the GLI engine Zink adopted for "
                    "the current CGL context")
    said = "".join(re.findall(r'"([^"]*)"', braced(dropped, "if (first_time(&dropped_call_said)) {").split("say(")[-1]))
    dropped_text = ("if (first_time(&dropped_call_said)) { char why[768]; "
                    "nvk_cgl_renderer_reason(why, sizeof(why), engine, *(void **)context, thread_bridge->engine, "
                    "malloc_size(thread_bridge->engine), context, malloc_size(context), GLI_PROFILE, "
                    "GLI_CGL_DISPATCH, offsetof(struct _CGLContextObject, disp)); "
                    'say("' + dropped_line + '", why); } '
                    'fail("The current CGL context\'s renderer is not the GLI engine its bridge was prepared for"); '
                    "return NULL;")
    check("bridge_current drops a call that does not come with the adopted engine, saying so once per process with "
          "what the call came with, what rend holds and both blocks' sizes",
          dropped.rstrip().endswith("return NULL;") and "if (first_time(&dropped_call_said)) {" in dropped
          and "nvk_cgl_renderer_reason(why, sizeof(why), engine, *(void **)context, thread_bridge->engine, "
          "malloc_size(thread_bridge->engine), context, malloc_size(context), GLI_PROFILE, GLI_CGL_DISPATCH, "
          "offsetof(struct _CGLContextObject, disp));" in flat(dropped)
          and said == dropped_line and dropped.count("say(") == 1
          and re.sub(r'"\s+"', "", flat(uncommented(dropped))).strip() == dropped_text
          and 'fail("The current CGL context\'s renderer is not the GLI engine its bridge was prepared for");'
          in dropped and 0 <= current.find("if (thread_bridge && thread_bridge->engine != engine) {")
          < current.find("materialize(thread_bridge)")
          and 'fail("No attached Zink context current on this thread"); return NULL; }' in flat(current), dropped)
    check("nvk_cgl.c answers the guard's VM question from this task's VM map, reading no memory through the pointer",
          "#define NVK_CGL_VM_REGION(p, v) nvk_cgl_vm_region((p), (v))" in cgl
          and "mach_vm_region_recurse(mach_task_self()" in cgl
          and "VM_MEMORY_MALLOC_PROB_GUARD == 13" in cgl and "VM_MEMORY_MALLOC_PGUARD == 13" in cgl)
    slot = function(cgl, r"static void \*\*\*integer_dispatch_slot\(struct nvk_cgl_bridge \*b, char \*why, size_t "
                    r"why_size\)")
    order = [slot.find(t) for t in (
        "nvk_cgl_room(meta, b->context, malloc_size(b->context), sizeof(struct _CGLContextObject),",
        "&block, &block_size) < head) {",
        "*(char **)(meta+PRIV_ENGINES)",
        "if (!items || count<1 || count>16) {",
        "nvk_cgl_room(items, block, block_size, (size_t)(meta-(const char *)block)+head,",
        "&list_block, &list_block_size) < (size_t)count*PRIV_ENGINE_STRIDE) {",
        "for (int i=0;i<count;++i)")]
    refusals = [braced(slot, "&block, &block_size) < head) {"), braced(slot, "if (!items || count<1 || count>16) {"),
                braced(slot, "&list_block, &list_block_size) < (size_t)count*PRIV_ENGINE_STRIDE) {")]
    slot_reasons = [
        'snprintf(why, why_size, "the CGL private object is neither a heap block nor a span of the CGL context\'s '
        'block that holds the recorded engine count and list");',
        'snprintf(why, why_size, "the CGL private object\'s engine list pointer is %s and its engine count is %d, '
        'where the record has a list of 1 to 16 engines", items ? "not null" : "null", count);',
        'snprintf(why, why_size, "the CGL engine list is neither a heap block nor a span of the CGL private '
        'object\'s block that holds its %d records", count);',
        'snprintf(why, why_size, "none of the %d records of the CGL engine list names this GLI engine", count);']

    def reasons(text):
        """Each snprintf into why in text as one line, its adjacent string literals joined: the buffer, its size,
        the format and the arguments."""
        return [re.sub(r'"\s+"', "", m.group(0)) for m in re.finditer(r"snprintf\(why\b.*?\);", flat(text))]

    check("integer_dispatch_slot reads the CGL private object and the engine list only as memory the allocator "
          "vouches for, and returns NULL otherwise",
          order[0] >= 0 and order == sorted(order) and len(set(order)) == len(order)
          and all(r.rstrip().endswith("return NULL;") and r.count("snprintf(why, why_size,") == 1 for r in refusals),
          slot[:300])
    slot_code = uncommented(slot)
    check("integer_dispatch_slot prints nothing; each of its four ways of finding no slot writes its reason, whole, "
          "into the caller's buffer",
          "say(" not in slot and "first_time(" not in slot and "stderr" not in slot
          and reasons(slot) == slot_reasons and slot.count("return NULL;") == 4
          and flat(slot).rstrip().endswith('names this GLI engine", count); return NULL; }')
          and "char unused[1]; if (!why) { why = unused; why_size = sizeof(unused); } char *meta=" in flat(slot_code)
          and len(re.findall(r"\bwhy\s*=[^=]", slot_code)) == 1
          and len(re.findall(r"\bwhy_size\s*=[^=]", slot_code)) == 1 and "sizeof(why)" not in slot_code
          # the parameter, the two uses in the statement that gives a null one a buffer, and one per reason: nothing
          # else names the buffer or its size, so nothing else writes a reason or blanks one
          and len(re.findall(r"\bwhy\b", slot_code)) == 3 + len(slot_reasons)
          and len(re.findall(r"\bwhy_size\b", slot_code)) == 2 + len(slot_reasons)
          and "const size_t head=PRIV_ENGINE_COUNT+sizeof(int16_t);" in slot, str(reasons(slot)))
    install = function(cgl, r"static void install_integer_dispatch\(struct nvk_cgl_bridge \*b\)")
    select = function(cgl, r"static bool select_integer_dispatch\(struct nvk_cgl_bridge \*b, bool zink\)")
    placed = braced(install, "      else {")
    install_reasons = [
        'snprintf(why,sizeof(why),"the callback vector in this GLI engine\'s record is null");',
        'snprintf(why,sizeof(why),"the callback vector in this GLI engine\'s record is 0x%jx, which starts no '
        'heap block",(uintmax_t)(uintptr_t)vector);',
        'snprintf(why,sizeof(why),"the callback vector in this GLI engine\'s record is a heap block of 0x%zx '
        'bytes, where the record has one of 0x80 to 0x1000 bytes",bytes);',
        'snprintf(why,sizeof(why),"dladdr does not name entries 4, 6, 9 and 15 of the callback vector in this '
        'GLI engine\'s record gliCreateContext, gliDestroyContext, gliSetInteger and gliGetVersion");']
    check("install_integer_dispatch says once per process, under its own flag, why CGLTexImageIOSurface2D stays "
          "Apple's in a Zink context: every way that leaves the hook out, except a failed allocation of the clone, "
          "writes its reason and reaches the line",
          flat(install).startswith("static void install_integer_dispatch(struct nvk_cgl_bridge *b) { char why[256]; "
                                   "void ***slot=integer_dispatch_slot(b,why,sizeof(why)); if (slot) { "
                                   "void **vector=*slot; size_t bytes=vector && malloc_zone_from_ptr(vector) ? "
                                   "malloc_size(vector) : 0; if (!vector) snprintf(")
          and reasons(install) == install_reasons
          # the declaration, the buffer and its size handed to integer_dispatch_slot, two per reason and the one in
          # the line: nothing else names the buffer, so nothing blanks a reason between its write and the line
          and len(re.findall(r"\bwhy\b", uncommented(install))) == 4 + 2 * len(install_reasons)
          and flat(install).rstrip().endswith(
              "if (first_time(&iosurface_refusal_said)) say(\"CGLTexImageIOSurface2D in a Zink context stays "
              "Apple's; \" LAYOUT_DIFFERS, why); }")
          and flat(placed).strip() == "void **copy=malloc(bytes); if (!copy) return; memcpy(copy,vector,bytes); "
          "copy[9]=(void *)iosurface_integer; b->integer_original=vector; b->integer_zink=copy; *slot=copy; return;"
          and "else if (!bytes) snprintf(" in flat(install)
          and "else if (bytes<16*sizeof(void *) || bytes>4096) snprintf(" in flat(install)
          and 'else if (!named_callback(vector[4],"gliCreateContext") || '
          '!named_callback(vector[6],"gliDestroyContext") || !named_callback(vector[9],"gliSetInteger") || '
          '!named_callback(vector[15],"gliGetVersion")) snprintf(' in flat(install)
          and install.count("return;") == 2 and install.count("say(") == 1
          and len(re.findall(r"\bwhy\s*=[^=]", uncommented(install))) == 0
          and cgl.count("iosurface_refusal_said") == 2
          and cgl.count("char why[256];") == 1 and cgl.count("char why[768];") == 2 and cgl.count("char why[") == 3
          and "void ***slot=integer_dispatch_slot(b,NULL,0);" in select and "say(" not in select
          and len(re.findall(r"\binteger_dispatch_slot\(", cgl)) == 3, install)
    says = re.findall(r"\bsay\(", cgl)
    check("nvk_cgl.c prints to standard error in one place, each of its three kinds of line once per process",
          cgl.count("fprintf(stderr") == 1 and cgl.count("stderr") == 1
          and 'static void say(const char *what, const char *why) { fprintf(stderr, "NVK Zink: %s (%s)\\n", what, why); }'
          in cgl and len(says) == 4   # the definition and three calls, each under first_time
          and cgl.count("if (first_time(&") == 3
          and "static bool first_time(atomic_flag *said) { return !atomic_flag_test_and_set(said); }" in cgl,
          str(len(says)))
    # the card checks mark a row inactive on the line prepare_gld prints, and on no other
    common = (ROOT / "tools" / "card" / "zink_common.h").read_text()
    m = re.search(r'#define LAYOUT_DIFFERS "([^"]*)"', cgl)
    differs = m.group(1) if m else "?"
    line = "NVK Zink: OpenGL stays Apple's; " + differs + " ("
    check("the line of a refused context starts with the text tools/card/zink_common.h reads as inactive",
          f'zk_tee_find("{line}", line,' in common and common.count("zk_tee_find(") == 2
          and cgl.count('say("OpenGL stays Apple\'s; " LAYOUT_DIFFERS,') == 1, line)
    # the documents quote the three lines as the code builds them
    quotes = ["NVK Zink: OpenGL stays Apple's; " + differs + " (",
              "NVK Zink: CGLTexImageIOSurface2D in a Zink context stays Apple's; " + differs + " (",
              "NVK Zink: " + dropped_line + " ("]
    for name in ("docs/ZINK-OPENGL.md", "docs/TESTING-ON-HARDWARE.md", "docs/TESTING-ON-HARDWARE.es.md",
                 "CHANGELOG.md"):
        text = flat((ROOT / name).read_text().replace("\n  ", "\n").replace("\n   ", "\n"))
        # with "..." for the reason, so that a quote of another release's output does not stand in for it
        check(f"{name} quotes each of the three lines as nvk_cgl.c prints it, with ... for its reason",
              all(q + "...)" in text for q in quotes), str([q for q in quotes if q + "...)" not in text]))
    guard = (ZINK / "nvk_cgl_guard.h").read_text()
    check("the guard header derives its checks from the three run-time facts the includer supplies",
          "#error" in guard and "NVK_CGL_BLOCK_SIZE(p)" in guard and "NVK_CGL_IN_ZONE(p)" in guard
          and "NVK_CGL_VM_REGION(p, &c.vm)" in guard)

    print(f"{fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
