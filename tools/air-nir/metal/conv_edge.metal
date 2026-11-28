// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Float-to-integer conversions at the edges Metal defines: NaN gives 0, a value out of range clamps to the type's
// limit (fast math on, as every kernel here and QuartzCore's shaders are compiled)
#include <metal_stdlib>
using namespace metal;

kernel void conv_edge(device const float *f [[buffer(0)]], device uint *out [[buffer(1)]], uint i [[thread_position_in_grid]])
{
   const float x = f[i];
   device uint *o = out + i * 6;
   o[0] = uint(int(x));            // float -> int
   o[1] = uint(x);                 // float -> uint
   o[2] = uint(int(short(int(x)))); // float -> int -> short
   o[3] = uint(int(x * 2.0f));     // a product (inf * 2, NaN * 2)
   o[4] = isnan(x) ? 1u : 0u;      // the tests a NaN or an infinity must pass under fast math too
   o[5] = (isinf(x) ? 2u : 0u) | (isfinite(x) ? 4u : 0u);
}
