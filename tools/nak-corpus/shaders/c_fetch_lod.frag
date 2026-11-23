#version 450
// (c) texelFetch and textureLod.  layout: -s 2
layout(set = 0, binding = 0) uniform sampler2D t0;
layout(set = 0, binding = 1) uniform sampler2D t1;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() {
   ivec2 c = ivec2(uv * 64.0);
   color = texelFetch(t0, c, 0) + textureLod(t1, uv, 2.5);
}
