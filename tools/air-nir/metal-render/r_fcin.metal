// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// stage inputs that exist only under a function constant: a vertex attribute and a fragment input whose constant is
// true (they are read), and another pair whose constant is false (they do not exist)
#include <metal_stdlib>
using namespace metal;
constant bool fci_col [[function_constant(0)]];
constant bool fci_junk [[function_constant(1)]];
struct VIn {
   float2 pos [[attribute(0)]];
   float4 col [[attribute(1), function_constant(fci_col)]];
   float4 junk [[attribute(2), function_constant(fci_junk)]];
};
struct V2F {
   float4 pos [[position]];
   float2 uv;
   float4 tint [[function_constant(fci_col)]];
   float4 extra [[function_constant(fci_junk)]];
};
vertex V2F rfi_vs(VIn in [[stage_in]])
{
   V2F o;
   o.pos = float4(in.pos, 0, 1);
   o.uv = in.pos * 0.5 + 0.5;
   if (fci_col) o.tint = in.col;
   if (fci_junk) o.extra = in.junk;
   return o;
}
fragment float4 rfi_fs(V2F in [[stage_in]])
{
   float4 c = float4(in.uv, 0, 0);
   if (fci_col) c = in.tint * float4(1, 1, 2, 1);
   if (fci_junk) c += in.extra;
   return c;
}
