// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// depth2d: a sample, a compare sample through a constexpr compare sampler, and a read.
#include <metal_stdlib>
using namespace metal;

constexpr sampler cmp(coord::normalized, address::clamp_to_edge, filter::nearest, compare_func::less);

kernel void tex_depth(depth2d<float, access::sample> d [[texture(0)]], sampler s [[sampler(0)]],
                      device const float2 *uv [[buffer(0)]], device float *out [[buffer(1)]],
                      uint i [[thread_position_in_grid]])
{
   float2 c = uv[i];
   out[i * 3u] = d.sample(s, c, level(0));
   out[i * 3u + 1u] = d.sample_compare(cmp, c, 0.5f, level(0));
   out[i * 3u + 2u] = d.read(uint2(c * 8.0f));
}
