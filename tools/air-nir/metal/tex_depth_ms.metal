// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// depth2d_ms and depth2d_ms_array read by sample, with their sizes and sample counts (no array size: the translator
// has no array-size query of a depth texture, and tex_depth_asize must be refused) (round 11: an engine's depth
// resolve and MSAA depth fragments read them), as the translator reads them (write_read.rs lower_read_depth: the
// sample where a level would be). tdm_vs/tdm_fs draw the textures first (plugin_run.m tex_ms_depth): each sample's depth
// is base + (sample + 1) / 16, per sample through [[sample_id]].
#include <metal_stdlib>
using namespace metal;
kernel void tex_depth_ms(depth2d_ms<float> d [[texture(0)]], depth2d_ms_array<float> da [[texture(1)]],
                         device float4 *out [[buffer(0)]], uint2 g [[thread_position_in_grid]])
{
   const uint i = g.y * 8u + g.x;
   out[i * 3u] = float4(d.read(g, 0), d.read(g, 1), d.read(g, 2), d.read(g, 3));
   out[i * 3u + 1u] = float4(da.read(g, 0, i % 4u), da.read(g, 1, 3u - i % 4u), float(d.get_num_samples()),
                             float(da.get_num_samples()));
   out[i * 3u + 2u] = float4(float(d.get_width()), float(d.get_height()), float(da.get_height()), float(da.get_width()));
}
struct DOut { float d [[depth(any)]]; };
vertex float4 tdm_vs(uint v [[vertex_id]])
{
   const float2 p = float2(float((v << 1) & 2u), float(v & 2u));
   return float4(p * 2.0f - 1.0f, 0.5f, 1.0f);
}
fragment DOut tdm_fs(uint s [[sample_id]], constant float &base [[buffer(0)]])
{
   DOut o;
   o.d = base + float(s + 1u) * 0.0625f;
   return o;
}
kernel void tex_depth_asize(depth2d_array<float> da [[texture(0)]], device uint *out [[buffer(0)]])
{
   out[0] = da.get_array_size();
}
