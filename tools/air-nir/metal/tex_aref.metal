// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// An array_ref of textures (as MPS's kernels take one, its length a function constant's): plugin-run.sh rewrites this
// array<texture2d<float>, 4> argument's type name to array_ref<texture2d<float, sample>> and drops its literal count,
// which Metal's public compiler does not let a kernel argument be; the translator reaches the texture indices left
// from its base (128 at most),
// every element through the bindless heap at the per-draw table's slot, an index past the bound ones the unbound
// texture's (the typed dummy)
#include <metal_stdlib>
using namespace metal;
kernel void tex_aref(array<texture2d<float>, 4> t [[texture(1)]], device float4 *out [[buffer(0)]], constant uint *k [[buffer(1)]],
                     uint2 gid [[thread_position_in_grid]])
{
   const uint i = gid.y * 8u + gid.x;
   out[i * 2u] = t[2].read(gid);
   out[i * 2u + 1u] = t[k[i & 7u]].read(gid);
}
