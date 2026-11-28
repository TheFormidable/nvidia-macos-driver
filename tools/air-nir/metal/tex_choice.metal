// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Run-time choices between texture handles and null texture_buffers (round 12): a select of two argument textures
// by data (tex_choice), a null texture against an argument (tex_choice_null), a null texture_buffer (tex_null_tbuf)
#include <metal_stdlib>
using namespace metal;
kernel void tex_choice(texture2d<float> a [[texture(0)]], texture2d<float> b [[texture(1)]],
                       device const uint *sel [[buffer(0)]], device float4 *out [[buffer(1)]], uint i [[thread_position_in_grid]])
{
   const uint2 c = uint2(i % 8, i / 8);
   texture2d<float> t = (sel[i] & 1u) ? a : b;
   out[i] = t.read(c);
}
kernel void tex_choice_null(texture2d<float> a [[texture(0)]], texture2d<float> b [[texture(1)]],
                            device const uint *sel [[buffer(0)]], device float4 *out [[buffer(1)]], uint i [[thread_position_in_grid]])
{
   const uint2 c = uint2(i % 8, i / 8);
   texture2d<float> n = (sel[i] & 2u) ? a : texture2d<float>();
   out[i * 2] = n.read(c);
   out[i * 2 + 1] = float4(is_null_texture(n) ? 1.0f : 0.0f, float(n.get_width()), 0.0f, 1.0f);
}
kernel void tex_null_tbuf(texture_buffer<float> tb [[texture(0)]], device float4 *out [[buffer(1)]], uint i [[thread_position_in_grid]])
{
   texture_buffer<float> nb = texture_buffer<float>();
   out[i * 2] = nb.read(i);
   out[i * 2 + 1] = float4(is_null_texture(nb) ? 1.0f : 0.0f, float(nb.get_width()), is_null_texture(tb) ? 1.0f : 0.0f, 1.0f);
}
