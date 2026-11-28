// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Sampling an integer texture: through constexpr samplers (the nearest texel fetched, by each address mode: repeat,
// mirrored repeat, clamp_to_zero, clamp_to_edge at level 0 and 1) and an argument sampler (the hardware's nearest)
#include <metal_stdlib>
using namespace metal;

constexpr sampler is_rep(coord::normalized, address::repeat, filter::nearest);
constexpr sampler is_mir(coord::normalized, address::mirrored_repeat, filter::nearest);
constexpr sampler is_zero(coord::normalized, address::clamp_to_zero, filter::nearest);
constexpr sampler is_edge(coord::normalized, address::clamp_to_edge, filter::nearest, mip_filter::nearest);

kernel void tex_intsample(texture2d<uint, access::sample> t [[texture(0)]], sampler near [[sampler(0)]],
                          device uint4 *out [[buffer(0)]], device const float2 *uv [[buffer(1)]],
                          uint i [[thread_position_in_grid]])
{
   float2 c = uv[i];
   out[i * 5u] = t.sample(is_rep, c, level(0));
   out[i * 5u + 1u] = t.sample(is_mir, c, level(0));
   out[i * 5u + 2u] = t.sample(is_zero, c, level(0));
   out[i * 5u + 3u] = t.sample(is_edge, c, level(1));
   out[i * 5u + 4u] = t.sample(near, saturate(c), level(0));
}
