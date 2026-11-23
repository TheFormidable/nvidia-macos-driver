#version 450
layout(set = 0, binding = 0) uniform sampler2D tex;
layout(set = 0, binding = 1) uniform U { vec4 v[24]; } u;
layout(location = 0) out vec2 uv;
void main() {
  vec2 p = vec2((gl_VertexIndex << 1) & 2, gl_VertexIndex & 2);
  vec4 a = textureLod(tex, p, 0.0);
  for (int k = 0; k < 24; k++) a += u.v[k] * float(k + 1);
  vec4 t = vec4(0.0);
  if (gl_VertexIndex > 1) {
    vec4 s = vec4(p.y);
    for (int k = 0; k < 24; k++) s = s * u.v[k] + p.xyxy;
    float c = s.x + s.y + s.z + s.w;
    t = textureLod(tex, vec2(c, c), 0.0);
  }
  uv = p + a.xy + t.xy;
  gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
