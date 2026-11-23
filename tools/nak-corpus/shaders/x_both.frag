#version 450
layout(set = 0, binding = 0) uniform sampler2D ta[4];
layout(set = 0, binding = 1) uniform U { vec4 v[28]; int sel; int cnt; } u;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
layout(push_constant) uniform PC { int i; } pc;
void main() {
  vec4 e = texture(ta[pc.i], uv) + texture(ta[0], uv.yx);
  vec4 a = vec4(0.0); for (int k = 0; k < 28; k++) a += u.v[k] * float(k + 1);
  vec4 t = vec4(0.0);
  if (uv.x > 0.5) {
    vec4 s = vec4(uv.y); for (int k = 0; k < 28; k++) s = s * u.v[k] + uv.xyxy;
    t = textureLod(ta[pc.i], s.xy, s.w) + texture(ta[0], vec2(s.x), s.y) + texture(ta[0], s.zw);
  } else {
    t = texture(ta[pc.i], uv * a.xy, a.z);
  }
  color = a + t + e;
}
