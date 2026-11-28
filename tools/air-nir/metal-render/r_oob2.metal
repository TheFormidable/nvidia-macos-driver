// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Buffers read at indices the data gives, as the SPIR-V road keeps them (bounds-checked storage-buffer descriptors): a
// vertex function reading a constant palette (one triangle in range, the other past its end), and a fragment function
// reading 8-bit and 16-bit elements of buffers whose lengths are not multiples of 4 (the device checks against the
// length rounded up to 4 bytes) and a palette at indices whose byte offsets pass 4 GiB (the offset is 32-bit)
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; float4 c [[flat]]; };
vertex VOut ro2_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]], constant float4 *vpal [[buffer(1)]],
                   device const uint *vidx [[buffer(2)]])
{
   VOut o;
   o.pos = float4(p[vid], 0, 1);
   o.c = vpal[vidx[vid]];
   return o;
}
fragment float4 ro2_fs(VOut in [[stage_in]], constant float4 *pal [[buffer(0)]], device const uint *idx [[buffer(3)]],
                       device const uchar *b8 [[buffer(4)]], device const half *h [[buffer(5)]])
{
   const uint2 q = uint2(in.pos.xy);
   const uint i = idx[q.y * 64 + q.x];
   const uint k = idx[4096 + q.y * 64 + q.x];
   return in.c + float4(float(b8[k]), float(h[k]), pal[i].y, pal[i].w);
}
