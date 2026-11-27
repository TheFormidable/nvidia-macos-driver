// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// varyings with user semantics, declared in another order in the fragment stage: the link moves them
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; float4 a [[user(alpha)]]; float2 b [[user(beta)]]; float c [[user(gamma)]]; float d [[user(delta)]]; };
vertex VOut rl_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]])
{
   const float2 q = p[vid], u = q * 0.5 + 0.5;
   VOut o;
   o.pos = float4(q, 0, 1);
   o.a = float4(u.x, u.y, 1 - u.x, 0.5); o.b = float2(2 * u.y, 0.25); o.c = u.x + u.y; o.d = 3 - u.x;
   return o;
}
struct FIn { float d [[user(delta)]]; float c [[user(gamma)]]; float4 pos [[position]]; float2 b [[user(beta)]]; float4 a [[user(alpha)]]; };
struct FOut { float4 c0 [[color(0)]]; float4 c1 [[color(1)]]; };
fragment FOut rl_fs(FIn in [[stage_in]])
{
   FOut o;
   o.c0 = in.a; o.c1 = float4(in.b, in.c, in.d);
   return o;
}
