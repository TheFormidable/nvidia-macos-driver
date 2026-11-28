// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Elementwise float math: add, mul, fma, sqrt, exp, sin, plus divide, rsqrt, log2, floor, fabs, min/max.
#include <metal_stdlib>
using namespace metal;

kernel void fmath(device const float *a [[buffer(0)]], device const float *b [[buffer(1)]],
                  device const float *c [[buffer(2)]], device float *out [[buffer(3)]],
                  uint i [[thread_position_in_grid]])
{
   float x = a[i], y = b[i], z = c[i];
   device float *o = out + i * 12;
   o[0] = x + y;
   o[1] = x * y;
   o[2] = fma(x, y, z);
   o[3] = sqrt(fabs(x));
   o[4] = exp(x);
   o[5] = sin(x);
   o[6] = x / y;
   o[7] = rsqrt(fabs(y) + 1.0f);
   o[8] = log2(fabs(z) + 1.0f);
   o[9] = floor(x * 3.0f);
   o[10] = max(x, y) - min(y, z);
   o[11] = cos(z) + tanh(y * 0.25f);
}
