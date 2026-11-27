// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// a fragment sampling through argument samplers bound with pixel coordinates (normalizedCoordinates NO): nearest and
// linear, clamp_to_edge, at the pixel coordinates of a 16x16 texture
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; float2 uv; };
vertex VOut rap_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]])
{
   VOut o;
   o.pos = float4(p[vid], 0, 1); o.uv = float2(p[vid].x * 0.5 + 0.5, 0.5 - p[vid].y * 0.5);
   return o;
}
struct FOut { float4 n [[color(0)]]; float4 l [[color(1)]]; };
fragment FOut rap_fs(VOut in [[stage_in]], texture2d<float> t [[texture(0)]], sampler sn [[sampler(0)]], sampler sl [[sampler(1)]])
{
   FOut o;
   o.n = t.sample(sn, in.uv * 16); o.l = t.sample(sl, in.uv * 16);
   return o;
}
