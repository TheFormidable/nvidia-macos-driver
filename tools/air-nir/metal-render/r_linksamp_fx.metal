// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// r_linksamp's visible function, in a module of its own (linked into rls_fs when its pipeline is made)
#include <metal_stdlib>
using namespace metal;
[[visible]] float4 rls_fx(texture2d<float> t, float2 uv)
{
   constexpr sampler s(filter::nearest, address::repeat);
   return t.sample(s, uv);
}
