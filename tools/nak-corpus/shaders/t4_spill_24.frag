#version 450
// P5: textureLod in a divergent block; its handle (constant index, ULDC) is hoisted to the end of the uniform start
// block; 24 vec4 uniforms are defined in the start block (used there) and used again in the divergent block before
// the texture, so more than 80 UGPR values are live into that block and spill_values rewrites the handle's use there
// to its GPR copy. layout: pipetest -s 1 -u   (binding 0 = sampler x1, binding 1 = uniform buffer)
layout(set = 0, binding = 0) uniform sampler2D tex;
layout(set = 0, binding = 1) uniform U { vec4 v[24]; } u;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() {
  vec4 a = vec4(0.0);
  for (int k = 0; k < 24; k++) a += u.v[k] * float(k + 1);
  vec4 t = vec4(0.0);
  if (uv.x > 0.5) {
    vec4 s = vec4(uv.y);
    for (int k = 0; k < 24; k++) s = s * u.v[k] + uv.xyxy;
    t = textureLod(tex, uv + s.xy + s.zw, 0.0);
  }
  color = a + t;
}
