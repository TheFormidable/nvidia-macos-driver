// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A 3D grid with ushort3 and uint3 positions.
#include <metal_stdlib>
using namespace metal;

kernel void grid3d(device uint *out [[buffer(0)]], uint3 gid [[thread_position_in_grid]],
                   ushort3 lid [[thread_position_in_threadgroup]], uint3 tg [[threadgroup_position_in_grid]],
                   uint3 size [[threads_per_grid]], ushort li [[thread_index_in_threadgroup]])
{
   uint at = ((gid.z * size.y + gid.y) * size.x + gid.x) * 2u;
   out[at] = gid.x | (gid.y << 8) | (gid.z << 16) | (uint(li) << 24);
   out[at + 1] = uint(lid.x) | (uint(lid.y) << 4) | (uint(lid.z) << 8) | (tg.x << 12) | (tg.y << 16) | (tg.z << 20);
}
