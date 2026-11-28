// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A threadgroup-memory tree reduction with threadgroup_barrier: sums of uint (exact) and float.
#include <metal_stdlib>
using namespace metal;

kernel void tg_reduce(device const uint *in [[buffer(0)]], device uint *sums [[buffer(1)]],
                      device float *fsums [[buffer(2)]],
                      uint gid [[thread_position_in_grid]], uint lid [[thread_position_in_threadgroup]],
                      uint tg [[threadgroup_position_in_grid]])
{
   threadgroup uint s[256];
   threadgroup float f[256];
   s[lid] = in[gid];
   f[lid] = float(in[gid] & 1023u) * 0.5f;
   threadgroup_barrier(mem_flags::mem_threadgroup);
   for (uint w = 128; w > 0; w >>= 1) {
      if (lid < w) {
         s[lid] += s[lid + w];
         f[lid] += f[lid + w];
      }
      threadgroup_barrier(mem_flags::mem_threadgroup);
   }
   if (lid == 0) {
      sums[tg] = s[0];
      fsums[tg] = f[0];
   }
}
