// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Pointers: a select between two device buffers, a pointer walked through a loop (a pointer phi), a pointer into a
// struct, and 64-bit integer math on the index.
#include <metal_stdlib>
using namespace metal;

struct Rec { uint a; float b; uint c[4]; };

kernel void ptr_ops(device const uint *x [[buffer(0)]], device const uint *y [[buffer(1)]],
                    device const Rec *recs [[buffer(2)]], device uint *out [[buffer(3)]],
                    uint i [[thread_position_in_grid]])
{
   device const uint *p = (i & 1u) ? x : y;
   uint s = 0;
   for (device const uint *q = p + i * 4u, *e = q + 4; q != e; q++)
      s += *q;
   device const Rec *r = recs + (i % 7u);
   device const uint *c = &r->c[i & 3u];
   ulong big = ulong(x[i * 4u]) * 0x100000001ul + ulong(i);
   out[i * 4] = s;
   out[i * 4 + 1] = *c + r->a;
   out[i * 4 + 2] = uint(big >> 13) ^ uint(big % 1000003ul);
   out[i * 4 + 3] = uint(r->b * 4.0f);
}
