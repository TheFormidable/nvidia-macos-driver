#!/usr/bin/env python3
# Holds nvmtl_compute_variant (plugin/nvmtl_vk.c) to the shape tools/test_cvar.c models: the threadgroup-size variant is
# compiled with g_var_lock released unless NVMTL_CVAR_PARALLEL=0, the table is looked at again under the lock before the
# result is put in, and a pipeline another thread beat is destroyed. Then builds test_cvar.c against the real header (it
# must pass) and against one whose insert does not look again (it must fail: two threads keep one size twice).
import pathlib, re, subprocess, sys, tempfile

root = pathlib.Path(__file__).resolve().parent.parent
vk = (root / 'plugin' / 'nvmtl_vk.c').read_text()
vkh = (root / 'plugin' / 'nvmtl_vk.h').read_text()
test_c = root / 'tools' / 'test_cvar.c'
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

f = body(vk, 'static VkPipeline nvmtl_compute_variant(nvk_pipeline *p, const uint32_t l[3])')
arm('nvmtl_compute_variant is found', bool(f))
unlock = f.find('if (parallel) pthread_mutex_unlock(&g_var_lock);')
create = f.find('nvmtl_create_compute_pipeline(')
relock = f.find('if (parallel) pthread_mutex_lock(&g_var_lock);')
insert = f.find('nvmtl_cvar_insert(')
arm('the lookup is made under the lock', 0 <= f.find('pthread_mutex_lock(&g_var_lock);') < f.find('nvmtl_cvar_find(p->var, p->nvar, l)') < unlock)
arm('the lock is released before the compile and taken again right after it', 0 <= unlock < create < relock < insert)
arm('the insert looks again exactly when the compile ran outside the lock', 'nvmtl_cvar_insert(&p->var, &p->nvar, &p->capvar, l, (void *)found, parallel,' in f)
arm('a pipeline another thread beat is destroyed and that thread\'s is returned',
    re.search(r'if \(put == NVMTL_CVAR_TAKEN\) \{\s*pvkDestroyPipeline\(g_dev, found, NULL\); found = \(VkPipeline\)winner;', f) is not None)
fail_at = f.find('if (r != VK_SUCCESS) {')
arm('a compile that failed outside the lock still takes a pipeline another thread kept for the size; under the lock it skips, as before',
    0 <= relock < fail_at < f.find('found = parallel ? (VkPipeline)nvmtl_cvar_find(p->var, p->nvar, l) : VK_NULL_HANDLE;')
    < f.find('if (!found) nvlog("vk: compute variant %ux%ux%u -> %d') < insert)
arm('the table keeps its growth (NVMTL_NVAR first, up to NVMTL_NVAR_MAX)', 'NVMTL_NVAR, NVMTL_NVAR_MAX,' in f)
arm('nothing but the variant table and its destroy takes g_var_lock',
    [m.start() for m in re.finditer(r'pthread_mutex_lock\(&g_var_lock\)', vk)] and
    all(vk.rfind('static VkPipeline nvmtl_compute_variant', 0, m.start()) > vk.rfind('\n}', 0, m.start()) or
        vk.rfind('void nvmtl_vk_pipeline_destroy(nvk_pipeline *p)', 0, m.start()) > vk.rfind('\n}', 0, m.start())
        for m in re.finditer(r'pthread_mutex_lock\(&g_var_lock\)', vk)))
arm('NVMTL_CVAR_PARALLEL is read once, on unless it is 0',
    re.search(r'static int on = -1;\s*if \(on < 0\) \{ const char \*e = getenv\("NVMTL_CVAR_PARALLEL"\); on = !\(e && e\[0\] == \'0\'\);', vk) is not None)
entry = re.search(r'struct nvmtl_cvar \{ uint32_t l\[3\]; void \*pipe; \}', vkh)
arm('the test models the table entry nvmtl_vk.h declares', entry is not None and 'struct nvmtl_cvar { uint32_t l[3]; void *pipe; };' in test_c.read_text())

STUB_FROM = '    if (recheck) {'
def build_run(header_dir, expect_ok, what):
    with tempfile.TemporaryDirectory() as bd:
        exe = pathlib.Path(bd) / 'tcv'
        subprocess.run(['cc', '-std=c11', '-O1', '-pthread', '-Wall', '-Wextra', '-I', str(header_dir), str(test_c), '-o', str(exe)], check=True)
        rc = subprocess.run([str(exe)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        arm(what, (rc.returncode == 0) == expect_ok)
        if (rc.returncode == 0) != expect_ok: print(rc.stdout[-1500:])

build_run(root / 'plugin', True, 'test_cvar.c passes against plugin/nvmtl_cvar.h')
hdr = (root / 'plugin' / 'nvmtl_cvar.h').read_text()
arm('the header has the recheck the negative control removes', STUB_FROM in hdr)
with tempfile.TemporaryDirectory() as d:
    (pathlib.Path(d) / 'nvmtl_cvar.h').write_text(hdr.replace(STUB_FROM, '    if (0 && recheck) {'))
    build_run(d, False, 'without the second look test_cvar.c FAILS (a size kept twice, a compiled pipeline lost)')

print('FAILED %d' % fails if fails else 'ok')
sys.exit(1 if fails else 0)
