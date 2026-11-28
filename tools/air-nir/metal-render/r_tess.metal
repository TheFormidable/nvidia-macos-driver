// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// r_tess: post-tessellation vertex functions (the plugin's tessellation path: its TCS and VS, this TES)
#include <metal_stdlib>
using namespace metal;

// a quad patch: four control points (position and a coordinate pair each, pulled per control point), a tint per patch
struct tq_cp { float4 pos [[attribute(0)]]; float2 st [[attribute(1)]]; };
struct tq_patch { patch_control_point<tq_cp> cp; float4 tint [[attribute(2)]]; };
struct tq_out {
   float4 position [[position]];
   float4 f;                    // s*s, t*t*t, u, v: the first two piecewise linear over the tessellated triangles
   float4 g;                    // (s-1/2)^2, (t-1/2)^2: symmetric about the patch's middle
   float4 tint [[flat]];
   uint pid [[flat]];
};

[[patch(quad, 4)]] vertex tq_out
tq_vs(tq_patch p [[stage_in]], float2 uv [[position_in_patch]], uint pid [[patch_id]])
{
   const float4 a = mix(p.cp[0].pos, p.cp[1].pos, uv.x), b = mix(p.cp[3].pos, p.cp[2].pos, uv.x);
   const float2 sa = mix(p.cp[0].st, p.cp[1].st, uv.x), sb = mix(p.cp[3].st, p.cp[2].st, uv.x);
   const float2 st = mix(sa, sb, uv.y);
   tq_out o;
   o.position = mix(a, b, uv.y);
   o.f = float4(st.x * st.x, st.y * st.y * st.y, uv.x, uv.y);
   o.g = float4((st.x - 0.5f) * (st.x - 0.5f), (st.y - 0.5f) * (st.y - 0.5f), 0.0f, 1.0f);
   o.tint = p.tint;
   o.pid = pid;
   return o;
}

struct tq_col { float4 a [[color(0)]]; float4 b [[color(1)]]; };

fragment tq_col
tq_fs(tq_out i [[stage_in]], bool front [[front_facing]])
{
   tq_col c;
   c.a = float4(i.f.x, i.f.y, i.tint.x + float(i.pid), front ? 1.0f : -1.0f);
   c.b = float4(i.g.x, i.g.y, i.f.z, i.f.w);
   return c;
}

// a triangle patch: three control points (position and colour), barycentrics, the instance as a patch input, and the
// patch's own data from a buffer at its patch id
struct tt_cp { float2 pos [[attribute(0)]]; float4 col [[attribute(1)]]; };
struct tt_patch { patch_control_point<tt_cp> cp; };
struct tt_out {
   float4 position [[position]];
   float4 col;
   float2 extra [[flat]];
};

[[patch(triangle, 3)]] vertex tt_out
tt_vs(tt_patch p [[stage_in]], float3 b [[position_in_patch]], uint pid [[patch_id]], uint inst [[instance_id]],
      constant float4 *shift [[buffer(1)]])
{
   const float2 xy = p.cp[0].pos * b.x + p.cp[1].pos * b.y + p.cp[2].pos * b.z;
   tt_out o;
   o.position = float4(xy + shift[inst].xy, 0.0f, 1.0f);
   o.col = p.cp[0].col * b.x + p.cp[1].col * b.y + p.cp[2].col * b.z;
   o.extra = float2(float(pid), float(inst));
   return o;
}

fragment float4
tt_fs(tt_out i [[stage_in]])
{
   return float4(i.col.xyz, i.col.w + i.extra.x * 10.0f + i.extra.y * 100.0f);
}

// the factors of each triangle patch from a kernel: edges and inside from a table per patch
kernel void
tt_factors(device MTLTriangleTessellationFactorsHalf *f [[buffer(0)]], constant float *lv [[buffer(1)]],
           uint i [[thread_position_in_grid]])
{
   const half v = half(lv[i]);
   f[i].edgeTessellationFactor[0] = v;
   f[i].edgeTessellationFactor[1] = v;
   f[i].edgeTessellationFactor[2] = v;
   f[i].insideTessellationFactor = v;
}
