// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Constexpr samplers with pixel coordinates: nearest clamp_to_edge, nearest clamp_to_zero and linear clamp_to_edge,
// at texel centres, exact texel edges and outside the texture.
#include <metal_stdlib>
using namespace metal;

constexpr sampler pn(coord::pixel, address::clamp_to_edge, filter::nearest);
constexpr sampler pz(coord::pixel, address::clamp_to_zero, filter::nearest);
constexpr sampler pl(coord::pixel, address::clamp_to_edge, filter::linear);

kernel void tex_pixel(texture2d<float, access::sample> t [[texture(0)]], device const float2 *xy [[buffer(0)]],
                      device float4 *out [[buffer(1)]], uint i [[thread_position_in_grid]])
{
   float2 c = xy[i];
   out[i * 3u] = t.sample(pn, c);
   out[i * 3u + 1u] = t.sample(pz, c);
   out[i * 3u + 2u] = t.sample(pl, c);
}
