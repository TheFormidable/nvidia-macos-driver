// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// a fragment with a constexpr sampler calling a visible function linked at pipeline creation (fragmentLinkedFunctions)
// that has a constexpr sampler of its own: both modules name theirs @__air_sampler_state, with different states (as
// RenderBox's custom_effect_fragment and the effect linked into it do); each must sample with its own
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; float2 uv; };
vertex VOut rls_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]])
{
   VOut o;
   o.pos = float4(p[vid], 0, 1); o.uv = float2(p[vid].x * 0.5 + 0.5, 0.5 - p[vid].y * 0.5);
   return o;
}
[[visible]] float4 rls_fx(texture2d<float> t, float2 uv);
fragment float4 rls_fs(VOut in [[stage_in]], texture2d<float> t [[texture(0)]])
{
   constexpr sampler s(filter::nearest, address::clamp_to_zero);
   const float2 uv = in.uv * 2 - 0.5;
   return t.sample(s, uv) + rls_fx(t, uv) * 2;
}
