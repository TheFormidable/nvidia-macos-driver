/* nvmtl_dlss_views.h - the views DLSS keeps of an image, and when Frame Generation must show the core the previous frame.
   The view cache: DLSS reads and writes its own plain 2D views of the scaler's images (nvmtl_vk.c, dlss_view), made once
   per (image, format, aspect, read/write) and kept until that image is destroyed or given a new backing, because the
   core keeps the descriptor handle it took for a view across frames. A fixed table of 48 stopped caching once full and
   made a new view for every evaluation that was never destroyed; this table grows instead. An entry lives exactly as
   long as its image (at most two per image: the input view and the output view), so nothing needs evicting, and no
   view is ever destroyed while an evaluation may still hold it.
   Frame Generation primes the core's history with prevColorTexture unless the core already saw that frame as the last
   encode's colorTexture. The previous encode's colour texture and prevColorTexture can be two Metal objects for one
   image (a texture view of it): the core is then handed the very same view, so the priming evaluation repeats the
   last one. The decision is pure so tools/test_dlss_views.c proves it without a GPU. */
#ifndef NVMTL_DLSS_VIEWS_H
#define NVMTL_DLSS_VIEWS_H
#include <stdint.h>
#include <stdlib.h>

/* The table starts at the size the fixed one had, so up to this many entries nothing changes. */
#define NVMTL_DVC_FIRST 48u

typedef struct { const void *img; uint32_t fmt; uint32_t aspect; int rw; void *v; } nvmtl_dvc_ent;
typedef struct { nvmtl_dvc_ent *e; uint32_t n, cap; } nvmtl_dvc;

/* The cached view for this key, or NULL. */
static inline void *nvmtl_dvc_find(const nvmtl_dvc *c, const void *img, uint32_t fmt, uint32_t aspect, int rw)
{
    if (!c || !img) return NULL;
    for (uint32_t k = 0; k < c->n; k++) {
        const nvmtl_dvc_ent *e = &c->e[k];
        if (e->img == img && e->fmt == fmt && e->aspect == aspect && e->rw == rw) return e->v;
    }
    return NULL;
}

/* Keeps a new view. Returns 0, or -1 only when the table cannot grow (out of memory): the caller then keeps the old
   behaviour of a view made for this evaluation alone. */
static inline int nvmtl_dvc_add(nvmtl_dvc *c, const nvmtl_dvc_ent *in)
{
    if (!c || !in || !in->img || !in->v) return -1;
    if (c->n == c->cap) {
        const uint32_t cap = c->cap ? c->cap * 2u : NVMTL_DVC_FIRST;
        if (cap <= c->cap) return -1;
        nvmtl_dvc_ent *e = (nvmtl_dvc_ent *)realloc(c->e, (size_t)cap * sizeof *e);
        if (!e) return -1;
        c->e = e; c->cap = cap;
    }
    c->e[c->n++] = *in;
    return 0;
}

/* Removes up to `max` entries of this image and returns how many, with their views in out[]; the caller destroys them.
   Keys are unique, so moving the last entry into the hole changes no lookup. Call again until it returns 0. */
static inline uint32_t nvmtl_dvc_take(nvmtl_dvc *c, const void *img, void **out, uint32_t max)
{
    uint32_t got = 0;
    if (!c || !img) return 0;
    for (uint32_t k = 0; k < c->n && got < max;) {
        if (c->e[k].img == img) { out[got++] = c->e[k].v; c->e[k] = c->e[--c->n]; }
        else k++;
    }
    return got;
}

/* What DLSS builds its resource from (nvmtl_vk.c, dlss_image: the image, its format, which also gives the aspect, and
   its size). Two textures with equal values here are handed to the core as the same cached view. */
typedef struct { const void *img; uint32_t fmt, w, h, samples; } nvmtl_dlss_src;

static inline int nvmtl_dlss_src_eq(nvmtl_dlss_src a, nvmtl_dlss_src b)
{
    return a.img && a.img == b.img && a.fmt == b.fmt && a.w == b.w && a.h == b.h && a.samples == b.samples;
}

/* Whether Frame Generation records the priming evaluation with prevColorTexture before the current frame.
   reset: shouldResetHistory; have_prev: a previous colour texture is set; have_history: the last encode recorded;
   same_object: prevColorTexture is the last encode's colorTexture object. Besides those, the priming is skipped when
   prevColorTexture is the image the core was handed last time (last_seen, kept at that encode) and the last encode's
   colour texture still owns that image now (last_now): while that texture keeps that image no other image can carry the
   same handle, and an image given a new backing since does not match, so it is primed as before. (Two new backings
   between encodes can bring the old handle value back; the texture then still holds that image, and the decision is
   the one the same-object case makes.) */
static inline int nvmtl_dlss_fg_prime(int reset, int have_prev, int have_history, int same_object,
                                      nvmtl_dlss_src prev, nvmtl_dlss_src last_seen, nvmtl_dlss_src last_now)
{
    if (reset || !have_prev) return 0;
    if (!have_history) return 1;
    if (same_object) return 0;
    return !(nvmtl_dlss_src_eq(prev, last_seen) && nvmtl_dlss_src_eq(last_now, last_seen));
}

#endif
