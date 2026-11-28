// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A binary search over a sorted buffer through pointers (a phi and a select of pointers into it, as RenderBox's uber
// vertex function searches its segments), at a key and a length the data gives: the SPIR-V road keeps the buffer a
// bounds-checked storage-buffer descriptor, and the bound past the last element reads 0
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; };
vertex VOut rbs_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]]) { VOut o; o.pos = float4(p[vid], 0, 1); return o; }
fragment float4 rbs_fs(VOut in [[stage_in]], device const float *sorted [[buffer(0)]], constant uint &n [[buffer(1)]],
                       device const float *keys [[buffer(2)]])
{
   const uint2 q = uint2(in.pos.xy);
   const float key = keys[q.y * 64 + q.x];
   device const float *first = sorted;
   uint count = n;
   do {
      const uint step = count / 2;
      device const float *it = first + step;
      const bool after = !(key < *it);
      first = after ? it + 1 : first;
      count = after ? count - (step + 1) : step;
   } while (count > 0);
   return float4(*first, first[0] * 2.0, 0, 1);
}
