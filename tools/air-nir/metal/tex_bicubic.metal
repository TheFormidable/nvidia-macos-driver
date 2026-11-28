// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Bicubic samples (round 10) as the translator emulates them (images/sample.rs lower_pixel_bicubic_sample): the
// sixteen texels (four in 1D) around coord - 0.5 weighted by Catmull-Rom's weights, addressed by the sampler's modes;
// normalized coordinates scaled by the size first. Normalized coordinates from buffer(2), the pixel ones from buffer(0)
// (tex_pixmodes's textures and coordinates).
#include <metal_stdlib>
using namespace metal;
constexpr sampler b_edge(filter::bicubic, address::clamp_to_edge);
constexpr sampler b_rep(filter::bicubic, address::repeat);
constexpr sampler b_zero(filter::bicubic, address::clamp_to_zero);
constexpr sampler b_mir(filter::bicubic, address::mirrored_repeat);
constexpr sampler bp_edge(coord::pixel, filter::bicubic, address::clamp_to_edge);
constexpr sampler bp_zero(coord::pixel, filter::bicubic, address::clamp_to_zero);
kernel void tex_bicubic(texture1d<float> t1 [[texture(0)]], texture2d<float> t2 [[texture(1)]],
                        device const float4 *cs [[buffer(0)]], device float4 *o [[buffer(1)]],
                        device const float4 *ns [[buffer(2)]], uint i [[thread_position_in_grid]])
{
   const float4 c = cs[i], n = ns[i];
   device float4 *r = o + i * 16;
   r[0] = t1.sample(b_edge, n.x);
   r[1] = t2.sample(b_edge, n.xy);
   r[2] = t2.sample(b_rep, n.xy);
   r[3] = t2.sample(b_zero, n.xy);
   r[4] = t2.sample(b_mir, n.xy);
   r[5] = t2.sample(b_edge, n.xy, level(1));
   r[6] = t2.sample(b_edge, n.xy, int2(1, -1));
   r[7] = t2.sample(bp_edge, c.xy);
   r[8] = t2.sample(bp_zero, c.xy, int2(-1, 1));
   r[9] = t1.sample(bp_edge, c.x);
}
