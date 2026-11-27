// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A 16x16 tile transpose through a padded 2D threadgroup array, and a threadgroup array of float4 indexed at run time.
#include <metal_stdlib>
using namespace metal;

kernel void tg_transpose(device const float *in [[buffer(0)]], device float *out [[buffer(1)]],
                         device float4 *vout [[buffer(2)]], constant uint2 &size [[buffer(3)]],
                         uint2 gid [[thread_position_in_grid]], uint2 lid [[thread_position_in_threadgroup]],
                         uint2 tg [[threadgroup_position_in_grid]])
{
   threadgroup float tile[16][17];
   threadgroup float4 v[64];
   tile[lid.y][lid.x] = in[gid.y * size.x + gid.x];
   uint l = lid.y * 16u + lid.x;
   if (l < 64u)
      v[l] = float4(float(l), float(tg.x), float(tg.y), 1.0f);
   threadgroup_barrier(mem_flags::mem_threadgroup);
   uint2 o = uint2(tg.y * 16u + lid.x, tg.x * 16u + lid.y);
   out[o.y * size.y + o.x] = tile[lid.x][lid.y];
   if (l < 64u)
      vout[(tg.y * (size.x / 16u) + tg.x) * 64u + l] = v[(l * 7u) & 63u] * 2.0f;
}
