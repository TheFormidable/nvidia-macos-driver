// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// two colour attachments: rgba8 and r32float
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; float2 uv; };
vertex VOut rm_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]])
{
   VOut o;
   o.pos = float4(p[vid], 0, 1); o.uv = float2(p[vid].x * 0.5 + 0.5, 0.5 - p[vid].y * 0.5);
   return o;
}
struct FOut { float4 c0 [[color(0)]]; float c1 [[color(1)]]; };
fragment FOut rm_fs(VOut in [[stage_in]])
{
   FOut o;
   o.c0 = float4(in.uv, 0.25, 1); o.c1 = in.uv.x * 100 + in.uv.y;
   return o;
}
