// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// pack and unpack of unorm4x8, snorm4x8, unorm2x16, snorm2x16 and half2x16 (to float and to half), and ldexp
#include <metal_stdlib>
using namespace metal;

kernel void pack(device const uint *in [[buffer(0)]], device const float4 *f [[buffer(1)]], device const int4 *e [[buffer(2)]],
                 device float4 *out [[buffer(3)]], device uint4 *packed [[buffer(4)]], uint i [[thread_position_in_grid]])
{
   const uint w = in[i];
   const float4 v = f[i];
   out[i * 6u] = unpack_unorm4x8_to_float(w);
   out[i * 6u + 1u] = unpack_snorm4x8_to_float(w);
   out[i * 6u + 2u] = float4(unpack_unorm2x16_to_float(w), unpack_snorm2x16_to_float(w));
   out[i * 6u + 3u] = float4(float2(unpack_unorm2x16_to_half(w)), float2(as_type<half2>(w)));
   out[i * 6u + 4u] = ldexp(v, e[i]);
   out[i * 6u + 5u] = float4(ldexp(v.x, e[i].y), float3(unpack_unorm4x8_to_half(w).xyz));
   packed[i] = uint4(pack_float_to_unorm4x8(v * 0.25 + 0.5), pack_float_to_snorm4x8(v * 0.25),
                     pack_float_to_unorm2x16(v.xy * 0.25 + 0.5), pack_float_to_snorm2x16(v.zw * 0.25));
}
