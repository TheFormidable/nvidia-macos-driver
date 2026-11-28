// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Argument buffers filled through MTLArgumentEncoder (setTexture:, setSamplerState:, setBuffer:, constantDataAtIndex:,
// newArgumentEncoderForBufferAtIndex: for the nested one): a sampled texture and its sampler, a buffer, inline
// constants, an array of written textures one chosen at run time, a read_write texture, a nested argument buffer with a
// buffer and a written texture, a texture_buffer read and one written, and an array of argument buffers each with a
// written texture, one chosen at run time
#include <metal_stdlib>
using namespace metal;

struct Inner2 {
   device float *out [[id(0)]];
   texture2d<float, access::write> wt [[id(1)]];
};
struct EArgs {
   texture2d<float> tex [[id(0)]];
   sampler smp [[id(1)]];
   device const float4 *src [[id(2)]];
   uint4 consts [[id(3)]];
   array<texture2d<float, access::write>, 2> wts [[id(4)]];
   texture2d<float, access::read_write> rw [[id(6)]];
   constant Inner2 *inner [[id(7)]];
   texture_buffer<float> tb [[id(8)]];
   texture_buffer<float, access::write> wtb [[id(9)]];
};
struct WItem {
   texture2d<float, access::write> t [[id(0)]];
};

kernel void ab_encoder(constant EArgs &a [[buffer(0)]], device float4 *out [[buffer(1)]], constant uint *sel [[buffer(2)]],
                       constant WItem *witems [[buffer(3)]], uint2 gid [[thread_position_in_grid]])
{
   const uint i = gid.y * 8u + gid.x;
   out[i * 3u] = a.tex.sample(a.smp, (float2(gid) + 0.5f) / 8.0f) + a.src[i] + float4(a.consts);
   a.wts[sel[i & 1u]].write(float4(float(i), 1.0f, 2.0f, 3.0f), gid);
   const float4 r = a.rw.read(gid);
   a.rw.write(r * 2.0f + 0.25f, gid);
   a.inner->wt.write(float4(0.5f, float(i) * 0.25f, 0.0f, 1.0f), gid);
   a.inner->out[i] = float(i) + 0.5f;
   out[i * 3u + 1u] = a.tb.read(i);
   a.wtb.write(float4(float(i) * 2.0f, 0.0f, 0.0f, 0.0f), i);
   witems[(i + sel[2]) & 1u].t.write(float4(float(i) * 0.5f, 0.0f, 0.0f, 1.0f), gid);
   out[i * 3u + 2u] = float4(float(sel[i & 1u]), 0.0f, 0.0f, 0.0f);
}
