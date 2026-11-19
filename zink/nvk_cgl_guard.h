/* SPDX-License-Identifier: MIT */
#ifndef NVK_CGL_GUARD_H
#define NVK_CGL_GUARD_H
/* nvk_cgl.c reads and writes through private GLEngine offsets that the source
 * fork this code is ported from recorded on macOS 15.8.1 (24H32) and 26.7.1
 * (25G241), x86_64. A build whose layout differs would have those reads and
 * writes land in the wrong memory of an OpenGL application. These helpers prove
 * at run time, from facts the process already has, that the memory is shaped as
 * the record says; nvk_cgl_prepare_gld keeps Apple's renderer (returns 0,
 * writing nothing) whenever a check fails, and names what the memory held.
 *
 * The includer supplies the run-time facts as macros, so the same checks
 * compile on a Mac against Apple's allocator and VM map and on Linux against a
 * test shim:
 *   NVK_CGL_BLOCK_SIZE(p)    the size of the heap block p starts, or 0 if p is
 *                            not the start of a block (malloc_size on a Mac);
 *   NVK_CGL_IN_ZONE(p)       nonzero if p lies in a known malloc zone
 *                            (malloc_zone_from_ptr(p) != NULL on a Mac);
 *   NVK_CGL_VM_REGION(p, v)  nonzero if p lies in a mapped region of this
 *                            task, filling the struct nvk_cgl_vm *v
 *                            (mach_vm_region_recurse on a Mac).
 * Each only asks the allocator or the VM map about a pointer value; none reads
 * through it.
 * The sizes and the dispatch-member offset come from the public macOS SDK
 * headers (struct _CGLContextObject, GLIFunctionDispatch); no binary is read. */
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#if !defined(NVK_CGL_BLOCK_SIZE) || !defined(NVK_CGL_IN_ZONE) || !defined(NVK_CGL_VM_REGION)
#error "define NVK_CGL_BLOCK_SIZE, NVK_CGL_IN_ZONE and NVK_CGL_VM_REGION before including nvk_cgl_guard.h"
#endif

/* x86_64 macOS maps user code and data below MACH_VM_MAX_ADDRESS (the SDK's
 * mach/i386/vm_param.h). */
#define NVK_CGL_USER_TOP ((uintptr_t)0x00007fffffe00000ull)

/* The VM region that holds an address: its user tag, its current protection
 * (VM_PROT_READ 1, VM_PROT_WRITE 2, VM_PROT_EXECUTE 4) and the bytes from the
 * address to the region's end. */
struct nvk_cgl_vm {
   unsigned tag;
   int prot;
   size_t avail;
};

/* A readable, writable region malloc made: the SDK's mach/vm_statistics.h tags
 * VM_MEMORY_MALLOC (1) through VM_MEMORY_MALLOC_LARGE_REUSED (9),
 * VM_MEMORY_MALLOC_NANO (11), VM_MEMORY_MALLOC_MEDIUM (12) and the guarded
 * malloc tag (13: VM_MEMORY_MALLOC_PGUARD in the 11.3 SDK,
 * VM_MEMORY_MALLOC_PROB_GUARD in the name nvk_cgl.c also accepts). An image's
 * data or text, a stack, the shared cache and an unmapped address are none of
 * these. */
static inline int nvk_cgl_vm_heap(const struct nvk_cgl_vm *v)
{
   return (v->prot & 3) == 3 && ((v->tag >= 1 && v->tag <= 9) || (v->tag >= 11 && v->tag <= 13));
}

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
   NVK_CGL_LAYOUT_CONTEXT_BLOCK,  /* the derived CGL context is the GLI context, or not a heap block holding the
                                     whole struct */
   NVK_CGL_LAYOUT_CONTEXT_REND,   /* the CGL context's first member (rend) is a heap object other than this GLI
                                     engine, or points into mapped memory malloc did not make */
   NVK_CGL_LAYOUT_SAVED_BLOCK,    /* the saved dispatch table is where the record rules a GLIFunctionDispatch out */
};

