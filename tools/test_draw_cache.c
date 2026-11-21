/* test_draw_cache.c - the per-VkCommandBuffer cache of index buffer, vertex buffer, topology and restart binds
   (plugin/nvmtl_draw_cache.h) and its off switch NVMTL_BIND_CACHE=0.
   Build and run anywhere (no GPU, no Metal):  cc -std=c11 -Wall -Wextra -I plugin tools/test_draw_cache.c -o /tmp/test_draw_cache && /tmp/test_draw_cache
   A small model of the command buffer state Vulkan keeps is driven next to the cache: after every step the state the
   cached recording leaves must equal the state recording everything leaves. Each arm is a sentence about a draw that
   would read the wrong binding if the rule were wrong. */
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "nvmtl_draw_cache.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }

/* what the VkCommandBuffer holds, and how many commands were recorded to get there */
typedef struct { uint32_t topo, restart; uint64_t ib, iboff; uint32_t ibtype; uint64_t vb[NVMTL_DC_NVIN], vboff[NVMTL_DC_NVIN]; unsigned cmds; } gpu;
typedef struct { gpu g; nvmtl_draw_cache d; int on; } rec;

static void topo(rec *r, uint32_t t, uint32_t restart)
{
    int er; if (nvmtl_dc_topo(&r->d, r->on, t, restart, &er)) { r->g.topo = t; r->g.cmds++; }
    if (er) { r->g.restart = restart; r->g.cmds++; }
}
static void ib(rec *r, uint64_t b, uint64_t off, uint32_t type)
{ if (nvmtl_dc_ib(&r->d, r->on, b, off, type)) { r->g.ib = b; r->g.iboff = off; r->g.ibtype = type; r->g.cmds++; } }
static void vb(rec *r, uint32_t i, uint64_t b, uint64_t off, int dyn)
{ if (nvmtl_dc_vb(&r->d, r->on, i, b, off, dyn)) { r->g.vb[i] = b; r->g.vboff[i] = off; r->g.cmds++; } }
/* a patch draw sets the topology itself, then tells the cache */
static void patches(rec *r) { r->g.topo = 3 /* PATCH_LIST stand-in */ + 100; r->g.restart = 0; r->g.cmds += 2; nvmtl_dc_topo_unknown(&r->d); }
/* NGX records into the command buffer: here it happens to bind a vertex buffer and an index buffer of its own */
static void ngx(rec *r) { r->g.vb[0] = 0x9990; r->g.vboff[0] = 0; r->g.ib = 0x9991; r->g.iboff = 0; r->g.cmds += 2; nvmtl_dc_invalidate(&r->d); }
/* a new VkCommandBuffer: nvmtl_vk_cmd_begin clears nvk_cmdbuf, and Vulkan's state is undefined (modelled as junk) */
static void begin(rec *r) { const unsigned n = r->g.cmds; memset(&r->g, 0xA5, sizeof r->g); r->g.cmds = n; memset(&r->d, 0, sizeof r->d); }

static int same(const gpu *a, const gpu *b)
{
    gpu x = *a, y = *b; x.cmds = y.cmds = 0;
    return !memcmp(&x, &y, sizeof x);
}

