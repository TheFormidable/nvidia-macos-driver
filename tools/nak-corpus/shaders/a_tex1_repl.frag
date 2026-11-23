#version 450
// stands in for a_tex1 under MESA_SPIRV_READ_PATH: the same interface, more work.  layout: -s 1
layout(set = 0, binding = 0) uniform sampler2D tex;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() { vec4 a = texture(tex, uv); vec4 b = texture(tex, uv.yx * 0.5); color = sqrt(a * b + vec4(uv, 0.25, 1.0)); }
