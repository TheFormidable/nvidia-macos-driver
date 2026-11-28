// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// minimum-LOD clamps (round 10): a sample at the level its derivatives give (level 0 here: one texel per pixel) with
// bias(...) and min_lod_clamp(c) reads level c or above; so does a gradient sample. The translator applied a clamp
// only to gradient samples: the others read level 0.
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; float2 uv; };
vertex VOut rml_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]])
{
   VOut o;
   o.pos = float4(p[vid], 0, 1); o.uv = float2(p[vid].x * 0.5 + 0.5, 0.5 - p[vid].y * 0.5);
   return o;
}
fragment float4 rml_fs(VOut in [[stage_in]], texture2d<float> t [[texture(0)]], sampler s [[sampler(0)]],
                       constant float *k [[buffer(0)]])
{
   return float4(t.sample(s, in.uv, bias(0.0), min_lod_clamp(2.0)).x,
                 t.sample(s, in.uv, bias(k[1]), min_lod_clamp(k[0])).x,
                 t.sample(s, in.uv, gradient2d(dfdx(in.uv), dfdy(in.uv)), min_lod_clamp(3.0)).x,
                 t.sample(s, in.uv, min_lod_clamp(k[2])).x);
}
