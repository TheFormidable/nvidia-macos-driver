// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// a fragment reading buffers through the address table: a constant palette, a device index buffer, a constant count
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; };
vertex VOut rb_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]]) { VOut o; o.pos = float4(p[vid], 0, 1); return o; }
fragment float4 rb_fs(VOut in [[stage_in]], constant float4 *pal [[buffer(0)]], device const uint *idx [[buffer(3)]],
                      constant uint &n [[buffer(5)]])
{
   const uint2 q = uint2(in.pos.xy);
   return pal[idx[(q.y * 64 + q.x) % n] & 15];
}
