// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A texture whose index is a function-constant expression, and sincos.
#include <metal_stdlib>
using namespace metal;

constant int tex_base [[function_constant(0)]];

kernel void fc_texture(texture2d<float, access::read> t [[texture(tex_base + 1)]], device const float *in [[buffer(0)]],
                       device float4 *out [[buffer(1)]], uint2 gid [[thread_position_in_grid]])
{
   uint i = gid.y * 8u + gid.x;
   float c;
   float sn = sincos(in[i], c);
   out[i * 2u] = t.read(gid);
   out[i * 2u + 1u] = float4(sn, c, 0.0f, 1.0f);
}
