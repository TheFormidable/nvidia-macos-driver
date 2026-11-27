// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A nearest pixel-coordinate constexpr sampler on a 2D-array texture (as MPS's convolutions sample), and
// metal::uniform<T> (air.is_uniform).
#include <metal_stdlib>
using namespace metal;

constexpr sampler pa(coord::pixel, address::clamp_to_edge, filter::nearest);

kernel void tex_pixel_array(texture2d_array<float, access::sample> t [[texture(0)]], device const float2 *xy [[buffer(0)]],
                            device const uint *layer [[buffer(1)]], device float4 *out [[buffer(2)]],
                            device uint *uout [[buffer(3)]], uint i [[thread_position_in_grid]])
{
   out[i] = t.sample(pa, xy[i], layer[i]);
   uniform<uint> n = make_uniform(layer[0] + 5u);
   uout[i] = n * 3u + i;
}
