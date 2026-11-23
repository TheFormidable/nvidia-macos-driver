#!/usr/bin/env python3
# usage: gen.py <dir>  -- writes <dir>/sh/<name>.frag (and .vert for vertex cases) and <dir>/cases.txt "name|args|vert"
# The generated set of the NAK corpus (tools/nak-corpus/README.md): 125 fragment and vertex shaders, many textures,
# branches, loops, shadow, cube, array, 3D, offset, grad, fetch and gather forms, under register pressure.
import os, sys
W = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.getcwd()
SH = os.path.join(W, "sh")
os.makedirs(SH, exist_ok=True)
cases = []

HDR = "#version 450\n#extension GL_EXT_nonuniform_qualifier : enable\n"
IO = "layout(location = 0) in vec2 uv;\nlayout(location = 0) out vec4 color;\n"

def ubo(binding, n):
    return f"layout(set = 0, binding = {binding}) uniform U {{ vec4 v[{n}]; }} u;\n"

def pre(n):
    return f"  vec4 a = vec4(0.0);\n  for (int k = 0; k < {n}; k++) a += u.v[k] * float(k + 1);\n"

def inb(n, var="s"):
    return f"    vec4 {var} = vec4(uv.y);\n    for (int k = 0; k < {n}; k++) {var} = {var} * u.v[k] + uv.xyxy;\n"

def add(name, args, src, vert="quad", vsrc=None):
    with open(os.path.join(SH, name + ".frag"), "w") as f:
        f.write(src)
    if vsrc is not None:
        with open(os.path.join(SH, name + ".vert"), "w") as f:
            f.write(vsrc)
        vert = name
    cases.append((name, args, vert))

# 1-6: many textures, divergent, uniform pressure, several handles spilled at once
for ns, n in [(4, 24), (8, 24), (8, 32), (12, 20)]:
    decl = "".join(f"layout(set = 0, binding = {i}) uniform sampler2D t{i};\n" for i in range(ns))
    body = HDR + decl + ubo(ns, n) + IO + "void main() {\n" + pre(n)
    body += "".join(f"  a += texture(t{i}, uv * {i + 1}.0);\n" for i in range(ns))
    body += "  vec4 t = vec4(0.0);\n  if (uv.x > 0.5) {\n" + inb(n)
    body += "".join(f"    t += textureLod(t{i}, s.xy * {i + 1}.0 + s.zw, float({i}) * s.x);\n" for i in range(ns))
    body += "  }\n  color = a + t;\n}\n"
    add(f"r_many{ns}_{n}", f"-s {ns} -u", body)

# both arms of a branch
n = 24
body = HDR + "layout(set = 0, binding = 0) uniform sampler2D t0;\nlayout(set = 0, binding = 1) uniform sampler2D t1;\n" + ubo(2, n) + IO
body += "void main() {\n" + pre(n) + "  vec4 t;\n  if (uv.x > 0.5) {\n" + inb(n) + "    t = texture(t0, s.xy + s.zw, s.x);\n  } else {\n" + inb(n, "q") + "    t = textureLod(t1, q.yx - q.wz, q.y) + texture(t0, q.zw);\n  }\n  color = a + t;\n}\n"
add("r_botharms", "-s 2 -u", body)

# nested loops, texture inside
body = HDR + "layout(set = 0, binding = 0) uniform sampler2D t0;\nlayout(set = 0, binding = 1) uniform sampler3D t1;\n" + ubo(2, 24) + IO
body += """layout(push_constant) uniform PC { int n; int m; } pc;
void main() {
""" + pre(24) + """  vec4 t = vec4(0.0);
  for (int i = 0; i < pc.n; i++) {
    if (uv.x > float(i) * 0.1) {
      for (int j = 0; j < pc.m; j++) {
        vec4 s = vec4(uv.y + float(j));
        for (int k = 0; k < 24; k++) s = s * u.v[k] + uv.xyxy;
        t += textureLod(t0, s.xy, s.z) + texture(t1, s.xyz, s.w);
        if (t.x > 4.0) break;
      }
    }
  }
  color = a + t;
}
"""
add("r_nestloop", "-s 2 -u -p 8", body)

