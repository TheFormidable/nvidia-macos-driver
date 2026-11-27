// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Threadgroup atomics: a per-threadgroup histogram, max and xor, then written out by the first threads.
#include <metal_stdlib>
using namespace metal;

kernel void tg_atomics(device const uint *in [[buffer(0)]], device uint *out [[buffer(1)]],
                       uint gid [[thread_position_in_grid]], uint lid [[thread_index_in_threadgroup]],
                       uint tg [[threadgroup_position_in_grid]])
{
   threadgroup atomic_uint hist[16];
   threadgroup atomic_uint mx, xr;
   if (lid < 16u)
      atomic_store_explicit(&hist[lid], 0u, memory_order_relaxed);
   if (lid == 0u) {
      atomic_store_explicit(&mx, 0u, memory_order_relaxed);
      atomic_store_explicit(&xr, 0u, memory_order_relaxed);
   }
   threadgroup_barrier(mem_flags::mem_threadgroup);
   uint v = in[gid];
   uint old = atomic_fetch_add_explicit(&hist[v & 15u], 1u, memory_order_relaxed);
   atomic_fetch_max_explicit(&mx, v, memory_order_relaxed);
   atomic_fetch_xor_explicit(&xr, v + old * 0u, memory_order_relaxed);
   threadgroup_barrier(mem_flags::mem_threadgroup);
   if (lid < 16u)
      out[tg * 18u + lid] = atomic_load_explicit(&hist[lid], memory_order_relaxed);
   if (lid == 16u)
      out[tg * 18u + 16u] = atomic_load_explicit(&mx, memory_order_relaxed);
   if (lid == 17u)
      out[tg * 18u + 17u] = atomic_load_explicit(&xr, memory_order_relaxed);
}
