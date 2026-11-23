#version 450
layout(set = 0, binding = 0) uniform sampler2DShadow tex;
layout(set = 0, binding = 1) uniform U { vec4 v[24]; } u;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() {
  vec4 a = vec4(texture(tex, vec3(uv, 0.5)));
  for (int k = 0; k < 24; k++) a += u.v[k] * float(k + 1);
  vec4 t = vec4(0.0);
  if (uv.x > 0.5) {
    vec4 s = vec4(uv.y);
    for (int k = 0; k < 24; k++) s = s * u.v[k] + uv.xyxy;
    float c = s.x + s.y + s.z + s.w;
    t = vec4(textureLodOffset(tex, vec3(c, s.x, c), c, ivec2(1, -1)));
  }
  color = a + t;
}
