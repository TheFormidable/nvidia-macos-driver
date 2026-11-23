#version 450
layout(set = 0, binding = 0) uniform sampler2D ta[4];
layout(set = 0, binding = 1) uniform U { vec4 v[28]; int sel; int cnt; } u;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() {
  vec4 e = texture(ta[1], uv);
  vec4 a = vec4(0.0); for (int k = 0; k < 28; k++) a += u.v[k] * float(k + 1);
  vec4 t = vec4(0.0);
  for (int i = 0; i < u.cnt; i++) {
    if (uv.x > float(i) * 0.05) {
    vec4 s = vec4(uv.y); for (int k = 0; k < 28; k++) s = s * u.v[k] + uv.xyxy;
      t += textureLod(ta[1], s.xy + t.zw, s.z) + texture(ta[u.sel & 3], s.wz, t.x);
      if (t.y > 2.0) break;
    }
  }
  color = a + t + e;
}
