// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// atomic<float> on device memory: fetch_add of exactly representable values, and atomic_uint fetch_add of a value
// computed per thread, into a few counters.
#include <metal_stdlib>
using namespace metal;

kernel void atomic_float(device const uint *in [[buffer(0)]], device atomic_float *sums [[buffer(1)]],
                         device atomic_uint *counts [[buffer(2)]], uint i [[thread_position_in_grid]])
{
   uint v = in[i];
   atomic_fetch_add_explicit(&sums[v & 3u], float(v & 255u) * 0.25f, memory_order_relaxed);
   atomic_fetch_add_explicit(&counts[v & 3u], v >> 20, memory_order_relaxed);
}
