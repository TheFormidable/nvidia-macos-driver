// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Device atomics: fetch_add (with the returned slot used), a histogram, fetch_max/min/or/and,
// and a compare_exchange loop.
#include <metal_stdlib>
using namespace metal;

kernel void dev_atomics(device const uint *in [[buffer(0)]], device atomic_uint *counters [[buffer(1)]],
                        device uint *slots [[buffer(2)]], device atomic_uint *hist [[buffer(3)]],
                        device atomic_uint *cas [[buffer(4)]], uint gid [[thread_position_in_grid]])
{
   uint v = in[gid];
   uint slot = atomic_fetch_add_explicit(&counters[0], 1u, memory_order_relaxed);
   slots[slot] = gid;
   atomic_fetch_add_explicit(&hist[v & 15u], 1u, memory_order_relaxed);
   atomic_fetch_max_explicit(&counters[1], v, memory_order_relaxed);
   atomic_fetch_min_explicit(&counters[2], v, memory_order_relaxed);
   atomic_fetch_or_explicit(&counters[3], 1u << (v & 31u), memory_order_relaxed);
   atomic_fetch_and_explicit(&counters[4], ~(1u << ((v >> 5) & 31u)), memory_order_relaxed);
   atomic_fetch_sub_explicit(&counters[5], v & 7u, memory_order_relaxed);
   // a compare-exchange loop: cas[0] += (v & 255) for odd v only
   if (v & 1u) {
      uint expected = atomic_load_explicit(&cas[0], memory_order_relaxed);
      while (!atomic_compare_exchange_weak_explicit(&cas[0], &expected, expected + (v & 255u),
                                                    memory_order_relaxed, memory_order_relaxed)) {
      }
   }
   atomic_exchange_explicit(&cas[1], 7u, memory_order_relaxed);
}
