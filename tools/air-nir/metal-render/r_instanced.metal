// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// vertex_id, instance_id, base_vertex and base_instance with an instanced draw: one quad per instance, its cell from the
// instance id, its corners from a buffer indexed by the vertex id less the draw's first vertex
#include <metal_stdlib>
using namespace metal;
struct V2F { float4 pos [[position]]; uint iid [[flat]]; float vstart; float binst; };
vertex V2F ri_vs(uint vid [[vertex_id]], uint iid [[instance_id]], uint bi [[base_instance]], constant float2 *corner [[buffer(0)]])
{
   const uint c = (vid - 3) % 6;
   const float2 cell = float2(iid % 4, iid / 4);
   const float2 p = (cell + corner[c]) * 0.5;   // 4x4 cells over [0,2]
   V2F o;
   o.pos = float4(p.x - 1, 1 - p.y, 0, 1);
   o.iid = iid; o.vstart = float(vid - c); o.binst = float(bi);
   return o;
}
fragment float4 ri_fs(V2F in [[stage_in]]) { return float4(float(in.iid), in.vstart, in.binst, 1); }
