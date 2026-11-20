/* nvmtl_draw_cache.h - the draw bindings a VkCommandBuffer already holds, so a draw does not record them again.
   Vulkan keeps the index buffer, every vertex buffer binding, the primitive topology and primitive restart in a
   command buffer until they are set again: binding a pipeline, beginning or ending a render pass and a barrier leave
   them as they are (all of the plugin's graphics pipelines take topology and restart as dynamic state, and NVK's own
   clears and blits save and restore what they change). The plugin set all of them again before every draw, and NVK does
   not drop a repeated index or vertex buffer bind: each one is a pushbuffer write and a macro run on the GPU. Topology
   and restart repeats are dropped by Mesa's runtime already, so for those this saves only the calls.

   The cache lives in nvk_cmdbuf, which nvmtl_vk_cmd_begin clears for every new VkCommandBuffer, and zero means
   "nothing known": a fresh command buffer records everything. Whoever records into the command buffer behind the cache
   (a patch draw sets its own topology, NGX records DLSS) says so with nvmtl_dc_topo_unknown or nvmtl_dc_invalidate.
   NVMTL_BIND_CACHE=0 records every bind again, as before; WindowServer always does. The decisions are pure, so
   tools/test_draw_cache.c proves them without a GPU. */
#ifndef NVMTL_DRAW_CACHE_H
#define NVMTL_DRAW_CACHE_H
#include <stdint.h>

#define NVMTL_DC_NVIN 31   /* vertex buffer bindings (NVMTL_NVIN) */
typedef struct {
    uint32_t topo1;      /* the last VkPrimitiveTopology recorded, plus one; 0 = unknown */
    uint32_t restart1;   /* the last primitive restart enable recorded, plus one; 0 = unknown */
    uint64_t ib_buf, ib_off;   /* the bound index buffer (VkBuffer) and offset; ib_buf 0 = unknown */
    uint32_t ib_type1;         /* its VkIndexType plus one */
    uint64_t vb_buf[NVMTL_DC_NVIN], vb_off[NVMTL_DC_NVIN];   /* each vertex buffer binding; vb_buf 0 = unknown */
} nvmtl_draw_cache;

/* Something recorded into the command buffer without the cache: forget everything. */
static inline void nvmtl_dc_invalidate(nvmtl_draw_cache *d) { *d = (nvmtl_draw_cache){ 0 }; }

/* Topology and restart were set behind the cache (a patch draw): the next draw sets them again. */
static inline void nvmtl_dc_topo_unknown(nvmtl_draw_cache *d) { d->topo1 = 0; d->restart1 = 0; }

/* 1 when vkCmdSetPrimitiveTopology must be recorded; *emit_restart says the same for the restart enable. on = the cache
   is in use for this process. Records the values, which the caller then sets. */
static inline int nvmtl_dc_topo(nvmtl_draw_cache *d, int on, uint32_t topo, uint32_t restart, int *emit_restart)
{
    const int et = !on || d->topo1 != topo + 1u, er = !on || d->restart1 != restart + 1u;
    d->topo1 = topo + 1u; d->restart1 = restart + 1u;
    *emit_restart = er;
    return et;
}

/* 1 when vkCmdBindIndexBuffer must be recorded for (buf, off, type). A null buffer is never taken as known. */
static inline int nvmtl_dc_ib(nvmtl_draw_cache *d, int on, uint64_t buf, uint64_t off, uint32_t type)
{
    const int e = !on || !buf || d->ib_buf != buf || d->ib_off != off || d->ib_type1 != type + 1u;
    d->ib_buf = buf; d->ib_off = off; d->ib_type1 = type + 1u;
    return e;
}

/* 1 when vertex buffer binding i must be recorded for (buf, off). dyn: the bound pipeline takes this binding's stride
   as dynamic state, which only a bind with the stride sets, so that bind is always recorded (and still remembered: the
   buffer and offset it binds are what the binding holds afterwards). */
static inline int nvmtl_dc_vb(nvmtl_draw_cache *d, int on, uint32_t i, uint64_t buf, uint64_t off, int dyn)
{
    if (i >= NVMTL_DC_NVIN) return 1;
    const int e = !on || dyn || !buf || d->vb_buf[i] != buf || d->vb_off[i] != off;
    d->vb_buf[i] = buf; d->vb_off[i] = off;
    return e;
}

#endif
