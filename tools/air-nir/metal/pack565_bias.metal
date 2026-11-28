// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// pack_float_to_unorm565 and pack_half_to_unorm565 (air.pack.unorm.rgb565, round 11 after 3.0.28: a resolve fragment packs
// with it), as the translator packs it (dispatch_texture.rs lower_pack_rgb565: NClamp to [0, 1], times 31, 63, 31,
// RoundEven); and a sample with a bias in a kernel, which the translator takes at level 0 with the bias dropped
// (sample_depth.rs push_image_sample_min_lod: no derivatives outside a fragment function)
#include <metal_stdlib>
using namespace metal;
kernel void pack565(device const float4 *in [[buffer(0)]], device ushort2 *out [[buffer(1)]], uint i [[thread_position_in_grid]])
{
   const float3 v = in[i].xyz;
   out[i] = ushort2(pack_float_to_unorm565(v), pack_half_to_unorm565(half3(v)));
}
kernel void sample_bias_k(texture2d<float> t [[texture(0)]], sampler s [[sampler(0)]], device const float *bias [[buffer(0)]],
                          device float4 *out [[buffer(1)]], uint i [[thread_position_in_grid]])
{
   const float2 c = float2((i % 4) + 0.5f, (i / 4 % 4) + 0.5f) / 4.0f;
   out[i * 2] = t.sample(s, c, metal::bias(bias[i]));
   out[i * 2 + 1] = t.sample(s, c, metal::bias(1.5f), min_lod_clamp(1.0f));
}
