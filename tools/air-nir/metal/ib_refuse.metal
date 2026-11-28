// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// An imageblock argument (tile memory): the frontend has no model of it and refuses the kernel, which stays on the
// SPIR-V road (the plugin's tile path)
#include <metal_stdlib>
using namespace metal;
struct Px { float4 c [[color(0)]]; };
kernel void ib_refuse(imageblock<Px, imageblock_layout_implicit> ib, device float4 *out [[buffer(0)]],
                      ushort2 lid [[thread_position_in_threadgroup]])
{
   out[lid.y * 16 + lid.x] = ib.read(lid).c;
}
