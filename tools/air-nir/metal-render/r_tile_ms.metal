// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A multisampled tile function that reads and writes its samples in a loop (round 12): its library function did not
// translate (air.get_imageblock_samples unknown, the sample index taken for the data rate), so newFunctionWithName
// failed on both roads. r_tile_ms makes it and its tile pipeline for 4 samples; it does not dispatch it (the harness
// draws single-sample attachments), and draws as r_tile's vertex and fragment functions do.
#include <metal_stdlib>
using namespace metal;
struct VOut { float4 pos [[position]]; };
vertex VOut rtm_vs(uint vid [[vertex_id]], constant float2 *p [[buffer(0)]]) { VOut o; o.pos = float4(p[vid], 0, 1); return o; }
fragment float4 rtm_fs(VOut in [[stage_in]]) { return float4(in.pos.x / 64.0f, in.pos.y / 64.0f, 0.25f, 1.0f); }
struct Pm { half4 c [[color(0)]]; };
kernel void rtm_tile(imageblock<Pm, imageblock_layout_implicit> ib, ushort2 tid [[thread_position_in_threadgroup]])
{
   for (ushort s = 0; s < ib.get_num_samples(); s++) {
      Pm v = ib.read(tid, s, imageblock_data_rate::sample);
      v.c.x += half(s) * 0.125h;
      ib.write(v, tid, s, imageblock_data_rate::sample);
   }
}
