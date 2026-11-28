// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// [[primitive_id]] in a fragment function: the frontend refuses it, so the pipeline must take the SPIR-V road and draw
// right there
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; };
vertex VOut rq_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]]) { VOut o; o.pos = float4(p[vid], 0, 1); return o; }
fragment float4 rq_fs(VOut in [[stage_in]], uint prim [[primitive_id]]) { return float4(float(prim), 1, 0, 1); }
