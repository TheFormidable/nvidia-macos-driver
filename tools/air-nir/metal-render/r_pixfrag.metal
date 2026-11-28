// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A fragment function sampling through pixel-coordinate samplers: at the implicit level (the translator's fetch
// emulation: a fragment's implicit sample would let the hardware pick a level from derivatives), at level(0) (the
// hardware filter at coord / size), with clamp_to_zero, and with an offset; coordinates past both edges
#include <metal_stdlib>
using namespace metal;
constexpr sampler l_edge(coord::pixel, filter::linear, address::clamp_to_edge);
constexpr sampler l_zero(coord::pixel, filter::linear, address::clamp_to_zero);
struct VOut { float4 pos [[position]]; };
vertex VOut rpf_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]]) { VOut o; o.pos = float4(p[vid], 0, 1); return o; }
fragment float4 rpf_fs(VOut in [[stage_in]], texture2d<float> t [[texture(0)]])
{
   const float2 c = in.pos.xy * 0.25 - 1.625;   /* px / 4 - 1.5: -1.5 to 14.25 across a 12-texel texture */
   return float4(t.sample(l_edge, c).x, t.sample(l_zero, c).x, t.sample(l_edge, c, level(0)).x,
                 t.sample(l_zero, c + 0.25, int2(1, -1)).x);
}