/* What a pointer-sized word holds, decided from the allocator's answers and
 * from address ranges, never by reading through it, with one exception: a word
 * that starts a heap block spanning the recorded GLI reads has that block's CGL
 * dispatch pointer compared with this CGL context's. */
enum nvk_cgl_word {
   NVK_CGL_WORD_NULL,        /* zero */
   NVK_CGL_WORD_ENGINE,      /* this GLI engine */
   NVK_CGL_WORD_SIBLING,     /* another GLI engine whose CGL dispatch pointer is this CGL context's */
   NVK_CGL_WORD_BLOCK,       /* the start of another heap block */
   NVK_CGL_WORD_IN_ENGINE,   /* inside this GLI engine's block, past its start */
   NVK_CGL_WORD_IN_CONTEXT,  /* inside the CGL context's block, its start included */
   NVK_CGL_WORD_LOOSE,       /* an aligned user address that starts no heap block and is inside neither block;
                                mapped and vm say what region holds it */
   NVK_CGL_WORD_WILD,        /* nonzero, and unaligned or outside user space */
};

struct nvk_cgl_word_class {
   enum nvk_cgl_word kind;
   uintptr_t value;
   size_t size;     /* the block's size, for ENGINE, SIBLING and BLOCK */
   size_t offset;   /* the offset into the block, for IN_ENGINE and IN_CONTEXT */
   int mapped;      /* for LOOSE: a region of this task holds the address */
   struct nvk_cgl_vm vm;
};

/* Filled on every return of nvk_cgl_check_gld_layout; a member past the check
 * that refused stays zero. */
struct nvk_cgl_layout_report {
   enum nvk_cgl_layout layout;
   size_t engine_size, context_size, dispatch_table_size;
   unsigned profile;
   int cgl_dispatch_null, saved_dispatch_null;
   int back_runs_into;   /* a table at a LOOSE back would reach the start of: 1 the GLI engine, 2 the CGL context */
   struct nvk_cgl_word_class context, rend, back;
};

/* The bytes the allocator vouches for at p: p starts a heap block (that block's
 * size), or p lies at least min_off bytes into the block outer starts, whose
 * size is outer_size (the bytes from p to that block's end). Zero otherwise.
 * *block and *block_size give the block that holds p. */
static inline size_t nvk_cgl_room(const void *p, const void *outer, size_t outer_size, size_t min_off,
                                  const void **block, size_t *block_size)
{
   *block = NULL; *block_size = 0;
   if (nvk_cgl_block_spans(p, 1)) {
      *block = p; *block_size = NVK_CGL_BLOCK_SIZE(p);
      return *block_size;
   }
   uintptr_t off = (uintptr_t)p - (uintptr_t)outer;
   if (!p || !outer || off < min_off || off >= outer_size) return 0;
   *block = outer; *block_size = outer_size;
   return outer_size - off;
}

/* Classify p against the GLI engine block and, when context is not null, the
 * CGL context block. */
