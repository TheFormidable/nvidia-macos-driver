// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// madsat (air.mad_sat) on 32-bit integers (round 11: v9_pool uses it): a * b + c clamped to the type, signed and
// unsigned, as the translator's wide multiply lowers it (integer_simd.rs lower_wide_multiply), which takes scalars only
// (a vector it refuses: mad_sat_v must be refused by the frontend too)
#include <metal_stdlib>
using namespace metal;
kernel void mad_sat_k(device const int4 *in [[buffer(0)]], device int4 *s [[buffer(1)]], device uint4 *u [[buffer(2)]],
                      uint i [[thread_position_in_grid]])
{
   const int4 a = in[i * 3], b = in[i * 3 + 1], c = in[i * 3 + 2];
   s[i] = int4(madsat(a.x, b.x, c.x), madsat(a.y, b.y, c.y), madsat(a.z, b.z, c.z), madsat(a.w, b.y, c.z));
   u[i] = uint4(madsat(uint(a.x), uint(b.x), uint(c.x)), madsat(uint(a.y), uint(b.y), uint(c.y)),
                madsat(uint(a.z), uint(b.z), uint(c.z)), madsat(uint(a.w), uint(b.x), uint(c.y)));
}
kernel void mad_sat_v(device const int4 *in [[buffer(0)]], device int4 *s [[buffer(1)]], uint i [[thread_position_in_grid]])
{
   s[i] = madsat(in[i * 3], in[i * 3 + 1], in[i * 3 + 2]);
}
