// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A kernel reading a buffer at indices the data gives, some past its end and some whose byte offsets pass 4 GiB: with
// buffer robustness (NVMTL_COMPUTE_ROBUST=1) the translator's module keeps the buffer a bounds-checked storage-buffer
// descriptor, and the road reads it through that descriptor (plugin_run.m runs it only with robustness on)
#include <metal_stdlib>
using namespace metal;
kernel void oob_kernel(constant float4 *pal [[buffer(0)]], device const uint *idx [[buffer(1)]], device float4 *out [[buffer(2)]],
                       device const ushort *h16 [[buffer(3)]], uint i [[thread_position_in_grid]])
{
   const uint k = idx[i];
   out[i] = pal[k] + float4(float(h16[k & 0xff]), 0, 0, 0);
}
