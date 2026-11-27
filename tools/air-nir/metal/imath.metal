// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Integer ops: signed and unsigned div and rem, shifts, min/max, popcount, clz, ctz, mul_hi, rotate.
#include <metal_stdlib>
using namespace metal;

kernel void imath(device const int *a [[buffer(0)]], device const int *b [[buffer(1)]],
                  device uint *out [[buffer(2)]], uint i [[thread_position_in_grid]])
{
   int x = a[i], y = b[i];
   uint ux = uint(x), uy = uint(y);
   uint s = uy & 31u;
   device uint *o = out + i * 16;
   o[0] = uint(x / y);
   o[1] = uint(x % y);
   o[2] = ux / uy;
   o[3] = ux % uy;
   o[4] = ux << s;
   o[5] = ux >> s;
   o[6] = uint(x >> int(s));
   o[7] = uint(min(x, y));
   o[8] = uint(max(x, y));
   o[9] = min(ux, uy);
   o[10] = max(ux, uy);
   o[11] = popcount(ux);
   o[12] = clz(ux);
   o[13] = ctz(uy);
   o[14] = mulhi(ux, uy);
   o[15] = rotate(ux, s) ^ uint(x * y) ^ uint(abs(x - y));
}
