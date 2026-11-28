// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Written arrays of textures, as the translator binds them since round 9 (resources::split_storage_texture_arrays):
// array<texture2d<float, access::write>, 3> at [[texture(4)]] written by a known index (its own binding) and by one
// chosen at run time (the storage heap at the table's slot, past the end once: the last element); an array of
// texture_buffers read by a known index and a written one by a known index (their own bindings)
#include <metal_stdlib>
using namespace metal;
kernel void tex_warrays(array<texture2d<float, access::write>, 3> w [[texture(4)]], array<texture_buffer<float>, 2> tb [[texture(0)]],
                        array<texture_buffer<float, access::write>, 2> wtb [[texture(2)]], constant uint *k [[buffer(0)]],
                        device float4 *out [[buffer(1)]], uint2 gid [[thread_position_in_grid]])
{
   const uint i = gid.y * 8u + gid.x;
   w[1].write(float4(float(i), 1, 2, 3), gid);
   const uint e = (i & 3u) == 3u ? k[0] : (i % 3u == 1u ? 0u : 2u);   /* 0 or 2, and k[0] (past the end: the last) */
   if (e != 1u)
      w[e].write(float4(float(i) * 0.5f, float(e), 0, 1), gid);
   out[i] = tb[1].read(i) + tb[0].read(i) * 2.0f;
   wtb[1].write(float4(float(i) + 0.25f, 0, 0, 1), i);
}
