// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// a clip distance written by the vertex stage: the half of the target where it is negative is clipped away
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; float cd [[clip_distance]] [1]; float2 uv; };
vertex VOut rc_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]])
{
   VOut o;
   o.pos = float4(p[vid], 0, 1); o.cd[0] = p[vid].x - 0.25; o.uv = p[vid] * 0.5 + 0.5;
   return o;
}
struct FIn { float4 pos [[position]]; float2 uv; };
fragment float4 rc_fs(FIn in [[stage_in]]) { return float4(in.uv, 0.75, 1); }
