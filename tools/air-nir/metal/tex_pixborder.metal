// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A pixel-coordinate sampler clamping to an opaque border: the translator emulates only a transparent-black border
// (reflect::StaticSamplerState::validate_lowering), so the frontend refuses it too
#include <metal_stdlib>
using namespace metal;
constexpr sampler l_white(coord::pixel, filter::linear, address::clamp_to_border, border_color::opaque_white);
kernel void tex_pixborder(texture2d<float> t2 [[texture(1)]], device const float4 *cs [[buffer(0)]], device float4 *o [[buffer(1)]],
                          uint i [[thread_position_in_grid]])
{
   o[i] = t2.sample(l_white, cs[i].xy);
}
