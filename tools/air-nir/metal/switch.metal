// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A switch with fall-through and default, and one the compiler may turn into a lookup table.
#include <metal_stdlib>
using namespace metal;

kernel void switch_case(device const uint *in [[buffer(0)]], device uint *out [[buffer(1)]],
                        uint i [[thread_position_in_grid]])
{
   uint x = in[i], r;
   switch (x % 9u) {
   case 0: r = x * 3u; break;
   case 1: r = x + 7u; break;
   case 2: x ^= 0x55u;  // falls through
   case 3: r = x << 2; break;
   case 5: r = x / 3u; break;
   case 7: r = ~x; break;
   default: r = x - 1u; break;
   }
   uint t;
   switch (x & 7u) {
   case 0: t = 11u; break;
   case 1: t = 23u; break;
   case 2: t = 5u; break;
   case 3: t = 99u; break;
   case 4: t = 1000u; break;
   case 5: t = 7u; break;
   case 6: t = 64u; break;
   default: t = 3u; break;
   }
   out[i * 2] = r;
   out[i * 2 + 1] = t;
}
