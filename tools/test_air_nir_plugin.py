#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Holds the plugin's AIR-to-NIR road for compute and render pipelines (plugin/NVMTLAirNir.m, docs/AIR-TO-NIR.md phases 2
# and 3) to its contract, on the source: the switch is NVMTL_AIR_NIR read once and only through nvmtl_air_nir_switch; NVK's exports are
# looked up only when the plugin opened NVK directly, all five or none; newComputePipelineState asks the road first and
# makes the SPIR-V pipeline exactly as before whenever the road gave none, with the reason; the cache key carries the
# AIR, the entry, the constants, the threadgroup size and lengths, the flags and the identity of NVK and LLVM; a variant
# of a kernel on the road is a copy of its NIR; the kernels whose AIR is the translator's rewrite stay on SPIR-V; the
# frontend's text-IR parse changes nothing between setjmp and longjmp (it once freed its buffer twice on a refusal); a
# render pipeline asks the road first and takes the SPIR-V road unchanged otherwise, never with a stage the SPIR-V road
# rewrites, its stages translated with the device's robustness, ALIGN4 and their linked varyings, both roads linking
# by one remap, a fragment stage's resources in set 1.
# Each check is also run on a copy of the source with what it guards taken out, and must fail there. Then builds and runs
# tools/test_air_nir_plugin.c (the pure helpers, plugin/nvmtl_air_nir.h).
import pathlib, re, subprocess, sys, tempfile

root = pathlib.Path(__file__).resolve().parent.parent
rd = lambda p: (root / p).read_text()
src = {p: rd(p) for p in ("plugin/NVMTLAirNir.m", "plugin/nvmtl_vk.c", "plugin/NVMTLDevice.m", "plugin/NVMTLObjects.m",
                          "plugin/NVMTLLibraryLoad.m", "nvk/air/nvk_air_to_nir.c")}
fails = 0
def arm(what, ok):
    global fails
    print(('ok    ' if ok else 'FAIL  ') + what)
    if not ok: fails += 1

def body(text, head):
    i = text.find(head)
    if i < 0: return ''
    j = text.find('{', i + len(head) - 1 if head.endswith('{') else i); depth = 0
    for k in range(j, len(text)):
        if text[k] == '{': depth += 1
        elif text[k] == '}':
            depth -= 1
            if depth == 0: return text[j:k + 1]
    return ''

def strip_comments(t):
    t = re.sub(r'/\*.*?\*/', '', t, flags=re.S)
    return re.sub(r'//[^\n]*', '', t)

# --- the checks, each over a dict of sources ------------------------------------------------------------------------
def switch_once(s):
    an = strip_comments(s["plugin/NVMTLAirNir.m"])
    on = body(an, 'static int nvmtl_air_nir_on(void)')
    reads = sum(len(re.findall(r'getenv\("NVMTL_AIR_NIR"\)', strip_comments(t))) for t in s.values())
    return ('dispatch_once' in on and 'getenv("NVMTL_AIR_NIR")' in on and 'nvmtl_air_nir_switch(e, me)' in on and reads == 1)

def exports_direct(s):
    vk = strip_comments(s["plugin/nvmtl_vk.c"])
    direct, loader = vk.find('nvlog("vk: driver opened directly'), vk.find('nvlog("vk: fell back to the loader')
    at = vk.find('dlsym(g_lib, "nvmtl_air_to_nir_v1")')
    five = all(f'dlsym(g_lib, "{n}")' in vk for n in ("nvmtl_air_to_nir_v1", "nvmtl_air_nir_free_v1", "nvmtl_air_nir_clone_v1",
                                                      "nvmtl_air_nir_serialize_v1", "nvmtl_air_nir_deserialize_v1"))
    none = re.search(r'if \(!pnvk_air_to_nir \|\| !pnvk_air_nir_free \|\| !pnvk_air_nir_clone \|\| !pnvk_air_nir_serialize \|\| '
                     r'!pnvk_air_nir_deserialize\)\s*pnvk_air_to_nir = NULL;', vk) is not None
    return 0 <= direct < at < loader and five and none and 'nvmtl_vk_vendor() == 0x10de' in vk[direct:at]

def fallback(s):
    dv = strip_comments(s["plugin/NVMTLDevice.m"])
    m = body(dv, '- (id<MTLComputePipelineState>)newComputePipelineStateWithFunction:(id<MTLFunction>)fn error:(NSError **)err {')
    ask, spv = m.find('nvmtl_air_nir_wanted('), m.find('nvmtl_vk_compute_pipeline_create(f->_spirv.bytes, f->_spirv.length, &ps->_p)')
    road = m.find('? nvmtl_air_nir_pipeline(f, nil, &nirWhy) : nil;')
    els = m.find('} else {', road)
    note = m.find('nvmtl_air_nir_note(f, 0, NO, nirWhy,', spv)
    return 0 <= ask < road < els < spv < note

