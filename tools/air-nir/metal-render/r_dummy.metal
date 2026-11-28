// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// The plugin's dummy bindings (round 11). rdw_k writes through every kind of binding nothing is bound at (a device
// buffer, read through its address, and a written texture_buffer); it runs before each draw of these tests. rd_fs is
// r_oob's fragment: a palette indexed with values read from memory (the translator keeps it a bounds-checked
// storage-buffer descriptor), drawn with the palette bound at offset 4, where Metal reads it. rdr_fs reads a buffer of
// that kind and a texture_buffer, both with nothing bound: 0 (a null descriptor, the zero dummy), whatever rdw_k wrote.
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; };
vertex VOut rd_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]]) { VOut o; o.pos = float4(p[vid], 0, 1); return o; }
fragment float4 rd_fs(VOut in [[stage_in]], constant float4 *pal [[buffer(0)]], device const uint *idx [[buffer(3)]])
{
   const uint2 q = uint2(in.pos.xy);
   return pal[idx[q.y * 64 + q.x]];
}
kernel void rdw_k(device float4 *out [[buffer(0)]], texture_buffer<float, access::write> wt [[texture(0)]],
                  uint i [[thread_position_in_grid]])
{
   out[i] = float4(1.0f, 2.0f, 3.0f, 4.0f);
   wt.write(float4(1.0f), i);
}
fragment float4 rdr_fs(VOut in [[stage_in]], device const uint *idx [[buffer(0)]], device const float4 *none [[buffer(1)]],
                       texture_buffer<float> tb [[texture(0)]])
{
   const uint2 q = uint2(in.pos.xy);
   const uint k = idx[q.y * 64 + q.x];
   return none[k] + tb.read(k);
}
