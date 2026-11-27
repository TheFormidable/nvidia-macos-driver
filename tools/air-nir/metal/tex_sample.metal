// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Sampling a 2D texture with explicit levels: an argument sampler nearest/clamp, one linear/repeat, a constexpr
// linear/clamp sampler, and a gather.
#include <metal_stdlib>
using namespace metal;

constexpr sampler cs_lin(coord::normalized, address::clamp_to_edge, filter::linear);

kernel void tex_sample(texture2d<float, access::sample> t [[texture(0)]], sampler near [[sampler(0)]],
                       sampler lin [[sampler(1)]], device float4 *out [[buffer(0)]],
                       device const float2 *uv [[buffer(1)]], uint i [[thread_position_in_grid]])
{
   float2 c = uv[i];
   out[i * 5u] = t.sample(near, c, level(0));
   out[i * 5u + 1u] = t.sample(lin, c, level(0));
   out[i * 5u + 2u] = t.sample(lin, c, level(1));
   out[i * 5u + 3u] = t.sample(cs_lin, c, level(0));
   out[i * 5u + 4u] = t.gather(near, c);
}
