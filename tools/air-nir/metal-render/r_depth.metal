// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// depth written by the fragment stage, into a depth attachment
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; };
vertex VOut rz_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]]) { VOut o; o.pos = float4(p[vid], 0.5, 1); return o; }
struct FOut { float4 c [[color(0)]]; float d [[depth(any)]]; };
fragment FOut rz_fs(VOut in [[stage_in]])
{
   FOut o;
   o.c = float4(in.pos.z, in.pos.w, 0, 1); o.d = (in.pos.x + in.pos.y) / 256.0;
   return o;
}
