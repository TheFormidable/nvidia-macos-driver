// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Sampling a cube texture by direction and a 2D-array texture by layer (nearest, level 0).
#include <metal_stdlib>
using namespace metal;

kernel void tex_cube(texturecube<float, access::sample> c [[texture(0)]], texture2d_array<float, access::sample> a [[texture(1)]],
                     sampler s [[sampler(0)]], device const float4 *dir [[buffer(0)]], device float4 *out [[buffer(1)]],
                     uint i [[thread_position_in_grid]])
{
   float4 d = dir[i];
   out[i * 2u] = c.sample(s, d.xyz, level(0));
   out[i * 2u + 1u] = a.sample(s, d.xy * 0.5f + 0.5f, uint(d.w) % 2u, level(0));
}
