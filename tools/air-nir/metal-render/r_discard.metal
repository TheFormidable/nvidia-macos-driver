// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// discard_fragment on a checkerboard of 4x4 pixels, and [[position]] in the fragment stage
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; };
vertex VOut rd_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]]) { VOut o; o.pos = float4(p[vid], 0, 1); return o; }
fragment float4 rd_fs(VOut in [[stage_in]])
{
   const uint2 q = uint2(in.pos.xy);
   if (((q.x >> 2) ^ (q.y >> 2)) & 1)
      discard_fragment();
   return float4(in.pos.x / 64, in.pos.y / 64, 0.5, 1);
}
