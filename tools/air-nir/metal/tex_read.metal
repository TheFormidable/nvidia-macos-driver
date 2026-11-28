// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Texel reads (access::read): rgba8 with a second mip level, r8, rgba16f into half, r32f and r32ui.
#include <metal_stdlib>
using namespace metal;

kernel void tex_read(texture2d<float, access::read> a [[texture(0)]], texture2d<float, access::read> r8 [[texture(1)]],
                     texture2d<half, access::read> h [[texture(2)]], texture2d<float, access::read> f [[texture(3)]],
                     texture2d<uint, access::read> u [[texture(4)]], device float4 *out [[buffer(0)]],
                     device uint4 *uout [[buffer(1)]], uint2 gid [[thread_position_in_grid]])
{
   uint i = gid.y * 16u + gid.x;
   out[i * 5u] = a.read(gid);
   out[i * 5u + 1u] = a.read(gid / 2u, 1);
   out[i * 5u + 2u] = r8.read(gid);
   out[i * 5u + 3u] = float4(h.read(gid));
   out[i * 5u + 4u] = f.read(gid);
   uout[i] = u.read(gid);
}
