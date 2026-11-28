// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// function constants in a fragment function
#include <metal_stdlib>
using namespace metal;
constant bool fc_red [[function_constant(0)]];
constant float fc_scale [[function_constant(1)]];
constant int fc_mode [[function_constant(2)]];
struct VOut { float4 pos [[position]]; float2 uv; };
vertex VOut rfc_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]])
{
   VOut o;
   o.pos = float4(p[vid], 0, 1); o.uv = float2(p[vid].x * 0.5 + 0.5, 0.5 - p[vid].y * 0.5);
   return o;
}
fragment float4 rfc_fs(VOut in [[stage_in]])
{
   float4 c = fc_red ? float4(1, 0, 0, 1) : float4(0, 0, 1, 1);
   c.g = fc_mode == 2 ? in.uv.x * fc_scale : in.uv.y;
   return c;
}
