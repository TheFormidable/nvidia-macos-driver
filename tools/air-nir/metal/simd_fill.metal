// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// simd_shuffle_and_fill_down/up with a cluster width (modulo), the quad-group builtins and the SIMD-group width as a
// call (round 11: QuartzCore's tile blur kernels use them), as the translator lowers them (air_calls/shuffle.rs)
#include <metal_stdlib>
using namespace metal;
kernel void simd_fill(device const float4 *in [[buffer(0)]], device const ushort2 *dm [[buffer(1)]], device float4 *out [[buffer(2)]],
                      device uint4 *lanes [[buffer(3)]], uint i [[thread_position_in_grid]],
                      ushort q [[thread_index_in_quadgroup]], ushort qg [[quadgroup_index_in_threadgroup]])
{
   const float4 x = in[i];
   const float4 f = -x - 1000.0f;
   const half4 h = half4(x);
   for (uint k = 0; k < 6; k++) {
      const ushort2 d = dm[k];
      out[i * 18 + k * 3 + 0] = simd_shuffle_and_fill_down(x, f, d.x, d.y);
      out[i * 18 + k * 3 + 1] = simd_shuffle_and_fill_up(x, f, d.x, d.y);
      out[i * 18 + k * 3 + 2] = float4(simd_shuffle_and_fill_down(h, half4(f), d.x, d.y));
   }
   out[i * 18 + 18 * 64] = simd_shuffle_and_fill_down(x, f, ushort(i % 5));   /* the SIMD-group's width as the modulo */
   out[i * 18 + 18 * 64 + 1] = simd_shuffle_and_fill_up(x, f, ushort(i % 7));
   lanes[i] = uint4(q, qg, 4u, 0u);
}
