// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// raster order groups (round 10): on a [[color(0)]] input, a written texture and a device buffer, as WindowServer's
// inplace_copy fragments (QuartzCore) carry one on their written texture. The translator binds each as it binds one
// without the group (no fragment shader interlock), and so does the frontend. Two instances of one quad, as r_fetch.
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; float2 uv; uint layer [[flat]]; };
vertex VOut rrog_vs(uint vid [[vertex_id]], uint iid [[instance_id]], constant float2 *p [[buffer(0)]])
{
   VOut o;
   o.pos = float4(p[vid], 0, 1); o.uv = float2(p[vid].x * 0.5 + 0.5, 0.5 - p[vid].y * 0.5); o.layer = iid;
   return o;
}
fragment float4 rrog_fs(VOut in [[stage_in]], float4 dst [[color(0), raster_order_group(0)]],
                        texture2d<float, access::write> tw [[texture(0), raster_order_group(0)]],
                        device const float4 *k [[buffer(0), raster_order_group(1)]])
{
   const float4 src = float4(in.uv, in.layer ? 0.75 : 0.25, 1);
   const float4 o = dst * 0.5 + src * 0.5 + k[in.layer];
   tw.write(o, uint2(in.pos.xy));
   return o;
}
