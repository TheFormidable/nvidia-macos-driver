// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// More of Metal's math library: round, trunc, ceil, fract, fmod, copysign, clamp, mix, step, smoothstep, saturate,
// sign, pow, atan2, asin, acos, sinh, fma on half, and integer add_sat, absdiff, extract_bits, reverse_bits, hadd.
#include <metal_stdlib>
using namespace metal;

kernel void math_lib(device const float *a [[buffer(0)]], device const float *b [[buffer(1)]],
                     device const int *n [[buffer(2)]], device float *out [[buffer(3)]],
                     device uint *iout [[buffer(4)]], uint i [[thread_position_in_grid]])
{
   float x = a[i], y = b[i];
   int k = n[i], m = n[i ^ 1u];
   device float *o = out + i * 16;
   o[0] = round(x);
   o[1] = trunc(x);
   o[2] = ceil(x);
   o[3] = fract(x);
   o[4] = fmod(x, y);
   o[5] = copysign(y, x);
   o[6] = clamp(x, -1.0f, 2.0f);
   o[7] = mix(x, y, 0.25f);
   o[8] = step(y, x) + smoothstep(-1.0f, 1.0f, x);
   o[9] = saturate(x) + sign(y);
   o[10] = pow(fabs(x) + 0.5f, 1.5f);
   o[11] = atan2(x, y);
   o[12] = asin(clamp(x * 0.1f, -1.0f, 1.0f)) + acos(clamp(y * 0.1f, -1.0f, 1.0f));
   o[13] = sinh(x * 0.5f);
   o[14] = float(fma(half(x), half(0.5f), half(y)));
   o[15] = exp2(x) - log(fabs(y) + 1.0f);
   device uint *u = iout + i * 8;
   u[0] = uint(addsat(k, m));
   u[1] = uint(subsat(k, m));
   u[2] = absdiff(k, m);
   u[3] = extract_bits(uint(k), 3, 9);
   u[4] = uint(extract_bits(k, 5, 7));
   u[5] = reverse_bits(uint(k));
   u[6] = hadd(uint(k), uint(m)) ^ uint(rhadd(k, m));
   u[7] = insert_bits(uint(k), uint(m), 4, 12) + uint(mad24(k & 0xffff, m & 0xffff, 5));
}
