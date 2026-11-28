// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// [[threadgroup(0)]] and [[threadgroup(1)]] arguments: reverse each threadgroup's slice, and a running count.
#include <metal_stdlib>
using namespace metal;

kernel void tg_arg(device const uint *in [[buffer(0)]], device uint *out [[buffer(1)]],
                   threadgroup uint *buf [[threadgroup(0)]], threadgroup ushort *tag [[threadgroup(1)]],
                   uint gid [[thread_position_in_grid]], uint lid [[thread_position_in_threadgroup]],
                   uint n [[threads_per_threadgroup]])
{
   buf[lid] = in[gid];
   tag[lid] = ushort(lid * 3u);
   threadgroup_barrier(mem_flags::mem_threadgroup);
   out[gid] = buf[n - 1u - lid] + uint(tag[(lid + 1u) % n]);
}
