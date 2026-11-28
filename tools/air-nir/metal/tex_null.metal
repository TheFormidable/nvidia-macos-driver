// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Null textures (round 11): the translator lowers texture2d<float>() to a placeholder sampled image at the first
// texture binding its module leaves free (it reads every sampled texture argument through the heap, and declares only
// read texture_buffer arguments and the first binding of read texture arrays there), so reading the null texture reads
// whatever is bound at that texture index; is_null_texture is true of it and false of an argument. tex_null: the
// placeholder at texture 0 (t's own index); tex_null_tb: a texture_buffer at 0, so the 2D placeholder at 1 and the 3D
// one (a second shape, after it) at 2.
#include <metal_stdlib>
using namespace metal;
kernel void tex_null(texture2d<float> t [[texture(0)]], device float4 *out [[buffer(0)]], uint i [[thread_position_in_grid]])
{
   texture2d<float> n = texture2d<float>();
   const uint2 c = uint2(i % 16, i / 16);
   out[i * 3 + 0] = float4(is_null_texture(n) ? 1.0f : 0.0f, is_null_texture(t) ? 1.0f : 0.0f,
                           float(n.get_width()), float(n.get_height()));
   out[i * 3 + 1] = n.read(c);
   out[i * 3 + 2] = t.read(c);
}
kernel void tex_null_tb(texture_buffer<float> tb [[texture(0)]], device float4 *out [[buffer(0)]], uint i [[thread_position_in_grid]])
{
   texture2d<float> n = texture2d<float>();
   texture3d<float> n3 = texture3d<float>();
   const uint2 c = uint2(i % 16, i / 16);
   out[i * 3 + 0] = float4(is_null_texture(n) ? 1.0f : 0.0f, is_null_texture(n3) ? 1.0f : 0.0f,
                           float(n.get_width()), float(n.get_height()));
   out[i * 3 + 1] = n.read(c);
   out[i * 3 + 2] = tb.read(i);
}
