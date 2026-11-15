/* SPDX-License-Identifier: MIT */
#ifndef NVK_CGL_GUARD_H
#define NVK_CGL_GUARD_H
/* nvk_cgl.c reads and writes through private GLEngine offsets that the source
 * fork this code is ported from recorded on macOS 15.8.1 (24H32) and 26.7.1
 * (25G241), x86_64. A build whose layout differs would have those reads and
 * writes land in the wrong memory of an OpenGL application. These helpers prove
 * at run time, from facts the process already has, that the memory is shaped as
 * the record says; nvk_cgl_prepare_gld keeps Apple's renderer (returns 0,
 * writing nothing) whenever a check fails.
 *
 * The includer supplies the two run-time facts as macros, so the same checks
 * compile on a Mac against Apple's allocator and on Linux against a test shim:
 *   NVK_CGL_BLOCK_SIZE(p)  the size of the heap block p starts, or 0 if p is
 *                          not the start of a block (malloc_size on a Mac);
 *   NVK_CGL_IN_ZONE(p)     nonzero if p lies in a known malloc zone
 *                          (malloc_zone_from_ptr(p) != NULL on a Mac).
 * The sizes and the dispatch-member offset come from the public macOS SDK
 * headers (struct _CGLContextObject, GLIFunctionDispatch); no binary is read. */
#include <stddef.h>

#if !defined(NVK_CGL_BLOCK_SIZE) || !defined(NVK_CGL_IN_ZONE)
#error "define NVK_CGL_BLOCK_SIZE and NVK_CGL_IN_ZONE before including nvk_cgl_guard.h"
#endif

/* A pointer that starts a heap block in a known zone and spans at least need
 * bytes. A pointer read from a mismatched offset is almost never the start of a
 * block of the right size, so NVK_CGL_BLOCK_SIZE returns 0 and this returns 0. */
static inline int nvk_cgl_block_spans(const void *p, size_t need)
{
   return p && NVK_CGL_IN_ZONE(p) && NVK_CGL_BLOCK_SIZE(p) >= need;
}

enum nvk_cgl_layout {
   NVK_CGL_LAYOUT_OK = 0,
   NVK_CGL_LAYOUT_ENGINE_BLOCK,   /* the GLI context is not a heap block spanning the recorded reads */
   NVK_CGL_LAYOUT_PROFILE,        /* the profile byte is neither legacy (0) nor core (1) */
   NVK_CGL_LAYOUT_CONTEXT_NULL,   /* the CGL dispatch pointer or the saved dispatch pointer is null */
   NVK_CGL_LAYOUT_CONTEXT_BLOCK,  /* the derived CGL context is not a heap block holding the whole struct */
   NVK_CGL_LAYOUT_CONTEXT_REND,   /* the CGL context's first member (rend) is not this GLI engine */
   NVK_CGL_LAYOUT_SAVED_BLOCK,    /* the saved dispatch table is not a heap block spanning GLIFunctionDispatch */
};

/* Check that gldCreateContext's GLI engine is shaped as the record says, and
 * hand back the profile byte, the CGL context derived from GLI_CGL_DISPATCH and
 * the saved dispatch table. On any nonzero return the outputs are untouched and
 * the caller must not read or write through these offsets.
 *
 * gli_cgl_dispatch holds &context->disp (CGL + disp_offset), so the context is
 * that pointer minus disp_offset. The record ties the two offsets together: the
 * context's first member is the GLI context (struct _CGLContextObject.rend), so
 * reading it back must give this same engine. On a mismatched layout that near
 * never holds, and the heap-block checks catch a pointer that is not a real CGL
 * context or saved table. */
static inline enum nvk_cgl_layout
nvk_cgl_check_gld_layout(void *engine,
                         size_t gli_profile, size_t gli_cgl_dispatch, size_t gli_saved_dispatch,
                         size_t disp_offset, size_t cgl_context_size, size_t dispatch_table_size,
                         unsigned char *out_profile, void **out_context, void ***out_back)
{
   /* The profile byte at gli_profile is the highest offset read in the GLI
    * context (gli_cgl_dispatch, gli_saved_dispatch and the share group are all
    * below it), so a block that spans it spans every read here. */
   if (!nvk_cgl_block_spans(engine, gli_profile + 1))
      return NVK_CGL_LAYOUT_ENGINE_BLOCK;
   unsigned char profile = *(unsigned char *)((char *)engine + gli_profile);
   if (profile > 1)
      return NVK_CGL_LAYOUT_PROFILE;
   void *dispatch = *(void **)((char *)engine + gli_cgl_dispatch);
   void **back = *(void ***)((char *)engine + gli_saved_dispatch);
   if (!dispatch || !back)
      return NVK_CGL_LAYOUT_CONTEXT_NULL;
   void *context = (char *)dispatch - disp_offset;
   if (!nvk_cgl_block_spans(context, cgl_context_size))
      return NVK_CGL_LAYOUT_CONTEXT_BLOCK;
   if (*(void **)context != engine)
      return NVK_CGL_LAYOUT_CONTEXT_REND;
   if (!nvk_cgl_block_spans(back, dispatch_table_size))
      return NVK_CGL_LAYOUT_SAVED_BLOCK;
   *out_profile = profile;
   *out_context = context;
   *out_back = back;
   return NVK_CGL_LAYOUT_OK;
}

#endif