static inline struct nvk_cgl_word_class
nvk_cgl_classify(const void *p, const void *engine, size_t engine_size,
                 const void *context, size_t context_size,
                 size_t gli_profile, size_t gli_cgl_dispatch, size_t disp_offset)
{
   struct nvk_cgl_word_class c = { NVK_CGL_WORD_NULL, (uintptr_t)p, 0, 0, 0, { 0, 0, 0 } };
   uintptr_t v = (uintptr_t)p;
   if (!v) return c;
   if (p == engine) { c.kind = NVK_CGL_WORD_ENGINE; c.size = engine_size; return c; }
   if (v - (uintptr_t)engine < engine_size) {
      c.kind = NVK_CGL_WORD_IN_ENGINE; c.offset = v - (uintptr_t)engine; return c;
   }
   if (context && v - (uintptr_t)context < context_size) {
      c.kind = NVK_CGL_WORD_IN_CONTEXT; c.offset = v - (uintptr_t)context; return c;
   }
   if ((v & (sizeof(void *) - 1)) || v < 0x1000 || v >= NVK_CGL_USER_TOP) {
      c.kind = NVK_CGL_WORD_WILD; return c;
   }
   if (!nvk_cgl_block_spans(p, 1)) {
      c.kind = NVK_CGL_WORD_LOOSE; c.mapped = NVK_CGL_VM_REGION(p, &c.vm) != 0; return c;
   }
   c.size = NVK_CGL_BLOCK_SIZE(p);
   c.kind = context && c.size > gli_profile &&
            *(void *const *)((const char *)p + gli_cgl_dispatch) == (const char *)context + disp_offset
            ? NVK_CGL_WORD_SIBLING : NVK_CGL_WORD_BLOCK;
   return c;
}

/* Check that gldCreateContext's GLI engine is shaped as the record says, and
 * hand back the profile byte, the CGL context derived from GLI_CGL_DISPATCH and
 * the saved dispatch table. On any nonzero return the outputs are untouched and
 * the caller must not read or write through these offsets; *r says what each
 * checked word held.
 *
 * gli_cgl_dispatch holds &context->disp (CGL + disp_offset), so the context is
 * that pointer minus disp_offset; it must be the start of a heap block, other
 * than the GLI engine, that holds the whole public struct. gldCreateContext runs
 * inside GLEngine's context creation, before CGL can store the new GLI context
 * in the context's first member (rend), so rend is accepted when it is null,
 * this engine, no aligned user address, or a value that starts no heap block,
 * points into neither the engine nor the context and lies in heap memory or in
 * no mapped region (what a block CGL has not cleared holds: a free-list link,
 * MallocPreScribble's 0xaa bytes). A rend that starts another heap block,
 * points into the engine or the context, or points into an image, a stack or
 * other memory malloc did not map (a vtable or class pointer) is refused: that
 * is an object the record does not put there. The adapter compares rend with
 * the engine again on every call through the current context (bridge_current,
 * original_noncurrent, swap_drawable).
 *
 * The saved dispatch table is the one CGL restores the context's dispatch
 * from, so it is CGL's memory, not GLEngine's, and prepare writes adapters
 * into it. It is accepted as its own heap block spanning GLIFunctionDispatch,
 * as a span of the CGL context's block past the public struct, or as a span
 * of readable, writable heap memory starting at no block start (a table inside
 * a CGL heap object this check cannot see: the CGL private object's engine
 * records are 0x1e90 bytes apart, room for a 0x18-byte head and a
 * GLIFunctionDispatch).
 * It is refused as a smaller heap block, the GLI engine or a span inside it,
 * another GLI engine, the CGL context's public part, a span running past the
 * context's block or past its heap region, a span that would reach the start
 * of the GLI engine or of the CGL context, an address in an image, a stack,
 * read-only or unmapped memory, or a value that is no aligned user address. */