int main(void)
{
    nvmtl_draw_cache d; memset(&d, 0, sizeof d); int er;
    arm("a fresh command buffer records the topology, POINT_LIST (0) included", nvmtl_dc_topo(&d, 1, 0, 0, &er) && er);
    arm("the same topology and restart again record nothing", !nvmtl_dc_topo(&d, 1, 0, 0, &er) && !er);
    arm("a new topology records only the topology", nvmtl_dc_topo(&d, 1, 3, 0, &er) && !er);
    arm("restart turned on between two strip draws records only the restart", !nvmtl_dc_topo(&d, 1, 3, 1, &er) && er);
    nvmtl_dc_topo_unknown(&d);
    arm("after a patch draw the next topology and restart are recorded again", nvmtl_dc_topo(&d, 1, 3, 1, &er) && er);
    arm("a fresh index buffer is bound", nvmtl_dc_ib(&d, 1, 0x1000, 0, 0));
    arm("the same index buffer, offset and type are not bound again", !nvmtl_dc_ib(&d, 1, 0x1000, 0, 0));
    arm("a new index type binds again (it also sets the restart index)", nvmtl_dc_ib(&d, 1, 0x1000, 0, 1));
    arm("a new index offset binds again", nvmtl_dc_ib(&d, 1, 0x1000, 64, 1));
    arm("another index buffer binds again", nvmtl_dc_ib(&d, 1, 0x2000, 64, 1));
    arm("a null index buffer is never taken as already bound", nvmtl_dc_ib(&d, 1, 0, 0, 0) && nvmtl_dc_ib(&d, 1, 0, 0, 0));
    arm("a fresh vertex buffer binding is bound", nvmtl_dc_vb(&d, 1, 0, 0x2000, 0, 0));
    arm("the same vertex buffer and offset are not bound again", !nvmtl_dc_vb(&d, 1, 0, 0x2000, 0, 0));
    arm("a binding with a dynamic stride is always bound", nvmtl_dc_vb(&d, 1, 0, 0x2000, 0, 1) && nvmtl_dc_vb(&d, 1, 0, 0x2000, 0, 1));
    arm("after a dynamic-stride bind the same buffer is still known to the next static pipeline", !nvmtl_dc_vb(&d, 1, 0, 0x2000, 0, 0));
    arm("another binding index is its own entry", nvmtl_dc_vb(&d, 1, 1, 0x2000, 0, 0));
    arm("a new vertex buffer offset binds again", nvmtl_dc_vb(&d, 1, 1, 0x2000, 16, 0));
    arm("a binding index past the table is always bound", nvmtl_dc_vb(&d, 1, NVMTL_DC_NVIN, 0x2000, 0, 0)
        && nvmtl_dc_vb(&d, 1, NVMTL_DC_NVIN, 0x2000, 0, 0));
    nvmtl_dc_invalidate(&d);
    arm("after an invalidation everything is recorded again",
        nvmtl_dc_ib(&d, 1, 0x2000, 64, 1) && nvmtl_dc_vb(&d, 1, 0, 0x2000, 0, 0) && nvmtl_dc_topo(&d, 1, 3, 1, &er) && er);
    memset(&d, 0, sizeof d);
    arm("a zeroed cache (a new VkCommandBuffer) knows nothing", nvmtl_dc_ib(&d, 1, 0x2000, 64, 1) && nvmtl_dc_vb(&d, 1, 0, 0x2000, 0, 0));
    arm("NVMTL_BIND_CACHE=0 records every bind, repeats included",
        nvmtl_dc_ib(&d, 0, 0x2000, 64, 1) && nvmtl_dc_ib(&d, 0, 0x2000, 64, 1) && nvmtl_dc_vb(&d, 0, 0, 0x2000, 0, 0)
        && nvmtl_dc_vb(&d, 0, 0, 0x2000, 0, 0) && nvmtl_dc_topo(&d, 0, 3, 1, &er) && er && nvmtl_dc_topo(&d, 0, 3, 1, &er) && er);

    /* A pseudo-random sequence of draws, patch draws, NGX recordings and new command buffers: the cached recording must
       leave the command buffer in the state the full recording leaves after every step, and record fewer commands. */
    rec on, off; memset(&on, 0, sizeof on); memset(&off, 0, sizeof off); on.on = 1; off.on = 0;
    begin(&on); begin(&off);
    uint32_t s = 12345; int diverged = 0; unsigned steps = 0;
    for (int n = 0; n < 200000; n++) {
        s = s * 1103515245u + 12345u; const uint32_t k = (s >> 16) % 100;
        rec *rs[2] = { &on, &off };
        for (int j = 0; j < 2; j++) {
            rec *r = rs[j];
            if (k < 1) begin(r);
            else if (k < 2) patches(r);
            else if (k < 3) ngx(r);
            else {
                /* a draw: a topology from a few, restart from the strip ones, an index buffer from a few, a vertex
                   layout of two bindings, the first sometimes with a dynamic stride */
                const uint32_t t = (s >> 4) % 3, rst = t == 2 && ((s >> 7) & 1);
                topo(r, t, rst);
                if ((s >> 9) & 1) ib(r, 0x1000 + ((s >> 10) % 3) * 0x100, ((s >> 12) % 2) * 64, (s >> 13) & 1);
                vb(r, 0, 0x5000 + ((s >> 14) % 2) * 0x100, 0, (s >> 15) % 7 == 0);
                vb(r, 1, 0x6000, ((s >> 18) % 3) * 32, 0);
                /* a draw reads what it set: only the fields it set are compared */
                if (r->g.topo != t || r->g.restart != rst || r->g.vb[0] != 0x5000 + ((s >> 14) % 2) * 0x100 || r->g.vb[1] != 0x6000
                    || r->g.vboff[1] != ((s >> 18) % 3) * 32) diverged++;
            }
        }
        steps++;
        if (!same(&on.g, &off.g)) diverged++;
    }
    arm("after every step of a long random sequence the cached recording holds what the full one holds", diverged == 0);
    {   /* control: a cache that forgot the patch draw's topology would be seen */
        rec a, b; memset(&a, 0, sizeof a); memset(&b, 0, sizeof b); a.on = 1; begin(&a); begin(&b);
        topo(&a, 3, 0); topo(&b, 3, 0);
        a.g.topo = 103; a.g.cmds++;           /* patch draw without nvmtl_dc_topo_unknown */
        patches(&b);
        topo(&a, 3, 0); topo(&b, 3, 0);
        arm("control: a patch draw that did not tell the cache leaves the wrong topology, and the check sees it", !same(&a.g, &b.g));
    }
    arm("the cache records fewer commands than recording everything", on.g.cmds < off.g.cmds);
    printf("%u steps\n", steps);

    printf(fails ? "FAILED %d\n" : "ok\n", fails);
    return fails != 0;
}
