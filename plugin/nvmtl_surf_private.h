/* nvmtl_surf_private.h - a single-plane IOSurface drawn through a private image: why, and the binds that need not wait
   for its copy-out.
   A single-plane surface (a window's drawable, an OpenGL view's surface) whose pixels cannot live in the accelerator
   family's VRAM is drawn through an image of this process and copied to the surface's pages after every GPU write
   (nvmtlSurfaceOutDirtied:), so that WindowServer composites it from those pages. Which condition sent it there was
   written only to the plugin's own log, which a release plugin keeps only for a process started with
   NVMTL_PROCESS_ENV=1. Each reason now reaches the system log once per process (nvmtl_spv_first), with the word the
   release log rule keeps ("refused", nvmtl_log_failure.h) and a fixed tag (NVMTL_SPV_TAG) to search for.
   Second, the copy-out holds the surface's monitor while it waits for the GPU. A command encoder that binds the surface
   in that time waited on the monitor in nvmtlSurfaceIn, only to find afterwards that the copy-out had made the image and
   the pages agree, and return without an upload. nvmtl_spv_bind_skips says when such a bind may return at once: the
   copy-out in flight is this process's own, from a private image whose surface no other process draws, and nothing
   that the wait would have acted on has moved since it began. NVMTL_SURFIN_SKIP_OWN_OUT=0 waits as before.
   The rules are pure, so tools/test_surf_private.c proves them without Metal. */
#ifndef NVMTL_SURF_PRIVATE_H
#define NVMTL_SURF_PRIVATE_H
#include <stdint.h>
#include <stddef.h>

#define NVMTL_SPV_TAG "private-surface:"
/* The note's line: surface id, texture width and height, the surface's width, height and row, the VkFormat, the reason's
   text. It says "refused", which keeps it in a release plugin's system log (nvmtl_log_failure.h). */
#define NVMTL_SPV_FORMAT NVMTL_SPV_TAG " family VRAM refused for surface %u %ux%u (surface %zux%zu, row %zu bytes, VkFormat %u): %s" \
    " - drawn through a private image and copied to the surface's pages after every GPU write (said once per process and reason)"

/* Why a single-plane surface took a private image, in the order nvmtl_surface_image_create tries the family's VRAM.
   The last one is not decided there: the surface is in the family's VRAM, but the kernel refused to mark it newest after
   a GPU write, so the copy-out copied it to the pages as it does for a private image. */
enum {
    NVMTL_SPV_SHARE_OFF = 0,     /* surface sharing is off for the whole process (nvmtl_vk_surface_share_on) */
    NVMTL_SPV_LARGER,            /* the texture is wider or taller than the surface */
    NVMTL_SPV_ROW32,             /* the surface's row is not a multiple of 32 bytes */
    NVMTL_SPV_NO_FD,             /* no descriptor could be opened for the family VRAM import */
    NVMTL_SPV_IMPORT,            /* NVK's import of the family VRAM failed (the kernel's status is on its "surfvram:" lines) */
    NVMTL_SPV_BUFFER,            /* a buffer over the imported VRAM was refused */
    NVMTL_SPV_ALIAS,             /* the linear image over the imported VRAM was refused */
    NVMTL_SPV_NO_STORAGE,        /* the linear import has no storage view and the wrapper needs one */
    NVMTL_SPV_NOT_RT,            /* the linear import is not a colour attachment and the wrapper may be drawn into */
    NVMTL_SPV_NO_TILED,          /* no tiled image for a shader-writable Managed wrapper */
    NVMTL_SPV_MARK_REFUSED,      /* in the family's VRAM, but its mark as newest was refused: copied out */
    NVMTL_SPV_COUNT
};

/* How far the attempt at the family's VRAM got, for nvmtl_spv_reason: the import's own answer (nvmtl_vk_surface_vram
   reports 1 no descriptor, 2 import refused, 3 buffer refused; 0 when it said nothing), then what came after it. */
enum { NVMTL_SPV_STAGE_NONE = 0, NVMTL_SPV_STAGE_IMPORT, NVMTL_SPV_STAGE_ALIAS, NVMTL_SPV_STAGE_NO_STORAGE,
       NVMTL_SPV_STAGE_NOT_RT, NVMTL_SPV_STAGE_NO_TILED };

