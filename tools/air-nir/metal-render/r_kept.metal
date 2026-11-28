// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// stores and 64-bit values in buffers the translator keeps bounds-checked descriptors (round 10): every buffer here is
// indexed with a value read from memory. A ulong palette read past its end, its last element straddling the end (one
// 64-bit value: 0 entirely); a float buffer and a ulong buffer written at those indices, past their ends too (dropped).
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; };
vertex VOut rk_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]]) { VOut o; o.pos = float4(p[vid], 0, 1); return o; }
fragment float4 rk_fs(VOut in [[stage_in]], device const uint *idx [[buffer(0)]], device const ulong *pal64 [[buffer(1)]],
                      device float *outf [[buffer(2)]], device ulong *out64 [[buffer(3)]])
{
   const uint p = uint(in.pos.x) + uint(in.pos.y) * 64;
   const uint k = idx[p];
   const ulong v = pal64[k];
   outf[k] = float(uint(v & 0xffff)) * 0.5f;
   out64[k] = v ^ 0x0123456789abcdefUL;
   return float4(float(uint(v)), float(uint(v >> 32)), float(k), 1);
}
