// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Storage textures: writes to rgba8, r32f and rgba16f, and a read_write r32ui read then written.
#include <metal_stdlib>
using namespace metal;

kernel void tex_write(texture2d<float, access::write> w8 [[texture(0)]], texture2d<float, access::write> wf [[texture(1)]],
                      texture2d<uint, access::read_write> ru [[texture(2)]], texture2d<half, access::write> wh [[texture(3)]],
                      uint2 gid [[thread_position_in_grid]])
{
   float2 p = float2(gid) / 15.0f;
   w8.write(float4(p.x, p.y, 1.0f - p.x, 0.5f), gid);
   wf.write(float4(p.x * 100.0f + p.y), gid);
   uint v = ru.read(gid).x;
   ru.write(uint4(v * 3u + 1u), gid);
   wh.write(half4(half(p.x), half(p.y), half(-1.5f), half(2.0f)), gid);
}