def reasons(s):
    an = strip_comments(s["plugin/NVMTLAirNir.m"])
    mk = body(an, 'static NVMTLComputePipelineState *nvmtl_air_nir_make(')
    nils = [m.start() for m in re.finditer(r'return nil;', mk)]
    # every way out without a pipeline says why: the shader lookup sets *why itself, the others just before
    ok = bool(nils)
    for p in nils:
        before = mk[max(0, p - 250):p]
        ok &= ('*why = ' in before) or before.rstrip().endswith('&memoKey, &hit)) ') or 'nvmtl_air_nir_shader(' in before[-200:]
    sh = body(an, 'static int nvmtl_air_nir_shader(')
    rets = [m.start() for m in re.finditer(r'return -1;', sh)]
    ok &= bool(rets) and all('*why = ' in sh[max(0, p - 300):p] for p in rets)
    return ok

def cache_key(s):
    an = strip_comments(s["plugin/NVMTLAirNir.m"])
    sh = body(an, 'static int nvmtl_air_nir_shader(')
    keys = all(f'@"{k}"' in sh for k in ("abi", "entry", "constants", "threadgroup", "size", "flags", "ids"))
    path = 'nvmtl_spvcache_path_air(ad, tail, @"nir1")' in sh and 'nvmtl_air_state(f->_air)' in sh
    ids = 'nvmtl_air_nir_image_uuid((const void *)pnvk_air_to_nir' in an and 'st.st_mtimespec' in an
    stored = re.search(r'if \(st == NVMTL_AIR_NIR_UNSUPPORTED \|\| st == NVMTL_AIR_NIR_BAD_AIR\) nvmtl_air_nir_store\(', sh) is not None
    return keys and path and ids and stored

def variants(s):
    vk = strip_comments(s["plugin/nvmtl_vk.c"])
    f = body(vk, 'static VkPipeline nvmtl_compute_variant(nvk_pipeline *p, const uint32_t l[3])')
    clone = 'pnvk_air_nir_clone(p->air_nir, l)' in f and 'if (ni.nir) pnvk_air_nir_free(ni.nir);' in f
    one = len(re.findall(r'nvmtl_create_compute_pipeline\(', f)) == 1
    early = 'if (!p->mod && !p->air_nir) return (VkPipeline)p->pipe;' in f
    destroy = body(vk, 'void nvmtl_vk_pipeline_destroy(nvk_pipeline *p)')
    failed = body(vk, 'static void nvmtl_failed_pipeline(nvk_pipeline *pipeline)')
    return clone and one and early and 'nvmtl_air_nir_release(p);' in destroy and 'nvmtl_air_nir_release(pipeline);' in failed

def rewrites_stay(s):
    dv, ll = strip_comments(s["plugin/NVMTLDevice.m"]), strip_comments(s["plugin/NVMTLLibraryLoad.m"])
    an = strip_comments(s["plugin/NVMTLAirNir.m"])
    return (dv.count('_spirvOnly = YES;') == 3 and 'sf->_spirvOnly = YES;' in ll and
            'if (f->_spirvOnly) {' in body(an, 'BOOL nvmtl_air_nir_wanted('))

def text_ir_setjmp(s):
    fe = strip_comments(s["nvk/air/nvk_air_to_nir.c"])
    f = body(fe, 'nvmtl_air_to_nir_v1(void *device, const struct nvmtl_air_nir_request *req, struct nvmtl_air_nir_result *res)\n{')
    a, b = f.find('if (setjmp(t->fail) == 0) {'), f.find('L.LLVMContextDispose(ctx);')
    region = f[a:b]
    return a >= 0 and b > a and not re.search(r'\bbuf\s*=[^=]', region) and 'if (buf && !text)' in region

def size_twin(s):
    ob = strip_comments(s["plugin/NVMTLObjects.m"])
    return ob.count('selected = nvmtl_air_nir_for_size(selected, l);') == 3

# --- render pipelines (round 4) ----------------------------------------------------------------------------------------
def render_fallback(s):
    dv = strip_comments(s["plugin/NVMTLDevice.m"])
    m = body(dv, '- (id<MTLRenderPipelineState>)newRenderPipelineStateWithDescriptor:(MTLRenderPipelineDescriptor *)d error:(NSError **)err {')
    ask = m.find('nvmtl_air_nir_render_wanted(vf, ff, noFrag, a8mask, amplified, samplePayload, &nirWhy) &&')
    make = m.find('!nvmtl_air_nir_render_pipeline(vf, ff, &rt, vin.nattr ? &vin : NULL, rsc, vdesc, &ps->_p, &nirWhy)')
    note = m.find('nvmtl_air_nir_render_note(vf, noFrag ? nil : ff, onNir, onNir ? nil : nirWhy,')
    ret = m.find('return ps;', note)
    link, spv = m.find('NSData *flk = nvmtl_varying_link(vf, ffx);'), m.find('prc = nvmtl_vk_pipeline_create_rt(')
    # after the multiview variants (they set rt.view_mask) and before the SPIR-V road's link and pipeline
    mv = m.find('rt.view_mask = mask;')
    return 0 <= mv < ask < make < note < ret < link < spv and 'if (onNir) {' in m[note:ret]

