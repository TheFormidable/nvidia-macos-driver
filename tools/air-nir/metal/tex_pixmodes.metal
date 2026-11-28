// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Pixel-coordinate samples as the translator lowers them: linear and nearest, clamp_to_edge and clamp_to_zero, of 1D,
// 2D, 3D and 2D array textures, at level 0 and 1, with offsets, and gathers; coordinates in range, past both edges and
// NaN. The 2D linear clamp_to_edge sample at level 0 without an offset is the hardware filter's; the rest the fetch
// emulation's (images/sample.rs, fetch_coord.rs, gather.rs)
#include <metal_stdlib>
using namespace metal;
constexpr sampler l_edge(coord::pixel, filter::linear, address::clamp_to_edge);
constexpr sampler l_zero(coord::pixel, filter::linear, address::clamp_to_zero);
constexpr sampler n_edge(coord::pixel, filter::nearest, address::clamp_to_edge);
constexpr sampler n_zero(coord::pixel, filter::nearest, address::clamp_to_zero);
kernel void tex_pixmodes(texture1d<float> t1 [[texture(0)]], texture2d<float> t2 [[texture(1)]],
                         texture3d<float> t3 [[texture(2)]], texture2d_array<float> ta [[texture(3)]],
                         device const float4 *cs [[buffer(0)]], device float4 *o [[buffer(1)]],
                         uint i [[thread_position_in_grid]])
{
   const float4 c = cs[i];
   device float4 *r = o + i * 16;
   r[0] = t1.sample(l_edge, c.x);
   r[1] = t1.sample(l_zero, c.x);
   r[2] = t1.sample(n_zero, c.x);
   r[3] = t2.sample(l_edge, c.xy);
   r[4] = t2.sample(l_zero, c.xy);
   r[5] = t2.sample(l_edge, c.xy, level(1));
   r[6] = t2.sample(l_edge, c.xy, int2(1, -1));
   r[7] = t2.sample(n_edge, c.xy, int2(-2, 1));
   r[8] = t3.sample(l_zero, c.xyz);
   r[9] = t3.sample(n_edge, c.xyz);
   r[10] = ta.sample(l_edge, c.xy, uint(c.w));
   r[11] = ta.sample(n_zero, c.xy, uint(c.w));
   r[12] = t2.gather(n_edge, c.xy);
   r[13] = t2.gather(l_zero, c.xy, int2(1, 0), component::y);
   r[14] = t1.sample(n_edge, c.x);
   r[15] = t3.sample(l_edge, c.xyz);
}
