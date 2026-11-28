// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// 2D-array and 3D textures: reads with a layer and a depth, and writes to a 3D and a 2D-array storage texture.
#include <metal_stdlib>
using namespace metal;

kernel void tex_array3d(texture2d_array<float, access::read> arr [[texture(0)]], texture3d<float, access::read> vol [[texture(1)]],
                        texture3d<float, access::write> vw [[texture(2)]], texture2d_array<uint, access::write> aw [[texture(3)]],
                        device float4 *out [[buffer(0)]], uint3 gid [[thread_position_in_grid]])
{
   uint i = (gid.z * 8u + gid.y) * 8u + gid.x;
   out[i * 2u] = arr.read(gid.xy, gid.z % 3u);
   out[i * 2u + 1u] = vol.read(gid);
   vw.write(float4(float(i) * 0.5f), gid);
   if (gid.z < 3u)
      aw.write(uint4(i * 7u), gid.xy, gid.z);
}
