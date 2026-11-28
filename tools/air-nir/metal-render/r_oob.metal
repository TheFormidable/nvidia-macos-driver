// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// a fragment reading a buffer at an index it read from another buffer, some past the end: the SPIR-V road keeps such a
// buffer a bounds-checked descriptor (a read past its end gives 0); the road must refuse it
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; };
vertex VOut ro_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]]) { VOut o; o.pos = float4(p[vid], 0, 1); return o; }
fragment float4 ro_fs(VOut in [[stage_in]], constant float4 *pal [[buffer(0)]], device const uint *idx [[buffer(3)]])
{
   const uint2 q = uint2(in.pos.xy);
   return pal[idx[q.y * 64 + q.x]];
}