RENDER_REFUSALS = ('if (noFrag || !ff)', 'if (vf->_spirvOnly || ff->_spirvOnly)', 'if (getenv("NVMTL_BDA_PROBE_FILE"))',
                   'if (a8mask)', 'if (amplified)', 'if (nvmtl_spirv_tess_info(vf->_spirv.bytes, vf->_spirv.length, &tpt, &tcps))',
                   'if (nvmtl_gl_uniform_index(vf) < 32 || nvmtl_gl_uniform_index(ff) < 32)',
                   'if (nvmtl_spirv_fetch_mask(ff->_spirv.bytes, ff->_spirv.length))',
                   'if (nvmtl_spirv_stencil_emu(ff->_spirv.bytes, ff->_spirv.length))',
                   'if (samplePayload && samplePayload != [NSNull null])', 'nvmtl_app_profile_edit_module(g_app_profile')
def render_rewrites(s):
    an = strip_comments(s["plugin/NVMTLAirNir.m"])
    w = body(an, 'BOOL nvmtl_air_nir_render_wanted(')
    return all(r in w for r in RENDER_REFUSALS) and w.count('return NO;') >= len(RENDER_REFUSALS)

def render_requests(s):
    an = strip_comments(s["plugin/NVMTLAirNir.m"])
    fl = body(an, 'static uint32_t nvmtl_air_nir_flags(uint32_t extra, BOOL render)')
    rp = body(an, 'int nvmtl_air_nir_render_pipeline(')
    sh = body(an, 'static int nvmtl_air_nir_shader(')
    return ('(g_compute_unchecked && !render ? NVMTL_AIR_NIR_UNCHECKED : 0)' in fl and '(render ? NVMTL_AIR_NIR_VARYINGS : 0)' in fl
            and rp.count('NVMTL_AIR_NIR_ALIGN4') == 2 and 'nvmtl_air_nir_flags(extra, varyings != nil)' in sh
            and 'k[@"varyings"] = varyings;' in sh and '@"vertex_descriptor": vdesc' in rp and '@{ @"stage": @"fragment" }' in rp)

def render_link(s):
    an, dv = strip_comments(s["plugin/NVMTLAirNir.m"]), strip_comments(s["plugin/NVMTLDevice.m"])
    rp = body(an, 'int nvmtl_air_nir_render_pipeline(')
    lk = body(dv, 'static NSData *nvmtl_varying_link(NVMTLFunction *vf, NVMTLFunction *ff) {')
    return ('nvmtl_varying_remap(vf, ff, remap, have, NULL)' in rp and 'link > 0 ? remap : NULL' in rp
            and 'nvmtl_varying_remap(vf, ff, remap, have, &unmatched)' in lk)

def fragment_set(s):
    fe = strip_comments(s["nvk/air/nvk_air_to_nir.c"])
    return ('t->set = t->stage == NVMTL_AIR_NIR_STAGE_FRAGMENT ? NVMTL_AIR_NIR_FRAGMENT_SET : 0;' in fe and
            fe.count('.desc_set = t->set') == 1 and fe.count('descriptor_set = t->set') == 2 and
            not re.search(r'desc(riptor)?_set = 0', fe))

