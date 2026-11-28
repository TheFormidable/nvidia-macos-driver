// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Arrays of textures: array<texture2d<float>, 4> at [[texture(1)]] (textures 1 to 4) read by a known index and by one
// chosen at run time (one per thread, and one from a buffer, past the end once: the last element), and
// array<texture2d<float, access::write>, 2> at [[texture(8)]] written by known indices
#include <metal_stdlib>
using namespace metal;

kernel void tex_arrays(array<texture2d<float>, 4> t [[texture(1)]], array<texture2d<float, access::write>, 2> w [[texture(8)]],
                       device float4 *out [[buffer(0)]], constant uint *k [[buffer(1)]], uint2 gid [[thread_position_in_grid]])
{
   const uint i = gid.y * 8u + gid.x;
   out[i * 4u] = t[2].read(gid);
   out[i * 4u + 1u] = t[i & 3u].read(gid);
   out[i * 4u + 2u] = t[k[i & 1u]].read(uint2(gid.y, gid.x));
   const float4 v = t[0].read(gid) + t[3].read(gid);
   out[i * 4u + 3u] = v;
   w[0].write(v, gid);
   w[1].write(t[1].read(gid) * 2.0f, gid);
}
