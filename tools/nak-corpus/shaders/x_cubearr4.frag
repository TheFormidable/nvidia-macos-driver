#version 450
layout(set = 0, binding = 0) uniform samplerCubeArray ta[4];
layout(set = 0, binding = 1) uniform U { vec4 v[28]; int sel; int cnt; } u;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() {
  vec4 e = texture(ta[3], vec4(uv, uv)) + texture(ta[0], vec4(uv, uv), 1.0);
  vec4 a = vec4(0.0); for (int k = 0; k < 28; k++) a += u.v[k] * float(k + 1);
  vec4 t = vec4(0.0);
  if (uv.x > 0.5) {
    vec4 s = vec4(uv.y); for (int k = 0; k < 28; k++) s = s * u.v[k] + uv.xyxy;
    for (int j = 0; j < 4; j++) t += textureLod(ta[j], s.wzyx + t, s.x);
    t += texture(ta[3], vec4(s.x), s.x);
  }
  color = a + t + e;
}
