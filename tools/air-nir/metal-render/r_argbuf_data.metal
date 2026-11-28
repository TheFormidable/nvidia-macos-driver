// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// r_argbuf with the indices chosen at run time read from buffers: the translator keeps the argument buffer, which an
// index read from memory selects within, a bounds-checked storage-buffer descriptor, and the frontend refuses it (an
// argument buffer as a descriptor), so the pipeline takes the SPIR-V road. Argument buffers in a vertex and a fragment
// function, written by the CPU as Metal lays them out (tier 2: a texture or
// sampler field holds its gpuResourceID, a pointer field its GPU address). The vertex function reads its positions and a
// scale through one, an array of textures by an index chosen at run time, a texture sampled with a sampler of it, a
// nested argument buffer's texture and buffer, and an element of an array of argument buffers chosen at run time; the
// fragment function samples a texture with a sampler of one, reads an array of textures by an index that changes from
// pixel to pixel, samples a texture of the array with an element of an array of samplers chosen per pixel, a nested
// argument buffer with its own texture, sampler and buffer, a palette buffer, and an array of argument buffers indexed
// per pixel
#include <metal_stdlib>
using namespace metal;

struct VInner {
   texture2d<float> t [[id(0)]];
   device const float4 *data [[id(1)]];
};
struct VArgs {
   device const float2 *pos [[id(0)]];
   float scale [[id(1)]];
   array<texture2d<float>, 2> vts [[id(2)]];
   sampler vs [[id(4)]];
   constant VInner *inner [[id(5)]];
};
struct VItem {
   device const float4 *v [[id(0)]];
   texture2d<float> t [[id(1)]];
};
struct VOut { float4 pos [[position]]; float2 uv; float4 vc [[flat]]; };

vertex VOut rab_vs(uint vid [[vertex_id]], constant VArgs &va [[buffer(0)]], constant VItem *vitems [[buffer(1)]],
                   constant uint *sel [[buffer(2)]])
{
   VOut o;
   const float2 p = va.pos[vid] * va.scale;
   o.pos = float4(p, 0, 1);
   o.uv = float2(p.x * 0.5 + 0.5, 0.5 - p.y * 0.5);
   float4 c = va.vts[sel[0]].read(uint2(3, 5)) + va.vts[0].sample(va.vs, float2(0.3, 0.7), level(0));
   c += va.inner->t.read(uint2(2, 1)) + va.inner->data[sel[1]];
   c += vitems[sel[1]].v[0] + vitems[sel[1]].t.read(uint2(6, 6));
   o.vc = c;
   return o;
}

struct FInner {
   texture2d<float> t [[id(0)]];
   sampler s [[id(1)]];
   device const float4 *data [[id(2)]];
};
struct FArgs {
   texture2d<float> tex [[id(0)]];
   sampler smp [[id(1)]];
   array<texture2d<float>, 3> texs [[id(2)]];
   array<sampler, 2> smps [[id(5)]];
   constant FInner *inner [[id(7)]];
   device const float4 *pal [[id(8)]];
};
struct FItem {
   texture2d<float> t [[id(0)]];
   device const float4 *v [[id(1)]];
};

fragment float4 rab_fs(VOut in [[stage_in]], constant FArgs &fa [[buffer(0)]], constant FItem *items [[buffer(1)]],
                       constant uint *sel [[buffer(2)]])
{
   const uint2 px = uint2(in.pos.xy);
   const uint k = (px.x + px.y) & 1u;
   float4 c = fa.tex.sample(fa.smp, in.uv);
   c += fa.texs[sel[k]].read(px / 4) * 2.0;
   c += fa.texs[1].sample(fa.smps[k], in.uv * 1.5);
   c += fa.inner->t.sample(fa.inner->s, in.uv * 2.0 - 0.5) * 0.5 + fa.inner->data[px.x & 7u];
   c += fa.pal[px.y & 3u];
   const uint it = sel[2] ^ k;
   c += items[it].t.read(px / 4) + items[it].v[px.x & 3u];
   return c + in.vc;
}
