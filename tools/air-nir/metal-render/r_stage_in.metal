// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// stage_in from a vertex descriptor: float2, float3, float4, uchar4 normalized, half2 and uchar4 (integer) attributes
#include <metal_stdlib>
using namespace metal;
struct VIn { float2 pos [[attribute(0)]]; float3 a3 [[attribute(1)]]; float4 a4 [[attribute(2)]]; float4 un [[attribute(3)]];
             half2 h2 [[attribute(4)]]; uchar4 ub [[attribute(5)]]; };
struct V2F { float4 pos [[position]]; float3 a3; float4 a4; float4 un; float2 h2; float4 ub; };
vertex V2F rs_vs(VIn in [[stage_in]])
{
   V2F o;
   o.pos = float4(in.pos, 0, 1);
   o.a3 = in.a3; o.a4 = in.a4; o.un = in.un; o.h2 = float2(in.h2); o.ub = float4(in.ub);
   return o;
}
struct FOut { float4 c0 [[color(0)]]; float4 c1 [[color(1)]]; float4 c2 [[color(2)]]; float4 c3 [[color(3)]]; };
fragment FOut rs_fs(V2F in [[stage_in]])
{
   FOut o;
   o.c0 = float4(in.a3, in.h2.x); o.c1 = in.a4; o.c2 = in.un; o.c3 = float4(in.ub.xy / 256.0, in.h2.y, in.ub.z / 16.0 + in.ub.w);
   return o;
}
