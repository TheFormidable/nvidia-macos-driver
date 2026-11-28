// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// a fragment sampling a texture through an argument sampler (nearest) and a constexpr sampler (linear), at the level
// its derivatives give
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; float2 uv; };
vertex VOut rt_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]])
{
   VOut o;
   o.pos = float4(p[vid], 0, 1); o.uv = float2(p[vid].x * 0.5 + 0.5, 0.5 - p[vid].y * 0.5);
   return o;
}
struct FOut { float4 n [[color(0)]]; float4 l [[color(1)]]; };
fragment FOut rt_fs(VOut in [[stage_in]], texture2d<float> t [[texture(0)]], sampler s [[sampler(0)]])
{
   constexpr sampler ls(filter::linear, address::clamp_to_edge);
   FOut o;
   o.n = t.sample(s, in.uv); o.l = t.sample(ls, in.uv);
   return o;
}
