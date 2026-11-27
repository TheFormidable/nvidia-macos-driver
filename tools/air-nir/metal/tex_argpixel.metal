// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// argument samplers bound with pixel coordinates (normalizedCoordinates NO): nearest clamp_to_edge, nearest
// clamp_to_zero and linear clamp_to_edge, as the dispatch binds them (the state reaches the frontend in the request)
#include <metal_stdlib>
using namespace metal;
kernel void tex_argpixel(texture2d<float> t [[texture(0)]], sampler sn [[sampler(0)]], sampler sz [[sampler(1)]],
                         sampler sl [[sampler(2)]], constant float2 *xy [[buffer(0)]], device float4 *out [[buffer(1)]],
                         uint i [[thread_position_in_grid]])
{
   const float2 c = xy[i];
   out[i * 3] = t.sample(sn, c);
   out[i * 3 + 1] = t.sample(sz, c);
   out[i * 3 + 2] = t.sample(sl, c);
}
