// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// perspective, no_perspective and flat varyings on a triangle whose vertices have different w
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; float s; float np [[center_no_perspective]]; float fl [[flat]]; int fi [[flat]]; };
vertex VOut rp_vs(uint vid [[vertex_id]], constant float4 *v [[buffer(0)]], constant float *val [[buffer(1)]])
{
   VOut o;
   o.pos = v[vid]; o.s = val[vid]; o.np = val[vid]; o.fl = val[vid]; o.fi = int(vid) + 10;
   return o;
}
fragment float4 rp_fs(VOut in [[stage_in]]) { return float4(in.s, in.np, in.fl, float(in.fi)); }
