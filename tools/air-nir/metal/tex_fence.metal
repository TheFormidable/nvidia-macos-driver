// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A read_write texture read, written, fenced and read again by the same thread.
#include <metal_stdlib>
using namespace metal;

kernel void tex_fence(texture2d<float, access::read_write> t [[texture(0)]], uint2 gid [[thread_position_in_grid]])
{
   float v = t.read(gid).x;
   t.write(float4(v + 1.0f), gid);
   t.fence();
   float w = t.read(gid).x;
   t.write(float4(w * 2.0f), gid);
}
