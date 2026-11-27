// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A constant-address-space parameter struct with a float4x4 matrix, a float2 and counts, applied to float4 points.
#include <metal_stdlib>
using namespace metal;

struct Params {
   float4x4 m;
   float2 scale;
   uint count;
   int offset;
};

kernel void mat_const(constant Params &p [[buffer(0)]], device const float4 *in [[buffer(1)]],
                      device float4 *out [[buffer(2)]], uint i [[thread_position_in_grid]])
{
   float4 v = in[i];
   float4 r = p.m * v;
   r.xy *= p.scale;
   r.w += float(int(p.count) + p.offset);
   out[i] = (transpose(p.m) * r) * 0.0625f;
}
