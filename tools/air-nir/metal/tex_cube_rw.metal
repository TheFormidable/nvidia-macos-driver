// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Cube textures read by texel (round 12), as the translator lowers a cube it never samples by direction: a 2D-array
// image read at (x, y, face) and the level (discovery.rs, fetch_coord.rs build_fetch_coord): a sampled texturecube
// at levels 0 and 1 (tex_cube_rd), a read_write one read and written back (tex_cube_rwk), and a written
// texturecube_array at (x, y, slice * 6 + face) (tex_cube_arrw)
#include <metal_stdlib>
using namespace metal;
kernel void tex_cube_rd(texturecube<float> s [[texture(0)]], device float4 *out [[buffer(0)]], device uint *o2 [[buffer(1)]],
                        uint3 g [[thread_position_in_grid]])
{
   const uint i = (g.z * 8u + g.y) * 8u + g.x;
   out[2u * i] = s.read(uint2(g.xy), g.z, 0);
   out[2u * i + 1u] = s.read(uint2(g.xy) / 2u, g.z, 1);
   if (i == 0u) {
      o2[0] = s.get_width(0);
      o2[1] = s.get_width(1);
   }
}
kernel void tex_cube_rwk(texturecube<uint, access::read_write> rw [[texture(1)]], device uint *o2 [[buffer(1)]],
                         uint3 g [[thread_position_in_grid]])
{
   const uint i = (g.z * 8u + g.y) * 8u + g.x;
   const uint4 v = rw.read(uint2(g.xy), g.z);
   o2[i] = v.x;
   rw.write(v + uint4(1000u), uint2(g.xy), g.z);
   if (i == 0u)
      o2[384] = rw.get_width();
}
kernel void tex_cube_arrw(texturecube_array<half, access::write> wa [[texture(2)]], device uint *o2 [[buffer(1)]],
                          uint3 g [[thread_position_in_grid]])
{
   for (uint slice = 0; slice < 2u; slice++)
      wa.write(half4(half(g.x), half(g.y), half(g.z), half(slice)), uint2(g.xy), g.z, slice);
   if (g.x == 0u && g.y == 0u && g.z == 0u) {
      o2[0] = wa.get_width();
      o2[1] = wa.get_array_size();
   }
}
