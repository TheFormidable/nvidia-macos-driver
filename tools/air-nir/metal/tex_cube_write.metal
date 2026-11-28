// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Writing texturecube faces (round 11: image-based-lighting prefilters write cube maps from kernels), as the translator
// writes a cube it never samples by direction: a 2D-array image at (x, y, face) (discovery.rs, fetch_coord.rs)
#include <metal_stdlib>
using namespace metal;
kernel void tex_cube_write(texturecube<half, access::write> c [[texture(0)]], texturecube<uint, access::write> u [[texture(1)]],
                           device uint *sz [[buffer(0)]], uint3 g [[thread_position_in_grid]])
{
   c.write(half4(half(g.x), half(g.y), half(g.z), 0.5h), uint2(g.xy), g.z);
   u.write(uint4(g.x + 8u * g.y + 64u * g.z), uint2(g.xy), g.z);
   if (g.x == 0 && g.y == 0 && g.z == 0) {
      sz[0] = c.get_width();
      sz[1] = c.get_height();
      sz[2] = u.get_width();
   }
}
