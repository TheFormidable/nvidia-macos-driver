// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// point_size and point_coord with a point draw
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; float size [[point_size]]; };
vertex VOut rpt_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]]) { VOut o; o.pos = float4(p[vid], 0, 1); o.size = 8; return o; }
fragment float4 rpt_fs(VOut in [[stage_in]], float2 pc [[point_coord]]) { return float4(pc, 0, 1); }
