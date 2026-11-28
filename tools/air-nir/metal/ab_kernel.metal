// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Argument buffers in a kernel, written by the CPU as Metal lays them out (tier 2: a texture or sampler field holds its
// gpuResourceID, a pointer field its GPU address): a texture sampled with a sampler, a device buffer read and written, an
// inline constant, an array of textures read by a known index and by one chosen at run time, an array of samplers, a
// nested argument buffer (a pointer to another one) with its own buffer and texture, a written texture, and an array of
// argument buffers indexed at run time
#include <metal_stdlib>
using namespace metal;

struct Inner {
   device const float *data [[id(0)]];
   texture2d<float> t [[id(1)]];
   uint bias [[id(2)]];
};
struct Args {
   texture2d<float> tex [[id(0)]];
   sampler smp [[id(1)]];
   device float *io [[id(2)]];
   float scale [[id(3)]];
   array<texture2d<float>, 3> texs [[id(4)]];
   array<sampler, 2> smps [[id(7)]];
   constant Inner *inner [[id(9)]];
   texture2d<float, access::write> wtex [[id(10)]];
};
struct Item {
   texture2d<float> t [[id(0)]];
   device const float4 *v [[id(1)]];
};

kernel void ab_kernel(constant Args &a [[buffer(0)]], device float4 *out [[buffer(1)]], constant uint *sel [[buffer(2)]],
                      constant Item *items [[buffer(3)]], uint2 gid [[thread_position_in_grid]])
{
   const uint i = gid.y * 8u + gid.x;
   const float2 uv = (float2(gid) + 0.5f) / 8.0f;
   out[i * 8u] = a.tex.sample(a.smp, (float2(gid) + 0.75f) / 8.0f);
   out[i * 8u + 1u] = float4(a.io[i] * a.scale, a.scale, 0.0f, 1.0f);
   a.io[64u + i] = a.io[i] + 1.0f;
   out[i * 8u + 2u] = a.texs[1].read(gid);
   out[i * 8u + 3u] = a.texs[sel[i & 1u]].read(gid);
   out[i * 8u + 4u] = a.texs[0].sample(a.smps[i & 1u], uv * 1.75f) + a.texs[2].sample(a.smps[1], uv * 1.25f);
   out[i * 8u + 5u] = float4(a.inner->data[i], float(a.inner->bias), 0.0f, 0.0f) + a.inner->t.read(uint2(gid.y, gid.x));
   const constant Item &it = items[(i + sel[2]) & 3u];
   out[i * 8u + 6u] = it.t.read(gid) + it.v[i & 7u];
   out[i * 8u + 7u] = float4(float(i), 0.0f, 0.0f, 0.0f);
   a.wtex.write(a.texs[2].read(gid) * 2.0f, gid);
}
