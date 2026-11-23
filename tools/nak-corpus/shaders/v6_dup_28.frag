#version 450
// P5: the handle is defined early in the uniform start block (a first texture() there uses it), then 28 vec4 uniforms
// are defined and used both in the start block and in a divergent block before a second, textureLod, sample of the
// same texture. More than 80 UGPR values are live, the handle has the farthest next use, so spill_values evicts it
// and rewrites its use in the divergent block to the GPR copy.
// layout: pipetest -s 1 -u   (set 0 binding 0 = COMBINED_IMAGE_SAMPLER x1, binding 1 = UNIFORM_BUFFER x1)
layout(set = 0, binding = 0) uniform sampler2D tex;
layout(set = 0, binding = 1) uniform U { vec4 v[28]; } u;
layout(location = 0) in vec2 uv;
layout(location = 0) out vec4 color;
void main() {
  vec4 a = texture(tex, uv);
  for (int k = 0; k < 28; k++) a += u.v[k] * float(k + 1);
  vec4 t = vec4(0.0);
  if (uv.x > 0.5) {
    vec4 s = vec4(uv.y);
    for (int k = 0; k < 28; k++) s = s * u.v[k] + uv.xyxy;
    float c = s.x + s.y + s.z + s.w; t = textureLod(tex, vec2(c, c), 0.0);
  }
  color = a + t;
}
