// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A tile function (round 11): rt_fs draws a value of the pixel's position into two attachments, then rt_tile runs once
// per 16x16 tile over the pass's imageblock, the attachments its implicit layout: red times setTileBytes' g, the second
// attachment plus one, the third's (BGRA8Unorm) red inverted (tools/tile_oracle measures the same on an Apple GPU). The plugin runs it as a compute kernel
// between two render-pass segments (NVMTL_TILE_SHADING=1); with the road on, that kernel takes it.
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; };
struct FOut { float4 c [[color(0)]]; float d [[color(1)]]; float4 e [[color(2)]]; };
vertex VOut rt_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]]) { VOut o; o.pos = float4(p[vid], 0, 1); return o; }
fragment FOut rt_fs(VOut in [[stage_in]])
{
   FOut o;
   o.c = float4(in.pos.x / 64.0f, in.pos.y / 64.0f, 0.25f, 1.0f);
   o.d = floor(in.pos.y) * 64.0f + floor(in.pos.x);
   o.e = float4(floor(in.pos.x) / 255.0f, floor(in.pos.y) / 255.0f, 128.0f / 255.0f, 1.0f);   /* BGRA8Unorm */
   return o;
}
struct Px { float4 c [[color(0)]]; float d [[color(1)]]; float4 e [[color(2)]]; };
kernel void rt_tile(imageblock<Px, imageblock_layout_implicit> ib, ushort2 tid [[thread_position_in_threadgroup]],
                    constant float &g [[buffer(0)]])
{
   Px v = ib.read(tid);
   v.c = float4(v.c.x * g, v.c.yzw);
   v.d += 1.0f;
   v.e = float4(1.0f - v.e.x, v.e.yzw);
   ib.write(v, tid);
}
