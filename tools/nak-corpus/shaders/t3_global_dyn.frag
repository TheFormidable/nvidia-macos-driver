#version 450
// P2 with a dynamic index: the binding spans past 64 KiB, so every element is read with a global load.
// layout: pipetest -s 1 -a 16400 -p 4
layout(set = 0, binding = 1) uniform sampler2D tex[16400];
layout(push_constant) uniform PC { int i; } pc;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() { color = texture(tex[pc.i], uv); }
