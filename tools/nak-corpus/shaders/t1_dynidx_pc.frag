#version 450
// P1: array of combined samplers in a cbuf-mapped set, dynamically uniform index from a push constant ->
// handle = LDC with a non-zero register offset (not uniform-capable) -> R2UR -> propagate_r2ur -> GPR handle.
// layout: pipetest -a 4 -p 4   (set 0 binding 0 = COMBINED_IMAGE_SAMPLER x4; push constant 4 bytes)
layout(set = 0, binding = 0) uniform sampler2D tex[4];
layout(push_constant) uniform PC { int i; } pc;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() { color = texture(tex[pc.i], uv); }