# gradient / proj / shadow / cube array / offset forms in a divergent block with pressure
forms = {
    "grad": ("sampler2D", "textureGrad(tx, s.xy, s.zw, s.wz)"),
    "proj": ("sampler2D", "textureProj(tx, s.xyz, s.w)"),
    "projlod": ("sampler2D", "textureProjLod(tx, s.xyzw, s.x)"),
    "shadow": ("sampler2DShadow", "vec4(texture(tx, s.xyz, s.w))"),
    "shadowoff": ("sampler2DShadow", "vec4(textureOffset(tx, s.xyz, ivec2(1, -2), s.w))"),
    "shadowlodoff": ("sampler2DShadow", "vec4(textureLodOffset(tx, s.xyz, s.w, ivec2(3, 1)))"),
    "arrshadow": ("sampler2DArrayShadow", "vec4(texture(tx, s.xyzw))"),
    "arrshadowoff": ("sampler2DArrayShadow", "vec4(textureOffset(tx, s.xyzw, ivec2(-1, 1)))"),
    "cubearr": ("samplerCubeArray", "texture(tx, s.xyzw, s.x)"),
    "cubearrlod": ("samplerCubeArray", "textureLod(tx, s.xyzw, s.y)"),
    "cubearrshadow": ("samplerCubeArrayShadow", "vec4(texture(tx, s.xyzw, s.x))"),
    "cubeshadow": ("samplerCubeShadow", "vec4(texture(tx, s.xyzw))"),
    "cubelod": ("samplerCube", "textureLod(tx, s.xyz, s.w)"),
    "arrlodoff": ("sampler2DArray", "textureLodOffset(tx, s.xyz, s.w, ivec2(2, 2))"),
    "3dbiasoff": ("sampler3D", "textureOffset(tx, s.xyz, ivec3(1, 2, 3), s.w)"),
    "1d": ("sampler1D", "textureLod(tx, s.x, s.y)"),
    "1darr": ("sampler1DArray", "texture(tx, s.xy, s.z)"),
    "1dshadow": ("sampler1DShadow", "vec4(texture(tx, s.xyz))"),
    "fetch": ("sampler2D", "texelFetch(tx, ivec2(s.xy), int(s.z))"),
    "gather": ("sampler2D", "textureGather(tx, s.xy, 1)"),
    "querylod": ("sampler2D", "vec4(textureQueryLod(tx, s.xy), 0.0, 0.0)"),
    "dupall": ("sampler2DArray", "textureLodOffset(tx, vec3(s.x, s.x, s.x), s.x, ivec2(1, 1))"),
    "dupcube": ("samplerCubeArray", "texture(tx, vec4(s.y, s.y, s.y, s.y), s.y)"),
    "dupshadow": ("sampler2DShadow", "vec4(textureLod(tx, vec3(s.z, s.z, s.z), s.z))"),
    "dup3d": ("sampler3D", "textureLod(tx, vec3(s.w), s.w)"),
}
for k, (ty, expr) in forms.items():
    for pressure in (0, 24):
        body = HDR + f"layout(set = 0, binding = 0) uniform {ty} tx;\n" + ubo(1, 24) + IO + "void main() {\n" + pre(24)
        body += "  vec4 t = vec4(0.0);\n  if (uv.x > 0.5) {\n"
        if pressure:
            body += inb(pressure)
        else:
            body += "    vec4 s = vec4(uv, uv.yx) + a;\n"
        body += f"    t = {expr};\n  }}\n  color = a + t;\n}}\n"
        add(f"f_{k}_{pressure}", "-s 1 -u", body)
    for pressure in (24, 32):
        e0 = expr.replace("s.", "s0.").replace("vec4(s0.", "vec4(s0.")
        body = HDR + f"layout(set = 0, binding = 0) uniform {ty} tx;\n" + ubo(1, pressure) + IO + "void main() {\n"
        body += "  vec4 s0 = vec4(uv, uv.yx);\n  vec4 e = " + e0 + ";\n" + pre(pressure)
        body += "  vec4 t = vec4(0.0);\n  if (uv.x > 0.5) {\n" + inb(pressure)
        body += f"    t = {expr};\n  }}\n  color = a + t + e;\n}}\n"
        add(f"g_{k}_{pressure}", "-s 1 -u", body)

# one handle used by two tex in different blocks: uniform one + non-uniform one (with spill), and a third after the merge
body = HDR + "layout(set = 0, binding = 0) uniform sampler2D tx;\n" + ubo(1, 28) + IO + "void main() {\n" + pre(28)
body += "  vec4 t = texture(tx, uv);\n  if (uv.x > 0.5) {\n" + inb(28) + "    t += textureLod(tx, s.xy, s.z);\n    if (uv.y > 0.25) t += texture(tx, s.zw + t.xy);\n  }\n  t += texture(tx, t.xy, 1.0);\n  color = a + t;\n}\n"
add("r_twoblocks", "-s 1 -u", body)

