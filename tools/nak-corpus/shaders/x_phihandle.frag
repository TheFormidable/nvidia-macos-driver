#version 450
layout(set = 0, binding = 0) uniform sampler2D ta[4];
layout(set = 0, binding = 1) uniform U { vec4 v[28]; int sel; int cnt; } u;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() {
  int idx = 0;
  if (u.sel > 2) idx = 3; else if (u.sel > 0) idx = 1;
  vec4 e = texture(ta[idx], uv);
  vec4 a = vec4(0.0); for (int k = 0; k < 28; k++) a += u.v[k] * float(k + 1);
  vec4 t = vec4(0.0);
  if (uv.x > 0.5) {
    vec4 s = vec4(uv.y); for (int k = 0; k < 28; k++) s = s * u.v[k] + uv.xyxy;
    t = textureLod(ta[idx], s.xy, s.w) + texture(ta[idx], vec2(s.x), s.x);
  }
  color = a + t + e;
}
