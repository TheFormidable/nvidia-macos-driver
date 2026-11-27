// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// function constants in vertex outputs and in a colour index: a varying that exists only when a constant is true (it
// is), one that exists only when another is (it is not), and [[color(n)]] with n from a constant
#include <metal_stdlib>
using namespace metal;
constant bool fco_tint [[function_constant(0)]];
constant bool fco_extra [[function_constant(1)]];
constant int fco_target [[function_constant(2)]];
struct VOut {
   float4 pos [[position]];
   float2 uv;
   float4 tint [[function_constant(fco_tint)]];
   float4 extra [[function_constant(fco_extra)]];
};
vertex VOut rfo_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]])
{
   VOut o;
   o.pos = float4(p[vid], 0, 1); o.uv = float2(p[vid].x * 0.5 + 0.5, 0.5 - p[vid].y * 0.5);
   if (fco_tint) o.tint = float4(o.uv, 0.25, 1);
   if (fco_extra) o.extra = float4(9);
   return o;
}
struct FIn { float4 pos [[position]]; float2 uv; float4 tint; };
struct FOut { float4 c [[color(fco_target + 1)]]; };
fragment FOut rfo_fs(FIn in [[stage_in]])
{
   FOut o;
   o.c = in.tint * float4(1, 1, 2, 1);
   return o;
}
