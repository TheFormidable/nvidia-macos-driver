// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Buffers the translator keeps bounds-checked descriptors, in its two other forms (round 11). rkm_fs: an array of
// structs of more than one scalar width, which the translator types member by member: its 64-bit member read and
// written as one value (0, and dropped, when it straddles the end). rkg_fs: a buffer stored to as floats, uints and
// float2s and read as half-words, which the translator makes a word array (its raw form) and whose stores it guards
// against byte offsets past 4 GiB.
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; };
struct M { float f; uint u; ulong l; float2 v; };
vertex VOut rk2_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]]) { VOut o; o.pos = float4(p[vid], 0, 1); return o; }
fragment float4 rkm_fs(VOut in [[stage_in]], device const uint *idx [[buffer(0)]], device const M *m [[buffer(1)]],
                       device M *o [[buffer(2)]])
{
   const uint p = uint(in.pos.x) + uint(in.pos.y) * 64;
   const uint k = idx[p];
   const M x = m[k];
   o[k].l = x.l ^ 0x0123456789abcdefUL;
   o[k].f = x.f * 2.0f;
   return float4(x.f, float(x.u), float(uint(x.l & 0xffffffu)), float(uint(x.l >> 40)) + x.v.y);
}
fragment float4 rkg_fs(VOut in [[stage_in]], device const uint *idx [[buffer(0)]], device float *o [[buffer(1)]])
{
   const uint p = uint(in.pos.x) + uint(in.pos.y) * 64;
   const uint k = idx[p];
   o[k] = 1.5f;
   device uint *ou = (device uint *)o;
   ou[k + 600] = 7u;
   device float2 *o2 = (device float2 *)o;
   o2[k + 1000] = float2(2.5f, 3.5f);
   /* a half-word read where nothing is written (words 1200 to 1599): with it the translator types the buffer as words */
   const ushort h = ((device const ushort *)o)[2u * (k % 400u) + 2401u];
   return float4(float(k & 255u), float(k >> 24), float(h), 1);
}