# sampler array with dynamic uniform index from UBO, used in non-uniform block (both LDC-offset and spill paths)
body = HDR + "layout(set = 0, binding = 0) uniform sampler2D ta[8];\n" + "layout(set = 0, binding = 1) uniform U { vec4 v[24]; int idx[4]; } u;\n" + IO + "void main() {\n" + pre(24)
body += "  vec4 t = texture(ta[u.idx[0]], uv);\n  if (uv.x > 0.5) {\n" + inb(24) + "    t += textureLod(ta[u.idx[1]], s.xy, s.w) + texture(ta[u.idx[2] & 7], s.zw, s.x);\n  }\n  color = a + t;\n}\n"
add("r_arrdyn_div", "-a 8 -u", body)

# nonuniform index (divergent handle from the start)
body = HDR + "layout(set = 0, binding = 0) uniform sampler2D ta[8];\n" + ubo(1, 24) + IO + "void main() {\n" + pre(24)
body += "  vec4 t = vec4(0.0);\n  if (uv.x > 0.5) {\n" + inb(24) + "    int i = int(s.x * 7.0) & 7;\n    t += texture(ta[nonuniformEXT(i)], s.xy) + textureLod(ta[3], s.zw, s.y);\n  }\n  color = a + t;\n}\n"
add("r_nonuniform", "-a 8 -u", body)

# sampler array past the cbuf-mapped part (global LD), dynamic push index, in divergent block with pressure
body = HDR + "layout(set = 0, binding = 0) uniform sampler2D t0;\nlayout(set = 0, binding = 1) uniform sampler2D big[16400];\n" + ubo(2, 24) + IO
body += "layout(push_constant) uniform PC { int i; int j; } pc;\nvoid main() {\n" + pre(24) + "  vec4 t = texture(big[pc.i], uv);\n  if (uv.x > 0.5) {\n" + inb(24) + "    t += textureLod(big[pc.j], s.xy, s.z) + texture(big[16399], s.zw) + texture(t0, s.yx);\n  }\n  color = a + t;\n}\n"
add("r_bigarr", "-s 1 -a 16400 -u -p 8", body)

# uniform branch nested in divergent branch, with a loop-carried texture and phi
body = HDR + "layout(set = 0, binding = 0) uniform sampler2D t0;\nlayout(set = 0, binding = 1) uniform sampler2DArray t1;\n" + "layout(set = 0, binding = 2) uniform U { vec4 v[24]; int flag; int cnt; } u;\n" + IO
body += "void main() {\n" + pre(24) + "  vec4 t = vec4(0.0);\n  if (uv.x > 0.5) {\n" + inb(24) + "    if (u.flag != 0) {\n      for (int i = 0; i < u.cnt; i++) { t = textureLod(t0, t.xy + s.xy, t.z) + t; s = s.yzwx; }\n    } else {\n      t = texture(t1, vec3(s.xy, s.z), s.w);\n    }\n  }\n  color = a + t;\n}\n"
add("r_unifnested", "-s 2 -u", body)

# discard + texture after it
body = HDR + "layout(set = 0, binding = 0) uniform sampler2D t0;\n" + ubo(1, 24) + IO + "void main() {\n" + pre(24)
body += "  if (uv.x > 0.9) discard;\n  vec4 t = vec4(0.0);\n  if (uv.x > 0.5) {\n" + inb(24) + "    if (s.x > 3.0) discard;\n    t = texture(t0, s.xy, s.z);\n  }\n  color = a + t;\n}\n"
add("r_discard", "-s 1 -u", body)

# function called in both arms
body = HDR + "layout(set = 0, binding = 0) uniform sampler2D t0;\nlayout(set = 0, binding = 1) uniform sampler2D t1;\n" + ubo(2, 24) + IO
body += "vec4 f(sampler2D s_, vec4 s) { return textureLod(s_, s.xy, s.z) * texture(s_, s.zw); }\nvoid main() {\n" + pre(24) + "  vec4 t;\n  if (uv.x > 0.5) {\n" + inb(24) + "    t = f(t0, s) + f(t1, s.wzyx);\n  } else {\n" + inb(20, "q") + "    t = f(t1, q);\n  }\n  color = a + t;\n}\n"
add("r_func", "-s 2 -u", body)

