// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A loop with phis and an early exit (Collatz steps with a cap), and a nested loop with a carried pair.
#include <metal_stdlib>
using namespace metal;

kernel void loop_phi(device const uint *in [[buffer(0)]], device uint *out [[buffer(1)]],
                     uint i [[thread_position_in_grid]])
{
   uint x = in[i], steps = 0, peak = x;
   while (x != 1u) {
      x = (x & 1u) ? 3u * x + 1u : x >> 1;
      peak = max(peak, x);
      if (++steps >= 500u)
         break;
   }
   uint a = 1, b = 0;
   for (uint j = 0; j < (in[i] & 15u); j++) {
      uint t = a + b;   // a and b swap through the phis
      b = a;
      a = t;
      if (a > 1000u)
         break;
   }
   out[i * 3] = steps;
   out[i * 3 + 1] = peak;
   out[i * 3 + 2] = a * 65536u + b;
}