CHECKS = [
    ("NVMTL_AIR_NIR is read once, in one place, through nvmtl_air_nir_switch", switch_once,
     ("plugin/NVMTLAirNir.m", 'BOOL nvmtl_air_nir_wanted(NVMTLFunction *f, BOOL replaced, NSString **why)\n{',
      'BOOL nvmtl_air_nir_wanted(NVMTLFunction *f, BOOL replaced, NSString **why)\n{\n    if (getenv("NVMTL_AIR_NIR")) return NO;')),
    ("NVK's five exports are looked up only when NVK was opened directly, on NVIDIA, all or none", exports_direct,
     ("plugin/nvmtl_vk.c", '                    pnvk_air_to_nir = NULL;   // all five or none', '                    ;')),
    ("newComputePipelineState asks the road first and makes the SPIR-V pipeline whenever it gave none, with the reason", fallback,
     ("plugin/NVMTLDevice.m", 'nvmtl_air_nir_note(f, 0, NO, nirWhy,', 'nvmtl_air_nir_note(f, 0, NO, nil,')),
    ("every way the road gives no pipeline says why", reasons,
     ("plugin/NVMTLAirNir.m", '            *why = @"its constexpr samplers would not sit at the slots the translator\'s reflection binds";\n', '')),
    ("the shader cache key carries AIR, entry, constants, size, lengths, flags and NVK's and LLVM's identity", cache_key,
     ("plugin/NVMTLAirNir.m", '@"threadgroup": tgLen ?: @{},', '')),
    ("a threadgroup-size variant on the road is a copy of its NIR, and destroy frees the NIR", variants,
     ("plugin/nvmtl_vk.c", 'if (ni.nir) pnvk_air_nir_free(ni.nir);', '')),
    ("kernels whose AIR is the translator's rewrite (mesh, object, tile, stage-in) stay on SPIR-V", rewrites_stay,
     ("plugin/NVMTLLibraryLoad.m", '    sf->_spirvOnly = YES;', '')),
    ("a dispatch of a size the road does not run takes the kernel's SPIR-V twin (three dispatch paths)", size_twin,
     ("plugin/NVMTLObjects.m", '    selected = nvmtl_air_nir_for_size(selected, l);\n    if (nvmtl_vk_tg_reject(l, NULL,', '    if (nvmtl_vk_tg_reject(l, NULL,')),
    ("the frontend's text-IR parse changes nothing between setjmp and longjmp", text_ir_setjmp,
     ("nvk/air/nvk_air_to_nir.c", '      if (buf && text) {', '      if (buf && text) {\n         buf = NULL;')),
    ("newRenderPipelineState asks the road first, after the multiview variants, and goes on to the SPIR-V road unchanged", render_fallback,
     ("plugin/NVMTLDevice.m", '          return ps;\n      } }', '      } }')),
    ("every pipeline whose stages the SPIR-V road rewrites stays on it (A8, multiview, tessellation, GL, fetch, stencil, ...)",
     render_rewrites, ("plugin/NVMTLAirNir.m", '    if (a8mask) {', '    if (0) {')),
    ("render stages are translated with the device's robustness, ALIGN4, their varyings, and keyed by them", render_requests,
     ("plugin/NVMTLAirNir.m", '(g_compute_unchecked && !render ? NVMTL_AIR_NIR_UNCHECKED : 0)', '(g_compute_unchecked ? NVMTL_AIR_NIR_UNCHECKED : 0)')),
    ("both roads link varyings by the one remap (nvmtl_varying_remap)", render_link,
     ("plugin/NVMTLAirNir.m", 'link > 0 ? remap : NULL', 'NULL')),
    ("the frontend puts a fragment stage's resources in set 1, a vertex stage's and a kernel's in set 0", fragment_set,
     ("nvk/air/nvk_air_to_nir.c", '   x->var->data.descriptor_set = t->set;', '   x->var->data.descriptor_set = 0;')),
]
for what, check, (path, old, new) in CHECKS:
    arm(what, check(src))
    mut = dict(src)
    if old not in mut[path]:
        arm(f"(negative control of '{what}': the text it removes is not in {path})", False); continue
    mut[path] = mut[path].replace(old, new, 1)
    arm(f"  ... and the check fails without it", not check(mut))

arm("plugin/nvmtl_air_nir_abi.h is nvk/air/nvmtl_air_nir_abi.h byte for byte",
    (root / "plugin/nvmtl_air_nir_abi.h").read_bytes() == (root / "nvk/air/nvmtl_air_nir_abi.h").read_bytes())
arm("NVMTLDevice.m imports NVMTLAirNir.m after nvmtl_vk.c and NVMTLLibraryLoad.m, whose statics it uses",
    0 <= src["plugin/NVMTLDevice.m"].find('#import "NVMTLLibraryLoad.m"') < src["plugin/NVMTLDevice.m"].find('#include "nvmtl_vk.c"')
    < src["plugin/NVMTLDevice.m"].find('#import "NVMTLAirNir.m"'))

with tempfile.TemporaryDirectory() as d:
    exe = pathlib.Path(d) / 't'
    b = subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-I', str(root / 'plugin'),
                        str(root / 'tools/test_air_nir_plugin.c'), '-o', str(exe)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    arm('tools/test_air_nir_plugin.c builds without a warning', b.returncode == 0)
    if b.returncode: print(b.stdout[-1500:])
    else:
        r = subprocess.run([str(exe)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        arm('tools/test_air_nir_plugin.c passes', r.returncode == 0)
        if r.returncode: print(r.stdout[-1500:])

print('FAILED %d' % fails if fails else 'ok')
sys.exit(1 if fails else 0)
