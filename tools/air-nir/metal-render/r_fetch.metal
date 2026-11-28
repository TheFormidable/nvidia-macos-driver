// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// framebuffer fetch: [[color(n)]] read and blended by hand; two instances of one quad, so the second reads what the
// first wrote (the plugin draws such a draw one triangle at a time behind a by-region barrier, on either road)
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; float2 uv; uint layer [[flat]]; };
vertex VOut rff_vs(uint vid [[vertex_id]], uint iid [[instance_id]], constant float2 *p [[buffer(0)]])
{
   VOut o;
   o.pos = float4(p[vid], 0, 1); o.uv = float2(p[vid].x * 0.5 + 0.5, 0.5 - p[vid].y * 0.5); o.layer = iid;
   return o;
}
// one RGBA32Float attachment read as a float4
fragment float4 rff_fs(VOut in [[stage_in]], float4 dst [[color(0)]])
{
   const float4 src = float4(in.uv, in.layer ? 0.75 : 0.25, 1);
   return dst * 0.5 + src * 0.5;
}
// three attachments: RGBA8Unorm read as a half4 (swizzled), R32Float as a float, R32Uint as a uint
struct FOut { half4 c0 [[color(0)]]; float c1 [[color(1)]]; uint c2 [[color(2)]]; };
fragment FOut rfm_fs(VOut in [[stage_in]], half4 d0 [[color(0)]], float d1 [[color(1)]], uint d2 [[color(2)]])
{
   FOut o;
   const half4 src = half4(half2(in.uv), in.layer ? 0.75h : 0.25h, 1.0h);
   o.c0 = d0.bgra * 0.5h + src * 0.5h;
   o.c1 = d1 * 2 + in.uv.x;
   o.c2 = in.layer ? d2 * 3 + 1 : uint(in.pos.x) + 7;
   return o;
}
// [[color(1)]] read with no attachment 1 (Core Animation's fragments read color(0..2) in one-attachment passes): the
// plugin's dummy image, zeros, on either road (Metal leaves the read undefined)
fragment float4 rfn_fs(VOut in [[stage_in]], float4 d0 [[color(0)]], float2 d1 [[color(1)]])
{
   return d0 * 0.5 + float4(in.uv, 0.5, 1) * 0.5 + float4(d1, 0, 0) * 1000;
}
