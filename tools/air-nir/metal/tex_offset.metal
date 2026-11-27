// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Texel offsets: a sample with an offset read from a buffer (known only at run time), one with a constant offset,
// and a gather with a constant offset.
#include <metal_stdlib>
using namespace metal;

kernel void tex_offset(texture2d<float, access::sample> t [[texture(0)]], sampler s [[sampler(0)]],
                       device const float2 *uv [[buffer(0)]], device const int2 *off [[buffer(1)]],
                       device float4 *out [[buffer(2)]], uint i [[thread_position_in_grid]])
{
   float2 c = uv[i];
   out[i * 3u] = t.sample(s, c, level(0), off[i]);
   out[i * 3u + 1u] = t.sample(s, c, level(0), int2(2, -3));
   out[i * 3u + 2u] = t.gather(s, c, int2(-1, 2));
}
