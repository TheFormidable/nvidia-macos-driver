// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// CoreAnimation's buffer-to-image blit as QuartzCore's default.metallib has it (buffer_image_blit_vert_lpf and
// buffer_image_blit_frag_lpf, the pipeline Terminal draws its window with): an instanced strip of 4 vertices per rect,
// ushort vertex and instance ids, the rects at buffer(1) and the matrix at buffer(2) of the vertex stage; the fragment
// reads one byte of a device buffer at buffer(9) at offset + x + y * bytes-per-row and writes two colour targets.
#include <metal_stdlib>
using namespace metal;
struct BufferImageRect { float2 min; float2 size; float offset; float bpr; };
struct VOut { float4 position [[position]]; float2 uv; float offset; float bpr; float x_max; float y_max; };
vertex VOut rbl_vs(ushort index [[vertex_id]], ushort instance [[instance_id]], constant float4x4 &mvp_matrix [[buffer(2)]],
                   device const BufferImageRect *rects [[buffer(1)]])
{
   const BufferImageRect r = rects[instance];
   const float2 uv = float2((index & 1) ? 0.0f : r.size.x, (index & 2) ? 0.0f : r.size.y);
   VOut o;
   o.position = mvp_matrix * float4(r.min.x + uv.x, r.min.y - uv.y, 0, 1);
   o.uv = uv; o.offset = r.offset; o.bpr = r.bpr; o.x_max = r.size.x - 1; o.y_max = r.size.y - 1;
   return o;
}
struct RLEBlitFragUniform { float4 color; };
struct FOut { float4 color0 [[color(0)]]; float4 color1 [[color(1)]]; };
fragment FOut rbl_fs(VOut in [[stage_in]], constant RLEBlitFragUniform &u [[buffer(1)]], device const uchar *buffer [[buffer(9)]])
{
   const float x = min(floor(in.uv.x), in.x_max), y = min(floor(in.uv.y), in.y_max);
   const float4 c = float(buffer[int(x + in.offset + y * in.bpr)]) * u.color;
   FOut o; o.color0 = c; o.color1 = c;
   return o;
}
