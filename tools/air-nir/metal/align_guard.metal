// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// plugin_run's align-guard test: float4 accesses (each promises 16 bytes) through buffers bound at offsets that do not
// keep the promise
#include <metal_stdlib>
using namespace metal;
kernel void align_copy(device const float4 *in [[buffer(0)]], device float4 *out [[buffer(1)]], uint i [[thread_position_in_grid]])
{
   out[i] = in[i] * 2 + float4(1, 2, 3, 4);
}
