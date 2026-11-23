#version 450
layout(set = 0, binding = 0) uniform sampler2D tex;
layout(set = 0, binding = 1) uniform U { vec4 v[24]; } u;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() {
  vec4 a = texture(tex, uv);
  for (int k = 0; k < 24; k++) a += u.v[k] * float(k + 1);
  vec4 t = vec4(0.0);
  if (uv.x > 0.5) {
    vec4 s = vec4(uv.y);
    for (int k = 0; k < 24; k++) s = s * u.v[k] + uv.xyxy;
    float c = s.x + s.y + s.z + s.w;
    t = textureLod(tex, vec2(c, c), 0.0) + textureLod(tex, s.xy, c) + texture(tex, vec2(s.x, c));
  }
  color = a + t;
}
