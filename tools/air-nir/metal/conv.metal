// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Conversions between uint, int, float and half, and narrow integer casts.
#include <metal_stdlib>
using namespace metal;

kernel void conv(device const float *f [[buffer(0)]], device const int *n [[buffer(1)]],
                 device const half *h [[buffer(2)]], device uint *out [[buffer(3)]],
                 device half *hout [[buffer(4)]], uint i [[thread_position_in_grid]])
{
   float x = f[i];
   int k = n[i];
   half y = h[i];
   device uint *o = out + i * 10;
   o[0] = uint(int(x));                 // float -> int (toward zero)
   o[1] = uint(fabs(x));                // float -> uint
   o[2] = as_type<uint>(float(k));      // int -> float
   o[3] = as_type<uint>(float(uint(k))); // uint -> float
   o[4] = as_type<uint>(float(half(x))); // float -> half -> float
   o[5] = uint(int(y));                 // half -> int
   o[6] = as_type<uint>(float(y) * 2.0f); // half -> float
   o[7] = uint(int(short(k)));          // int -> short, sign extended back
   o[8] = uint(uchar(k)) | (uint(ushort(k)) << 8); // uint -> uchar, ushort
   o[9] = uint(int(char(k >> 4)));       // int -> char, sign extended back
   hout[i * 2] = half(x) + y;            // half add
   hout[i * 2 + 1] = half(k & 255) * y;  // int -> half, half mul
}
