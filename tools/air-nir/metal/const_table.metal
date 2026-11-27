// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Program-scope constant tables indexed at run time: a float table, a uint table and a struct table.
#include <metal_stdlib>
using namespace metal;

struct Entry { uint key; float scale; short bias; };
constant float kF[16] = { 0.5f, 1.25f, -3.0f, 7.75f, 100.0f, -0.125f, 2.0f, 9.5f,
                          -11.0f, 13.25f, 0.0f, 1e-3f, 42.0f, -42.0f, 3.5f, 6.0f };
constant uint kU[13] = { 2u, 3u, 5u, 7u, 11u, 13u, 17u, 19u, 23u, 29u, 31u, 37u, 41u };
constant Entry kE[5] = { { 1u, 2.0f, -3 }, { 10u, 0.5f, 4 }, { 100u, -1.0f, 5 }, { 1000u, 4.0f, -6 }, { 7u, 8.0f, 7 } };

kernel void const_table(device const uint *in [[buffer(0)]], device float *out [[buffer(1)]],
                        uint i [[thread_position_in_grid]])
{
   uint x = in[i];
   Entry e = kE[x % 5u];
   out[i * 3] = kF[x & 15u] * 2.0f;
   out[i * 3 + 1] = float(kU[(x >> 4) % 13u]);
   out[i * 3 + 2] = float(e.key) * e.scale + float(e.bias);
}
