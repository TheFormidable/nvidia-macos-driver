// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// An array of texture_buffers read and written at indices known only at run time (round 10): the translator keeps one
// array binding for each (32 + n with three elements, 480 + n with two), which the plugin's layout holds with one
// descriptor each
#include <metal_stdlib>
using namespace metal;
kernel void tex_tbarray(array<texture_buffer<float>, 3> tb [[texture(0)]], array<texture_buffer<float, access::write>, 2> wtb [[texture(4)]],
                        device const uint *sel [[buffer(0)]], device float4 *out [[buffer(1)]], uint i [[thread_position_in_grid]])
{
   const uint k = sel[i];
   out[i] = tb[k % 3u].read(i);
   wtb[k & 1u].write(float4(float(i) + 0.5f, 0, 0, 1), i);
}
