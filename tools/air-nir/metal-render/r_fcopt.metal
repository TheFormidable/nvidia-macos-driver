// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// An optional function constant the pipeline never gives, as CoreAnimation's fixed_vert_*_gen leave theirs unset
// (fc_texcoord_count0/1): read straight (0, as the translator reads it), through is_function_constant_defined (false),
// and in a predicate the static initializer computes from it, which decides whether a vertex attribute and a varying
// exist. fco_gen is given (false in r_fcopt, true in r_fcopt_gen).
#include <metal_stdlib>
using namespace metal;
constant bool fco_gen [[function_constant(0)]];
constant uint fco_count [[function_constant(3)]];
constant bool fco_has_col = fco_gen || fco_count != 0;
constant uint fco_n = is_function_constant_defined(fco_count) ? fco_count : 5u;
struct VIn {
   float2 pos [[attribute(0)]];
   float4 col [[attribute(1), function_constant(fco_has_col)]];
};
struct V2F {
   float4 pos [[position]];
   float2 uv;
   float4 tint [[function_constant(fco_has_col)]];
};
vertex V2F rfop_vs(VIn in [[stage_in]])
{
   V2F o;
   o.pos = float4(in.pos, 0, 1);
   o.uv = in.pos * 0.5 + 0.5;
   if (fco_has_col) o.tint = in.col;
   return o;
}
fragment float4 rfop_fs(V2F in [[stage_in]])
{
   float4 c = float4(in.uv, float(fco_n) / 8.0, float(fco_count));
   if (fco_has_col) c = in.tint * float4(1, 1, 2, 1) + float4(0, 0, 0, float(fco_count));
   return c;
}
