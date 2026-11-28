// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// depth2d and depth2d_array gathers (round 11: games' depth-of-field and bloom downsamples use them), with and without
// a constant offset, as the translator lowers air.gather_depth (gather.rs lower_gather_depth: component 0)
#include <metal_stdlib>
using namespace metal;
kernel void tex_gather_depth(depth2d<float> d [[texture(0)]], depth2d_array<float> da [[texture(1)]], sampler s [[sampler(0)]],
                             device const float2 *uv [[buffer(0)]], device float4 *out [[buffer(1)]],
                             uint i [[thread_position_in_grid]])
{
   const float2 c = uv[i];
   out[i * 4] = d.gather(s, c);
   out[i * 4 + 1] = d.gather(s, c, int2(1, -1));
   out[i * 4 + 2] = da.gather(s, c, i % 3);
   out[i * 4 + 3] = da.gather(s, c, 2, int2(-2, 1));
}
