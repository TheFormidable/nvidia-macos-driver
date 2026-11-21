// nvmtl_tex_upload.h: ordering and lifetime decisions for asynchronous texture uploads.
//
// replaceRegion and IOSurface uploads currently submit a copy and wait for the GPU before returning
// (nvmtl_vk_image_write*), so a GL app that uploads many textures per frame stalls once per upload. An
// earlier attempt (community PR #96) deferred the wait but was reverted: nvmtl_vk_image_destroy frees the
// image and its memory at once, and IOSurface-backed textures are uploaded then destroyed, so a deferred
// copy could land in freed GPU memory.
//
// This is the decision logic for doing it safely, gated by NVMTL_ASYNC_TEX_UPLOAD. One async upload may be
// in flight per image; its in-flight handle is recorded on the image. Before anything that would overwrite,
// read back, CPU-map or free that image's memory, the pending upload is finished (fence waited) first. Pure,
// so the invariants are unit-tested without a GPU; the plugin supplies the fence primitives.
//
// Invariants (each an arm of test_tex_upload.c):
//   - synchronous path (flag off): a new upload submits-and-waits; nothing is ever left pending, so the
//     destroy/readback guard finds nothing to finish and is a no-op (this is what keeps the current path
//     byte-for-byte the same);
//   - a second upload to the same image finishes the first (it would otherwise overwrite a texture, or reuse
//     staging bytes, while the GPU is still copying);
//   - destroy, readback, CPU map and surface-out finish a pending upload first (no use-after-free: this is
//     the #96 regression this guard exists to prevent);
//   - a draw that merely SAMPLES the image needs no CPU finish: the upload's command buffer was submitted, so
//     queue order plus the image's TRANSFER->SHADER_READ barrier already order the copy before the sample;
//   - two uploads racing on one image lose no handle: the second store hands the first back to be finished;
//   - what pending uploads hold stays within NVMTL_UP_HELD_MAX uploads and NVMTL_UP_HELD_BYTES_MAX bytes.

#ifndef NVMTL_TEX_UPLOAD_H
#define NVMTL_TEX_UPLOAD_H

#include <stdint.h>

// Per-image async-upload bookkeeping. `infl` is an opaque handle to the in-flight submission (the plugin
// stores a pointer to the upload's small header, nvmtl_up_rec in nvmtl_up_reap.h, cast to uint64_t); 0 means no
// async upload is pending for this image.
typedef struct { uint64_t infl; } nvmtl_up_state;

typedef enum {
    NVMTL_UP_SUBMIT_WAIT  = 0,   // submit the copy and wait now (the synchronous default)
    NVMTL_UP_SUBMIT_ASYNC = 1,   // submit without waiting; the caller records the handle with nvmtl_up_set
} nvmtl_up_submit;

// Decide how to submit a new upload to an image. If a previous async upload to THIS image is still in
// flight, it must be finished first: its handle is returned through *finish_first (0 means none). The submit
// mode is async only when the flag is on.
static inline nvmtl_up_submit nvmtl_up_before_upload(const nvmtl_up_state *s, int async_flag,
                                                     uint64_t *finish_first)
{
    *finish_first = s ? s->infl : 0;
    return async_flag ? NVMTL_UP_SUBMIT_ASYNC : NVMTL_UP_SUBMIT_WAIT;
}

// What a consumer (destroy, readback, CPU map, surface-out) must finish before touching the image's memory:
// the pending in-flight handle, or 0 for nothing. On the synchronous path `infl` is never set, so this
// returns 0 and the consumer proceeds exactly as it does today.
static inline uint64_t nvmtl_up_before_consume(const nvmtl_up_state *s)
{
    return s ? s->infl : 0;
}

// Record the in-flight handle after an async submit and return the handle it replaces (0 for none). Two threads that
// upload to the same image at once both find no handle, both submit, and the second store finds the first one's
// handle. It is handed back so that the caller finishes it: overwriting it would leak that upload's staging buffer,
// command pool, query pool and fence, and leave its copy running when the image is freed. Clear the handle after the
// upload has been finished (waited).
static inline uint64_t nvmtl_up_set(nvmtl_up_state *s, uint64_t infl)
{
    const uint64_t old = s ? s->infl : 0;
    if (s) s->infl = infl;
    return old;
}
static inline void nvmtl_up_clear(nvmtl_up_state *s) { if (s) s->infl = 0; }

// How much the pending asynchronous uploads of the whole process may hold. A pending upload is kept until its image
// is uploaded again, read back, migrated or destroyed, or until a later upload finds its copy finished and gives it back
// (nvmtl_up_reap.h); without that reaper (NVMTL_ASYNCUP_REAP=0) a texture that is uploaded once and only sampled
// afterwards keeps it for its whole life. Each one holds its own host-visible staging buffer, the size of the upload, and also a
// command pool, a timestamp query pool and a fence. The byte limit bounds the memory; the count limit bounds the
// driver objects, which many small uploads would pile up long before they reach the byte limit. Without room the
// upload takes the synchronous path (the shared per-thread staging buffer and a wait), which holds nothing afterwards.
#define NVMTL_UP_HELD_MAX       256u
#define NVMTL_UP_HELD_BYTES_MAX (256ull << 20)

// 1 when one more asynchronous upload of `bytes` fits beside the `held_count` uploads holding `held_bytes` of staging.
// Written so that no sum can wrap, even when held_bytes is already past the limit.
static inline int nvmtl_up_room(uint64_t held_count, uint64_t held_bytes, uint64_t bytes)
{
    return held_count < NVMTL_UP_HELD_MAX && bytes <= NVMTL_UP_HELD_BYTES_MAX
        && held_bytes <= NVMTL_UP_HELD_BYTES_MAX - bytes;
}

#endif
