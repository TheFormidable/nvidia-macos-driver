#version 450
// control: one combined sampler, constant index, set cbuf-mapped -> handle is a ULDC result, no abort before or after.
// layout: pipetest -s 1   (set 0 binding 0 = COMBINED_IMAGE_SAMPLER x1)
layout(set = 0, binding = 0) uniform sampler2D tex;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() { color = texture(tex, uv); }
