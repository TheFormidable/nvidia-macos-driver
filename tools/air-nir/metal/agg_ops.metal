// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Structures and arrays held as values: a function the compiler must not inline returns a struct holding an array
// from two branches (insertvalue chains, and a phi of the struct once it is inlined), and its callers take the
// elements apart (extractvalue).
#include <metal_stdlib>
using namespace metal;

struct Pair {
   float v[4];
   uint tag;
   ushort small;
};

__attribute__((noinline)) static Pair
make_pair(device const float *in, uint i, uint mode)
{
   Pair p;
   if (mode & 1u) {
      for (uint k = 0; k < 4u; k++)
         p.v[k] = in[i * 4u + k] * 2.0f;
      p.tag = i;
      p.small = ushort(i * 3u);
   } else {
      for (uint k = 0; k < 4u; k++)
         p.v[k] = in[i * 4u + 3u - k] + 1.0f;
      p.tag = ~i;
      p.small = ushort(i + 7u);
   }
   return p;
}

kernel void agg_ops(device const float *in [[buffer(0)]], device float *out [[buffer(1)]],
                    device uint *tags [[buffer(2)]], uint i [[thread_position_in_grid]])
{
   Pair p = make_pair(in, i, i);
   Pair q = make_pair(in, i ^ 1u, i >> 1);
   for (uint k = 0; k < 4u; k++)
      out[i * 4u + k] = p.v[k] - q.v[(k + 1u) & 3u];
   tags[i * 2u] = p.tag ^ uint(q.small);
   tags[i * 2u + 1u] = q.tag + uint(p.small);
}
