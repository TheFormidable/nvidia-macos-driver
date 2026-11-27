// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Vectors: float4 and int4 arithmetic, swizzles, dot, cross, normalize, vector compare and select, any/all,
// uchar4 and half2 reinterpretation.
#include <metal_stdlib>
using namespace metal;

kernel void vec_ops(device const float4 *a [[buffer(0)]], device const float4 *b [[buffer(1)]],
                    device const uint *u [[buffer(2)]], device float4 *out [[buffer(3)]],
                    device uint4 *iout [[buffer(4)]], uint i [[thread_position_in_grid]])
{
   float4 x = a[i], y = b[i];
   float4 s = x * y + x.wzyx;
   float3 c = cross(x.xyz, y.xyz);
   float d = dot(x, y);
   float3 n = normalize(y.xyz + float3(0.5f));
   bool4 gt = x > y;
   float4 m = select(x, y, gt);
   out[i * 4] = s;
   out[i * 4 + 1] = float4(c, d);
   out[i * 4 + 2] = float4(n, length(x));
   out[i * 4 + 3] = m + float4(float(any(gt)), float(all(gt)), x.yy);
   uint w = u[i];
   uchar4 bytes = as_type<uchar4>(w);
   int4 iv = int4(bytes) - int4(128) + int4(x);
   half2 h = as_type<half2>(w & 0x3bff3bffu);
   iout[i] = uint4(iv * int4(3, -1, 7, 2)) ^ uint4(bytes.wzyx) + uint4(uint(float(h.x) * 1024.0f), uint(float(h.y) * 1024.0f), w >> 24, w & 0xffu);
}
