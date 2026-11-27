// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Argument indices from function-constant expressions: a buffer at [[buffer(bidx + 1)]] and a texture at
// [[texture(tidx * 2)]] (Apple's compiler keeps each in a __metal_implicit_attr_int_expr global its static
// initializer fills)
#include <metal_stdlib>
using namespace metal;

constant int bidx [[function_constant(0)]];
constant int tidx [[function_constant(1)]];

kernel void fc_argindex(device float4 *out [[buffer(0)]], constant float *in [[buffer(bidx + 1)]],
                        texture2d<float, access::read> t [[texture(tidx * 2)]], uint2 gid [[thread_position_in_grid]])
{
   const uint i = gid.y * 8u + gid.x;
   out[i] = t.read(gid) + in[i];
}
