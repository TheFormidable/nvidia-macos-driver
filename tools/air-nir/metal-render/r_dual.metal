// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// dual-source blending (round 10): [[color(0), index(1)]] is the blend's second source (Location 0, Index 1)
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; float2 uv; };
vertex VOut rdual_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]])
{
   VOut o;
   o.pos = float4(p[vid], 0, 1); o.uv = float2(p[vid].x * 0.5 + 0.5, 0.5 - p[vid].y * 0.5);
   return o;
}
struct DOut { float4 c0 [[color(0), index(0)]]; float4 c1 [[color(0), index(1)]]; };
fragment DOut rdual_fs(VOut in [[stage_in]])
{
   DOut o;
   o.c0 = float4(in.uv, 0.5, 1);
   o.c1 = float4(0.25, 0.5, in.uv.y, 0.75);
   return o;
}
