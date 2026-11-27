// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Texture size queries: width and height at a level, mip count, array size, depth, a storage texture's size and a
// texture_buffer's width.
#include <metal_stdlib>
using namespace metal;

kernel void tex_query(texture2d<float, access::sample> t [[texture(0)]], texture2d_array<float, access::read> a [[texture(1)]],
                      texture3d<float, access::read> v [[texture(2)]], texture2d<float, access::write> w [[texture(3)]],
                      texture_buffer<float, access::read> b [[texture(4)]], device uint *out [[buffer(0)]])
{
   out[0] = t.get_width();
   out[1] = t.get_height(1);
   out[2] = t.get_num_mip_levels();
   out[3] = a.get_width();
   out[4] = a.get_height();
   out[5] = a.get_array_size();
   out[6] = v.get_width();
   out[7] = v.get_depth();
   out[8] = w.get_width();
   out[9] = w.get_height();
   out[10] = b.get_width();
   out[11] = t.get_width(2);
}
