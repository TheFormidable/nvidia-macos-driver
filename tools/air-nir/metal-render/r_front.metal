// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// front_facing with culling off: a clockwise and a counter-clockwise triangle
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; };
vertex VOut rf_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]]) { VOut o; o.pos = float4(p[vid], 0, 1); return o; }
fragment float4 rf_fs(VOut in [[stage_in]], bool front [[front_facing]])
{
   return front ? float4(1, 0, 0, 1) : float4(0, 1, 0, 1);
}
