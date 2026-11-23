#version 450
layout(set = 0, binding = 0) uniform sampler2DArrayShadow ta[4];
layout(set = 0, binding = 1) uniform U { vec4 v[28]; int sel; int cnt; } u;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() {
  vec4 e = vec4(texture(ta[2], vec4(uv, 1.0, 0.5)));
  vec4 a = vec4(0.0); for (int k = 0; k < 28; k++) a += u.v[k] * float(k + 1);
  vec4 t = vec4(0.0);
  if (uv.x > 0.5) {
    vec4 s = vec4(uv.y); for (int k = 0; k < 28; k++) s = s * u.v[k] + uv.xyxy;
    float c = s.x * s.y;
    t = vec4(textureOffset(ta[2], vec4(c, c, c, c), ivec2(1, 1)) + textureGrad(ta[2], vec4(c, c, c, c), vec2(c), vec2(c)));
  }
  color = a + t + e;
}
