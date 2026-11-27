// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Reading a multisampled texture by sample, with its size and sample count
#include <metal_stdlib>
using namespace metal;

kernel void tex_ms(texture2d_ms<float, access::read> t [[texture(0)]], device float4 *out [[buffer(0)]],
                   uint2 g [[thread_position_in_grid]])
{
   const uint n = t.get_num_samples();
   float4 s = float4(0);
   for (uint k = 0; k < n; k++)
      s += t.read(g, k);
   out[(g.y * 8u + g.x) * 2u] = s / float(n);
   out[(g.y * 8u + g.x) * 2u + 1u] = float4(float(t.get_width()), float(t.get_height()), float(n), t.read(g, n - 1u).y);
}
