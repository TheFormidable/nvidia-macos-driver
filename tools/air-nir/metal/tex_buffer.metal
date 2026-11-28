// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// texture_buffer: reads through a uniform texel buffer (r32f) and writes through a storage texel buffer (r32ui).
#include <metal_stdlib>
using namespace metal;

kernel void tex_buffer(texture_buffer<float, access::read> in [[texture(0)]], texture_buffer<uint, access::write> out [[texture(1)]],
                       device float4 *o [[buffer(0)]], uint i [[thread_position_in_grid]])
{
   float4 v = in.read(i);
   o[i] = v;
   out.write(uint4(uint(v.x * 4.0f) + i), i);
}
