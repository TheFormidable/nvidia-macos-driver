// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Thread-private arrays indexed at run time (kept in thread memory): an insertion sort of 12 values and a histogram.
#include <metal_stdlib>
using namespace metal;

kernel void thread_array(device const uint *in [[buffer(0)]], device uint *out [[buffer(1)]],
                         uint i [[thread_position_in_grid]])
{
   uint v[12];
   uint h[8] = { 0 };
   for (uint k = 0; k < 12u; k++) {
      v[k] = in[i * 12u + k];
      h[v[k] & 7u]++;
   }
   for (uint k = 1; k < 12u; k++) {
      uint x = v[k];
      int j = int(k) - 1;
      while (j >= 0 && v[j] > x) {
         v[j + 1] = v[j];
         j--;
      }
      v[j + 1] = x;
   }
   for (uint k = 0; k < 12u; k++)
      out[i * 20u + k] = v[k];
   for (uint k = 0; k < 8u; k++)
      out[i * 20u + 12u + k] = h[k];
}