# very large shader: 4 samplers, 64 tex in divergent block, uniform pressure 32
body = HDR + "".join(f"layout(set = 0, binding = {i}) uniform sampler2D t{i};\n" for i in range(4)) + ubo(4, 32) + IO
body += "void main() {\n" + pre(32) + "  vec4 t = vec4(0.0);\n  if (uv.x > 0.5) {\n" + inb(32)
for i in range(64):
    body += f"    t += textureLod(t{i % 4}, s.xy * {i + 1}.0 + t.zw, s.z + t.x);\n    s = s.yzwx * u.v[{i % 32}] + t;\n"
body += "  }\n  color = a + t;\n}\n"
add("r_large", "-s 4 -u", body)

# huge GPR pressure: many live vec4 across the textures in divergent block
body = HDR + "layout(set = 0, binding = 0) uniform sampler2D t0;\nlayout(set = 0, binding = 1) uniform sampler2DArray t1;\n" + ubo(2, 24) + IO
body += "void main() {\n" + pre(24) + "  vec4 t = vec4(0.0);\n  if (uv.x > 0.5) {\n" + inb(24)
body += "    vec4 r[24];\n    for (int k = 0; k < 24; k++) r[k] = textureLod(t0, s.xy * float(k + 1), s.z);\n"
body += "    for (int k = 0; k < 24; k++) t += texture(t1, vec3(r[23 - k].xy, r[k].z), r[k].w) * r[(k * 7) % 24];\n"
body += "  }\n  color = a + t;\n}\n"
add("r_gprpressure", "-s 2 -u", body)

# sparse + min lod clamp (if glslang accepts)
body = "#version 450\n#extension GL_ARB_sparse_texture2 : enable\n#extension GL_ARB_sparse_texture_clamp : enable\n"
body += "layout(set = 0, binding = 0) uniform sampler2D t0;\nlayout(set = 0, binding = 1) uniform sampler2DArray t1;\n" + ubo(2, 24) + IO
body += "void main() {\n" + pre(24) + "  vec4 t = vec4(0.0);\n  if (uv.x > 0.5) {\n" + inb(24)
body += "    vec4 r; int c = sparseTextureClampARB(t0, s.xy, s.z, r, s.w);\n    vec4 q = textureClampARB(t1, s.xyz, s.w, s.x);\n    t = r + q + vec4(sparseTexelsResidentARB(c));\n"
body += "  }\n  color = a + t;\n}\n"
add("r_sparseclamp", "-s 2 -u", body)

# min lod clamp, uniform block, no pressure
body = "#version 450\n#extension GL_ARB_sparse_texture_clamp : enable\n"
body += "layout(set = 0, binding = 0) uniform sampler2D t0;\nlayout(set = 0, binding = 1) uniform sampler2DArray t1;\n" + IO
body += "void main() {\n  color = textureClampARB(t0, uv, 0.5, 1.0) + textureClampARB(t1, vec3(uv, 2.0), 0.25);\n}\n"
add("r_clamp_unif", "-s 2", body)

# vertex shaders with textures
for nm, n in [("v_lod", 0), ("v_lod_p24", 24), ("v_lod_p32", 32)]:
    vs = "#version 450\nlayout(set = 0, binding = 0) uniform sampler2D t0;\nlayout(set = 0, binding = 1) uniform sampler2DArray t1;\n" + ubo(2, max(n, 1))
    vs += "layout(location = 0) out vec2 uv;\nvoid main() {\n  vec2 p = vec2((gl_VertexIndex << 1) & 2, gl_VertexIndex & 2);\n"
    if n:
        vs += f"  vec4 a = vec4(0.0);\n  for (int k = 0; k < {n}; k++) a += u.v[k] * float(k + 1);\n"
    else:
        vs += "  vec4 a = u.v[0];\n"
    vs += "  vec4 t = textureLod(t0, p, 0.0);\n  if (gl_VertexIndex > 1) {\n    vec4 s = vec4(p.x);\n"
    if n:
        vs += f"    for (int k = 0; k < {n}; k++) s = s * u.v[k] + p.xyxy;\n"
    vs += "    t += textureLod(t1, vec3(s.xy, s.z), s.w) + textureLod(t0, s.zw, 1.0);\n  }\n  uv = p + t.xy + a.zw;\n  gl_Position = vec4(p * 2.0 - 1.0, t.z, 1.0);\n}\n"
    fs = HDR + IO + "void main() { color = vec4(uv, 0.0, 1.0); }\n"
    add(nm, "-s 2 -u", fs, vsrc=vs)

with open(os.path.join(W, "cases.txt"), "w") as f:
    for c in cases:
        f.write("|".join(c) + "\n")
print(len(cases))
