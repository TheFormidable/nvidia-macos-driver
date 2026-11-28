// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// The SIMD-group builtins: lane, SIMD-group index, width and count, next to the thread index.
#include <metal_stdlib>
using namespace metal;

kernel void simd_builtins(device uint *out [[buffer(0)]], uint gid [[thread_position_in_grid]],
                          uint lane [[thread_index_in_simdgroup]], uint sg [[simdgroup_index_in_threadgroup]],
                          uint width [[threads_per_simdgroup]], uint count [[simdgroups_per_threadgroup]],
                          uint lid [[thread_index_in_threadgroup]])
{
   out[gid * 4] = lane;
   out[gid * 4 + 1] = sg;
   out[gid * 4 + 2] = width | (count << 16);
   out[gid * 4 + 3] = lid;
}