static inline enum nvk_cgl_layout
nvk_cgl_check_gld_layout(void *engine,
                         size_t gli_profile, size_t gli_cgl_dispatch, size_t gli_saved_dispatch,
                         size_t disp_offset, size_t cgl_context_size, size_t dispatch_table_size,
                         unsigned char *out_profile, void **out_context, void ***out_back,
                         struct nvk_cgl_layout_report *r)
{
   *r = (struct nvk_cgl_layout_report){ .dispatch_table_size = dispatch_table_size };
   /* The profile byte at gli_profile is the highest offset read in the GLI
    * context (gli_cgl_dispatch, gli_saved_dispatch and the share group are all
    * below it), so a block that spans it spans every read here. */
   if (engine && NVK_CGL_IN_ZONE(engine)) r->engine_size = NVK_CGL_BLOCK_SIZE(engine);
   if (!nvk_cgl_block_spans(engine, gli_profile + 1))
      return r->layout = NVK_CGL_LAYOUT_ENGINE_BLOCK;
   unsigned char profile = *(unsigned char *)((char *)engine + gli_profile);
   r->profile = profile;
   if (profile > 1)
      return r->layout = NVK_CGL_LAYOUT_PROFILE;
   void *dispatch = *(void **)((char *)engine + gli_cgl_dispatch);
   void **back = *(void ***)((char *)engine + gli_saved_dispatch);
   r->cgl_dispatch_null = !dispatch;
   r->saved_dispatch_null = !back;
   if (!dispatch || !back)
      return r->layout = NVK_CGL_LAYOUT_CONTEXT_NULL;
   void *context = (char *)dispatch - disp_offset;
   r->context = nvk_cgl_classify(context, engine, r->engine_size, NULL, 0,
                                 gli_profile, gli_cgl_dispatch, disp_offset);
   if (r->context.kind != NVK_CGL_WORD_BLOCK || r->context.size < cgl_context_size)
      return r->layout = NVK_CGL_LAYOUT_CONTEXT_BLOCK;
   r->context_size = r->context.size;
   r->rend = nvk_cgl_classify(*(void **)context, engine, r->engine_size, context, r->context_size,
                              gli_profile, gli_cgl_dispatch, disp_offset);
   r->back = nvk_cgl_classify(back, engine, r->engine_size, context, r->context_size,
                              gli_profile, gli_cgl_dispatch, disp_offset);
   enum nvk_cgl_word rk = r->rend.kind;
   int rend_ok = rk == NVK_CGL_WORD_NULL || rk == NVK_CGL_WORD_ENGINE || rk == NVK_CGL_WORD_WILD ||
                 (rk == NVK_CGL_WORD_LOOSE && (!r->rend.mapped || nvk_cgl_vm_heap(&r->rend.vm)));
   int back_ok = 0;
   switch (r->back.kind) {
   case NVK_CGL_WORD_BLOCK:
      back_ok = r->back.size >= dispatch_table_size; break;
   case NVK_CGL_WORD_IN_CONTEXT:
      back_ok = r->back.offset >= cgl_context_size &&
                r->back.offset + dispatch_table_size <= r->context_size;
      break;
   case NVK_CGL_WORD_LOOSE:
      /* A table that would reach the start of the GLI engine or of the CGL
       * context before it ends is not CGL's saved table. */
      r->back_runs_into = (uintptr_t)engine - r->back.value < dispatch_table_size ? 1
                        : (uintptr_t)context - r->back.value < dispatch_table_size ? 2 : 0;
      back_ok = r->back.mapped && nvk_cgl_vm_heap(&r->back.vm) && r->back.vm.avail >= dispatch_table_size &&
                !r->back_runs_into;
      break;
   default:
      back_ok = 0; break;
   }
   if (!rend_ok)
      return r->layout = NVK_CGL_LAYOUT_CONTEXT_REND;
   if (!back_ok)
      return r->layout = NVK_CGL_LAYOUT_SAVED_BLOCK;
   *out_profile = profile;
   *out_context = context;
   *out_back = back;
   return r->layout = NVK_CGL_LAYOUT_OK;
}

