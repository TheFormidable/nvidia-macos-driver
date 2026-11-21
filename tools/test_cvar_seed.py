#!/usr/bin/env python3
# Holds a compute kernel's creation-time pipeline (nvmtl_vk_compute_pipeline_create_g_lifetime_body, plugin/nvmtl_vk.c)
# to the specialization nvmtl_compute_variant gives a dispatch of the same size, so the pipeline kept as that size's
# variant is the one the dispatch would have compiled: the same map entries, the same data, the same create path. And
# the table is written only after the last step that can fail, since nvmtl_failed_pipeline does not free it.
import pathlib, re, sys

root = pathlib.Path(__file__).resolve().parent.parent
vk = (root / 'plugin' / 'nvmtl_vk.c').read_text()
hdr = (root / 'plugin' / 'nvmtl_cvar_seed.h').read_text()
fails = 0
def arm(what, ok):
    global fails
    print(('ok    ' if ok else 'FAIL  ') + what)
    if not ok: fails += 1

def body(src, head):
    i = src.find(head)
    if i < 0: return ''
    j = src.find('\n{', i); depth = 0
    for k in range(j + 1, len(src)):
        if src[k] == '{': depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0: return src[j + 1:k + 1]
    return ''

create = body(vk, 'static int nvmtl_vk_compute_pipeline_create_g_lifetime_body(const void *cs, size_t n, uint32_t flags, nvk_pipeline *out)\n{')
variant = body(vk, 'static VkPipeline nvmtl_compute_variant(nvk_pipeline *p, const uint32_t l[3])')
arm('both functions are found', bool(create) and bool(variant))
ME = 'VkSpecializationMapEntry me[4] = { { 0, 0, 4 }, { 1, 4, 4 }, { 2, 8, 4 }, { 3, 12, 4 } };'
SI = 'VkSpecializationInfo si = { 4, me, 16, sd };'
arm('the same four map entries at creation and at dispatch', ME in create and ME in variant)
arm('the same specialization info at creation and at dispatch', SI in create and SI in variant)
var_sd = 'uint32_t sd[4] = { l[0], l[1], l[2], (p->lin && (l[0] > wl[0] || l[1] > wl[1] || l[2] > wl[2])) ? 1u : 0u };'
arm('the dispatch computes its data as nvmtl_cvar_spec_data does',
    var_sd in variant and 'const uint32_t *wl = g_lim.maxComputeWorkGroupSize;' in variant and
    'sd[0] = l[0]; sd[1] = l[1]; sd[2] = l[2];' in hdr and
    'sd[3] = (lin && (l[0] > w[0] || l[1] > w[1] || l[2] > w[2])) ? 1u : 0u;' in hdr)
arm('creation takes the kernel\'s lin flag and the same per-dimension limits',
    'const uint32_t *wl = g_lim.maxComputeWorkGroupSize;' in create and 'nvmtl_cvar_spec_data(d, out->lin, wl, sd);' in create
    and create.find('out->lin = nvmtl_spirv_has_specid(cs, n / 4, 3);') < create.find('nvmtl_cvar_spec_data('))
arm('the size is read from the module the shader module is made from, and must fit the device without linearizing',
    'nvmtl_cvar_seed_size(cs, n / 4, d)' in create and 'nvmtl_cvar_size_fits(d, wl, g_lim.maxComputeWorkGroupInvocations)' in create
    and create.find('nvmtl_cvar_seed_size(') > create.find('cs = data.bytes; n = data.length;'))
arm('the unseeded create passes no specialization, as before', '"main", seed ? &si : NULL }' in create)
arm('the dispatch compiles with the same shader module entry point and create path',
    '(VkShaderModule)p->mod, "main", &si }' in variant and 'nvmtl_create_compute_pipeline(' in variant
    and 'nvmtl_opaque_CreateComputePipelines(' in create)
vkck = create.find('VKCK(nvmtl_opaque_CreateComputePipelines(')
seedw = create.find('out->var = calloc(NVMTL_NVAR, sizeof *out->var)')
arm('the table is written after the create succeeded, and nothing that can fail follows it',
    0 <= vkck < seedw and 'VKCK' not in create[seedw:] and 'return -1' not in create[seedw:])
arm('the kept entry is the default size with the creation pipeline',
    'memcpy(out->var[0].l, d, sizeof out->var[0].l); out->var[0].pipe = out->pipe; out->nvar = 1;' in create)
destroy = body(vk, 'void nvmtl_vk_pipeline_destroy(nvk_pipeline *p)')
arm('destroy destroys var[0] and p->pipe once (they are the same handle)',
    'nvmtl_destroy_pipe_once(seen, &nseen, 64, (VkPipeline)p->var[i].pipe)' in destroy and
    destroy.find('(VkPipeline)p->var[i].pipe)') < destroy.find('nvmtl_destroy_pipe_once(seen, &nseen, 64, (VkPipeline)p->pipe)'))
arm('NVMTL_CVAR_SEED is read once, on unless it is 0',
    re.search(r'static int on = -1;\s*if \(on < 0\) \{ const char \*e = getenv\("NVMTL_CVAR_SEED"\); on = !\(e && e\[0\] == \'0\'\);', vk) is not None
    and 'const int seed = nvmtl_cvar_seed_on() &&' in create)

print('FAILED %d' % fails if fails else 'ok')
sys.exit(1 if fails else 0)
