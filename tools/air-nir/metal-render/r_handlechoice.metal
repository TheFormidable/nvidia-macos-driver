// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A texture call through a phi of texture and sampler handles chosen by a known value, as Apple's GL compiles a GLSL
// helper that takes a sampler2D (Minecraft's sample_lightmap): plugin-run.sh builds it with -O0, so the choice reaches
// the frontend as a switch on a literal (after inlining) and a phi of the argument handles
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; float2 uv; };
vertex VOut rhc_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]])
{
   VOut o;
   o.pos = float4(p[vid], 0, 1); o.uv = float2(p[vid].x * 0.5 + 0.5, 0.5 - p[vid].y * 0.5);
   return o;
}
static float4 pick(int which, texture2d<float> t0, texture2d<float> t1, texture2d<float> t2, sampler s0, sampler s1, float2 uv)
{
   texture2d<float> t = t0;
   sampler s = s0;
   switch (which) {
   case 1: t = t1; s = s1; break;
   case 2: t = t2; s = s1; break;
   default: break;
   }
   return t.sample(s, uv);
}
fragment float4 rhc_fs(VOut in [[stage_in]], texture2d<float> t0 [[texture(0)]], texture2d<float> t1 [[texture(1)]],
                       texture2d<float> t2 [[texture(2)]], sampler s0 [[sampler(0)]], sampler s1 [[sampler(1)]])
{
   return pick(2, t0, t1, t2, s0, s1, in.uv) + pick(0, t0, t1, t2, s0, s1, in.uv) * 4.0;
}