static inline const char *nvk_cgl_word_phrase(char *buf, size_t n, const struct nvk_cgl_word_class *c)
{
   switch (c->kind) {
   case NVK_CGL_WORD_NULL: snprintf(buf, n, "null"); break;
   case NVK_CGL_WORD_ENGINE: snprintf(buf, n, "this GLI engine"); break;
   case NVK_CGL_WORD_SIBLING:
      snprintf(buf, n, "another GLI engine of this CGL context, a heap block of 0x%zx bytes", c->size); break;
   case NVK_CGL_WORD_BLOCK: snprintf(buf, n, "a heap block of 0x%zx bytes", c->size); break;
   case NVK_CGL_WORD_IN_ENGINE: snprintf(buf, n, "inside this GLI engine at +0x%zx", c->offset); break;
   case NVK_CGL_WORD_IN_CONTEXT: snprintf(buf, n, "inside the CGL context at +0x%zx", c->offset); break;
   case NVK_CGL_WORD_LOOSE:
      if (c->mapped)
         snprintf(buf, n, "0x%jx, which starts no heap block, is inside neither the GLI engine nor the CGL context "
                  "and lies in a region of VM tag %u with protection %d, 0x%zx bytes before its end",
                  (uintmax_t)c->value, c->vm.tag, c->vm.prot, c->vm.avail);
      else
         snprintf(buf, n, "0x%jx, which starts no heap block, is inside neither the GLI engine nor the CGL context "
                  "and lies in no mapped region", (uintmax_t)c->value);
      break;
   case NVK_CGL_WORD_WILD:
      snprintf(buf, n, "0x%jx, which is no aligned user-space address", (uintmax_t)c->value); break;
   }
   return buf;
}

/* The text layout_refused_once prints between the parentheses: the check that
 * refused and what the memory held. It holds no parenthesis. */
static inline void nvk_cgl_layout_reason(char *buf, size_t n, const struct nvk_cgl_layout_report *r)
{
   char a[256], b[256];
   switch (r->layout) {
   case NVK_CGL_LAYOUT_OK:
      snprintf(buf, n, "the layout matches the record"); break;
   case NVK_CGL_LAYOUT_ENGINE_BLOCK:
      if (r->engine_size)
         snprintf(buf, n, "the GLI context is a heap block of 0x%zx bytes, too small for the recorded reads",
                  r->engine_size);
      else
         snprintf(buf, n, "the GLI context starts no heap block");
      break;
   case NVK_CGL_LAYOUT_PROFILE:
      snprintf(buf, n, "the GLI profile byte is %u, neither 0 for legacy nor 1 for core", r->profile); break;
   case NVK_CGL_LAYOUT_CONTEXT_NULL:
      snprintf(buf, n, "the GLI context's %s null",
               r->cgl_dispatch_null && r->saved_dispatch_null ? "CGL and saved dispatch pointers are"
               : r->cgl_dispatch_null ? "CGL dispatch pointer is" : "saved dispatch pointer is");
      break;
   case NVK_CGL_LAYOUT_CONTEXT_BLOCK:
      snprintf(buf, n, "the CGL context derived from the GLI context is %s, not a heap block of its own "
               "holding struct _CGLContextObject", nvk_cgl_word_phrase(a, sizeof(a), &r->context));
      break;
   case NVK_CGL_LAYOUT_CONTEXT_REND:
      snprintf(buf, n, "the CGL context's renderer word is %s; the saved dispatch table is %s; "
               "this GLI engine is a heap block of 0x%zx bytes and the CGL context one of 0x%zx bytes",
               nvk_cgl_word_phrase(a, sizeof(a), &r->rend), nvk_cgl_word_phrase(b, sizeof(b), &r->back),
               r->engine_size, r->context_size);
      break;
   case NVK_CGL_LAYOUT_SAVED_BLOCK:
      snprintf(buf, n, "the saved dispatch table is %s, where the record rules out a GLIFunctionDispatch "
               "of 0x%zx bytes%s; the CGL context's renderer word is %s; "
               "this GLI engine is a heap block of 0x%zx bytes and the CGL context one of 0x%zx bytes",
               nvk_cgl_word_phrase(a, sizeof(a), &r->back), r->dispatch_table_size,
               r->back_runs_into == 1 ? ", which would run into this GLI engine"
               : r->back_runs_into == 2 ? ", which would run into the CGL context" : "",
               nvk_cgl_word_phrase(b, sizeof(b), &r->rend), r->engine_size, r->context_size);
      break;
   }
}

#endif
