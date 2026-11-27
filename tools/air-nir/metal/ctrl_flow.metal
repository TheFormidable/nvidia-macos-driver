// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Control flow: nested loops with break and continue, a do-while, an early return from inside a loop, and a loop
// whose trip count depends on threadgroup-uniform and per-thread data.
#include <metal_stdlib>
using namespace metal;

kernel void ctrl_flow(device const uint *in [[buffer(0)]], device uint *out [[buffer(1)]],
                      uint i [[thread_position_in_grid]])
{
   uint x = in[i], acc = 0;
   for (uint a = 0; a < 8u; a++) {
      if (((x >> a) & 1u) == 0u)
         continue;
      for (uint b = 0; b < 8u; b++) {
         if (b > a)
            break;
         acc += a * 8u + b;
         if (acc > 300u)
            break;
      }
   }
   uint y = x, steps = 0;
   do {
      y = y * 1103515245u + 12345u;
      steps++;
   } while ((y >> 28) != 0u && steps < 40u);
   out[i * 3] = acc;
   out[i * 3 + 1] = steps;
   for (uint k = 0; k < 32u; k++) {
      if (((x * 2654435761u) >> k) & 1u && k > (x & 15u)) {
         out[i * 3 + 2] = k;
         return;
      }
   }
   out[i * 3 + 2] = 99u;
}
