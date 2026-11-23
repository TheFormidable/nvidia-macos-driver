#version 450
layout(set = 0, binding = 0) uniform sampler2D tex[4];
layout(push_constant) uniform PC { int i; } pc;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() { color = vec4(0.0); if (uv.x > 0.5) color = textureLod(tex[pc.i], vec2(uv.y, uv.y), 0.0); }
