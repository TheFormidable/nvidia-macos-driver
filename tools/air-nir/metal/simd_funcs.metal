// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// SIMD-group functions: sum, max, prefix sums, shuffles, broadcast, ballot, all/any.
#include <metal_stdlib>
using namespace metal;

kernel void simd_funcs(device const uint *in [[buffer(0)]], device const float *fin [[buffer(1)]],
                       device uint *out [[buffer(2)]], device float *fout [[buffer(3)]],
                       uint i [[thread_position_in_grid]], uint lane [[thread_index_in_simdgroup]])
{
   uint v = in[i];
   float f = fin[i];
   out[i * 8] = simd_sum(v);
   out[i * 8 + 1] = simd_max(v);
   out[i * 8 + 2] = simd_prefix_exclusive_sum(v);
   out[i * 8 + 3] = simd_shuffle(v, (lane * 5u + 3u) & 31u);
   out[i * 8 + 4] = simd_broadcast(v, 7);
   out[i * 8 + 5] = simd_shuffle_down(v, 3u);
   out[i * 8 + 6] = uint(uint64_t(simd_ballot((v & 3u) == 0u)));
   out[i * 8 + 7] = uint(simd_all(v != 0u)) | (uint(simd_any(v == 5u)) << 1) | (simd_min(v) & ~3u);
   fout[i * 2] = simd_sum(f);
   fout[i * 2 + 1] = simd_prefix_inclusive_sum(f);
}
