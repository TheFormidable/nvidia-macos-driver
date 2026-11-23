#version 450
// P5: a textureLod in a divergent block whose handle (constant index, ULDC) is hoisted to the uniform start block,
// while more than 80 uniform values are live into that block and used there before the texture, so spill_values
// evicts the handle and rewrites its use in the non-uniform block to the GPR copy.
// layout: pipetest -s 1 -u   (binding 0 = sampler x1, binding 1 = uniform buffer)
layout(set = 0, binding = 0) uniform sampler2D tex;
layout(set = 0, binding = 1) uniform U { vec4 v[32]; } u;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() {
  vec4 a = vec4(0.0);
  for (int k = 0; k < 32; k++) a = a * u.v[(k + 1) % 32] + u.v[k];
  vec4 t = vec4(0.0);
  if (uv.x > 0.5) {
    vec4 s = vec4(uv.y);
    for (int k = 0; k < 32; k++) s = s * u.v[k] + uv.xyxy;
    t = textureLod(tex, uv + s.xy, 0.0);
  }
  color = a + t;
}
