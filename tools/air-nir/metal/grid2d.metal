// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A 2D grid: uint2 positions in the grid and threadgroup, the threadgroup's position and the grid size.
#include <metal_stdlib>
using namespace metal;

kernel void grid2d(device uint *out [[buffer(0)]], uint2 gid [[thread_position_in_grid]],
                   uint2 lid [[thread_position_in_threadgroup]], uint2 tg [[threadgroup_position_in_grid]],
                   uint2 size [[threads_per_grid]], uint2 groups [[threadgroups_per_grid]],
                   uint2 tgsize [[threads_per_threadgroup]])
{
   uint at = (gid.y * size.x + gid.x) * 4u;
   out[at] = gid.x | (gid.y << 16);
   out[at + 1] = lid.x | (lid.y << 8) | (tg.x << 16) | (tg.y << 24);
   out[at + 2] = size.x | (size.y << 16);
   out[at + 3] = groups.x | (groups.y << 8) | (tgsize.x << 16) | (tgsize.y << 24);
}