/* The reason for one single-plane surface that took a private image. The first three are the checks made before any
   import, in the same order and with the same arithmetic as nvmtl_surface_image_create (bpp 0 counts as 4 bytes). */
static inline int nvmtl_spv_reason(int share_on, uint32_t w, uint32_t h, uint32_t bpp, size_t row, size_t surf_h,
                                   int stage, int import_why)
{
    if (!share_on) return NVMTL_SPV_SHARE_OFF;
    if ((size_t)w * (bpp ? bpp : 4) > row || h > surf_h) return NVMTL_SPV_LARGER;
    if (row % 32) return NVMTL_SPV_ROW32;
    switch (stage) {
    case NVMTL_SPV_STAGE_ALIAS:      return NVMTL_SPV_ALIAS;
    case NVMTL_SPV_STAGE_NO_STORAGE: return NVMTL_SPV_NO_STORAGE;
    case NVMTL_SPV_STAGE_NOT_RT:     return NVMTL_SPV_NOT_RT;
    case NVMTL_SPV_STAGE_NO_TILED:   return NVMTL_SPV_NO_TILED;
    default: break;
    }
    return import_why == 1 ? NVMTL_SPV_NO_FD : import_why == 3 ? NVMTL_SPV_BUFFER : NVMTL_SPV_IMPORT;
}

static inline const char *nvmtl_spv_text(int reason)
{
    static const char *const text[NVMTL_SPV_COUNT] = {
        "surface sharing is off in this process (NVK's import, external memory or DRM linear missing, NVMTL_NO_SURFACE_SHARE=1, or a GPU without a display of its own)",
        "the texture is larger than the surface",
        "the surface's row is not a multiple of 32 bytes",
        "no descriptor for the family VRAM import (a sandbox refused it)",
        "NVK's import of the family VRAM failed (the kernel's status is on its own \"surfvram: surface\" lines, the first 8 and every 1024th grant)",
        "a buffer over the imported VRAM was refused",
        "the linear image over the imported VRAM was refused",
        "the linear import has no storage view",
        "the linear import is not a colour attachment",
        "no tiled image for a shader-writable Managed wrapper",
        "it is in the family's VRAM but the kernel refused to mark it newest",
    };
    return reason >= 0 && reason < NVMTL_SPV_COUNT ? text[reason] : "unknown";
}

/* 1 the first time this process meets `reason` (one bit per reason in *seen, taken atomically), else 0. */
static inline int nvmtl_spv_first(uint32_t *seen, int reason)
{
    if (!seen || reason < 0 || reason >= NVMTL_SPV_COUNT) return 0;
    const uint32_t bit = 1u << reason;
    return !(__atomic_fetch_or(seen, bit, __ATOMIC_RELAXED) & bit);
}

/* A copy-out that may be passed by binds while it waits for the GPU: on, not WindowServer, a private image (not the
   family's VRAM, not a tiled image of it), a surface no other process was seen drawing (_clientWritten: a bind of such a
   surface first waits for the other process's GPU write, which only nvmtlSurfaceIn does), and an image that was in step
   with the surface before this copy-out began. */
static inline int nvmtl_spv_out_passable(int on, int window_server, int vram, int client_written, int synced)
{
    return on && !window_server && !vram && !client_written && synced;
}

/* A bind may return without nvmtlSurfaceIn when a passable copy-out of this process is in flight (out_busy) and nothing
   moved that the wait would have acted on: no zero fill this image was owed was lost (zero_lost), and no other writer of
   the surface's VRAM published since the copy-out began (gen_now == gen_at_out). After such a wait, nvmtlSurfaceIn
   finds the image in step with the surface and returns without an upload; when gen or the zero fill did move, the bind
   waits and nvmtlSurfaceIn decides as before. */
static inline int nvmtl_spv_bind_skips(uint32_t out_busy, int zero_lost, uint32_t gen_now, uint32_t gen_at_out)
{
    return out_busy && !zero_lost && gen_now == gen_at_out;
}

#endif
