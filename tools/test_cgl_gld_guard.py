#!/usr/bin/env python3
"""The CGL adapter reads and writes through private GLEngine offsets the source fork recorded on two macOS builds.
zink/nvk_cgl_guard.h holds the structural checks that nvk_cgl_prepare_gld runs before any such read or write, so that a
build whose layout differs keeps Apple's renderer instead of writing into the wrong memory. This test compiles those
checks for the host and feeds nvk_cgl_check_gld_layout synthetic heap blocks and VM regions: layouts the record allows
(accepted: the CGL context's rend null, this engine, or no heap object in heap or unmapped memory; the saved table its
own block, past the context's public part or a span of heap memory at no block start that reaches neither block) and
one per way the layout can differ (each refused, naming what the memory held), with controls. It also feeds
nvk_cgl_room, the question integer_dispatch_slot asks of the CGL private object and of its engine list. It touches no
GPU, loads no Apple framework and installs nothing, so it is safe to run on any Mac; malloc_size has no Linux name, so
a shim behind the header's macros uses malloc_usable_size on Linux and malloc_size on a Mac, and a table of synthetic
regions stands in for the VM map.
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
static void expect_room(const char *name, size_t got, size_t want, const void *block, const void *want_block,
                        size_t block_size, size_t want_block_size) {
   int ok = got == want && block == want_block && block_size == want_block_size;
   printf("%s %s got=0x%zx want=0x%zx\n", ok ? "ok  " : "FAIL", name, got, want);
   if (!ok) failed = 1;
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
   expect_reason("reason_engine_not_block", "the GLI context starts no heap block");
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
   expect_reason("reason_cgl_dispatch_null", "the GLI context's CGL dispatch pointer is null");
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_CGL_DISPATCH, NULL); put_ptr(engine, GLI_SAVED_DISPATCH, NULL);
   expect("both_dispatch_null", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_NULL);
   expect_reason("reason_both_dispatch_null", "the GLI context's CGL and saved dispatch pointers are null");
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_SAVED_DISPATCH, NULL);
   expect("saved_dispatch_null", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_NULL);
   expect_reason("reason_saved_dispatch_null", "the GLI context's saved dispatch pointer is null");

   /* refused: the derived context is the engine, inside it, too small or no heap block */
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_CGL_DISPATCH, (char *)engine + 0x2000 + DISP_OFFSET);
   expect("context_interior", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_BLOCK);
   expect_reason("reason_context_interior",
                 "the CGL context derived from the GLI context is inside this GLI engine at +0x2000");
   build(&engine, &context, &back, 1);
   put_ptr(engine, GLI_CGL_DISPATCH, (char *)engine + DISP_OFFSET);
   expect("context_is_engine", run(engine, &profile, &octx, &oback), NVK_CGL_LAYOUT_CONTEXT_BLOCK);
   expect_reason("reason_context_is_engine", "the CGL context derived from the GLI context is this GLI engine");
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
     expect_reason("reason_context_too_small", "the CGL context derived from the GLI context is a heap block of 0x");
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
     got = nvk_cgl_room(outer + outer_size, outer, outer_size, 0x1e90, &blk, &blk_size);
     expect_room("room_at_outer_end", got, 0, blk, NULL, blk_size, 0);
     got = nvk_cgl_room(outer - 0x10, outer, outer_size, 0, &blk, &blk_size);
     expect_room("room_before_outer", got, 0, blk, NULL, blk_size, 0);
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
                    "room_list_past_head", "room_list_over_head"}
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
    check("prepare_gld prints the guard's own reason text, and nvk_cgl.c prints to stderr nowhere else",
          "nvk_cgl_layout_reason(" in body
          and 'layout_refused_once(&context_refusal_reported, "OpenGL", why)' in body
          and cgl.count("fprintf(stderr") == 1 and cgl.count("layout_refused_once(&context_refusal_reported") == 1)
    check("bridge_current names a renderer that is not the bridge's engine apart from a missing bridge",
          "thread_bridge && thread_bridge->engine != engine" in cgl)
    check("prepare_gld adopts one GLI engine per CGL context, refusing a further one before the screen and any write",
          0 <= body.find("nvk_cgl_check_gld_layout(") < body.find("find_bridge((CGLContextObj)context_ptr)")
          < body.find("nvk_zink_screen_ready()") < body.find("memcpy(b->original"), body[:200])
    check("nvk_cgl.c answers the guard's VM question from this task's VM map, reading no memory through the pointer",
          "#define NVK_CGL_VM_REGION(p, v) nvk_cgl_vm_region((p), (v))" in cgl
          and "mach_vm_region_recurse(mach_task_self()" in cgl
          and "VM_MEMORY_MALLOC_PROB_GUARD == 13" in cgl)
    m = re.search(r"static void \*\*\*integer_dispatch_slot\(.*?\n\}\n", cgl, re.S)
    slot = m.group(0) if m else ""
    check("integer_dispatch_slot reads the CGL private object and the engine list only as memory the allocator "
          "vouches for",
          0 <= slot.find("nvk_cgl_room(meta, b->context, malloc_size(b->context), sizeof(struct _CGLContextObject),")
          < slot.find("*(char **)(meta+PRIV_ENGINES)")
          < slot.find("nvk_cgl_room(items, block, block_size, (size_t)(meta-(const char *)block)+head,")
          < slot.find("for (int i=0;i<count;++i)")
          and "< head)" in slot and "< (size_t)count*PRIV_ENGINE_STRIDE)" in slot, slot[:300])
    check("a refusal there says CGLTexImageIOSurface2D stays Apple's, not OpenGL, under its own once-flag",
          slot.count('layout_refused_once(&iosurface_refusal_reported, "CGLTexImageIOSurface2D in a Zink context",') == 2
          and '"NVK Zink: %s stays Apple\'s; GLEngine\'s private layout does not match the record (%s)\\n"' in cgl)
    check("a refusal is logged once per process on stderr",
          "atomic_flag" in cgl and "layout_refused_once" in cgl)
    guard = (ZINK / "nvk_cgl_guard.h").read_text()
    check("the guard header derives its checks from the three run-time facts the includer supplies",
          "#error" in guard and "NVK_CGL_BLOCK_SIZE(p)" in guard and "NVK_CGL_IN_ZONE(p)" in guard
          and "NVK_CGL_VM_REGION(p, &c.vm)" in guard)

    print(f"{fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
