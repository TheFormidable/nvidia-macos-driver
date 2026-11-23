#version 450
// (b) tex[ubo.idx]: an array of 4 combined samplers indexed by a uniform (dynamically uniform, not constant) value
// from a uniform buffer.  layout: -a 4 -u  (binding 0 = sampler2D[4], binding 1 = the uniform block)
layout(set = 0, binding = 0) uniform sampler2D tex[4];
layout(set = 0, binding = 1) uniform U { int idx; } ubo;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() { color = texture(tex[ubo.idx], uv); }
