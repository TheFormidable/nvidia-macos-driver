// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Pixel-coordinate samples with repeat and mirrored_repeat, which Metal's compiler does not let a pixel-coordinate
// sampler have: plugin-run.sh rewrites the state words of the module (clamp_to_edge to repeat, clamp_to_zero to
// mirrored_repeat), as another compiler's AIR could carry them, and the translator emulates them (the coordinate
// wrapped into one period, each texel by the mode)
#include <metal_stdlib>
using namespace metal;
constexpr sampler l_rep(coord::pixel, filter::linear, address::clamp_to_edge);
constexpr sampler l_mir(coord::pixel, filter::linear, address::clamp_to_zero);
constexpr sampler n_rep(coord::pixel, filter::nearest, address::clamp_to_edge);
constexpr sampler n_mir(coord::pixel, filter::nearest, address::clamp_to_zero);
kernel void tex_pixwrap(texture1d<float> t1 [[texture(0)]], texture2d<float> t2 [[texture(1)]],
                        texture3d<float> t3 [[texture(2)]], texture2d_array<float> ta [[texture(3)]],
                        device const float4 *cs [[buffer(0)]], device float4 *o [[buffer(1)]],
                        uint i [[thread_position_in_grid]])
{
   const float4 c = cs[i];
   device float4 *r = o + i * 16;
   r[0] = t1.sample(l_rep, c.x);
   r[1] = t1.sample(l_mir, c.x);
   r[2] = t2.sample(l_rep, c.xy);
   r[3] = t2.sample(l_mir, c.xy, int2(1, 1));
   r[4] = t3.sample(l_rep, c.xyz);
   r[5] = t3.sample(n_mir, c.xyz);
   r[6] = ta.sample(l_mir, c.xy, uint(c.w));
   r[7] = t2.sample(n_rep, c.xy, level(1));
   r[8] = t2.gather(n_rep, c.xy);
   r[9] = t2.gather(l_mir, c.xy, int2(-1, 2), component::x);
   r[10] = t1.sample(n_mir, c.x);
   r[11] = ta.sample(n_rep, c.xy, uint(c.w));
   r[12] = t2.sample(l_rep, c.xy, level(1));
   r[13] = t3.sample(l_mir, c.xyz);
   r[14] = t1.sample(n_rep, c.x);
   r[15] = t2.sample(n_mir, c.xy, int2(2, -1));
}
