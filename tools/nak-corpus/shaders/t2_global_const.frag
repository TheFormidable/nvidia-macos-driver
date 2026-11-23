#version 450
// P2: the descriptor lies past 64 KiB of its set, so NVK reads it with a global load (load_global_constant_offset)
// instead of LDC; constant index. Ld is not uniform-capable -> R2UR -> propagate_r2ur -> GPR handle.
// layout: pipetest -s 1 -a 16400   (binding 0 = sampler x1, binding 1 = sampler x16400 at set offset 4; element
// 16399 is at offset 65600 > NVK_MAX_CBUF_SIZE)
layout(set = 0, binding = 1) uniform sampler2D tex[16400];
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() { color = texture(tex[16399], uv); }
