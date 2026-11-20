#!/usr/bin/env python3
# The wiring of the texture recycle cache (plugin/nvmtl_texrc.h; its rules are proved by tools/test_texrc.c): only the
# ordinary colour textures of -[NVMTLDevice newTextureWithDescriptor:] are made recyclable, a dead image is parked only
# where the image grace releases it, the take happens before any image or memory is made, the cache is given back
# before anything else when VRAM runs short, parked memory stays counted until it is freed, and NVMTL_TEX_RECYCLE=0
# plus WindowServer reach the switch.
import pathlib, re, sys

root = pathlib.Path(__file__).resolve().parent.parent
vk = (root / 'plugin' / 'nvmtl_vk.c').read_text()
dev = (root / 'plugin' / 'NVMTLDevice.m').read_text()
hdr = (root / 'plugin' / 'nvmtl_vk.h').read_text()
fails = 0

def arm(what, ok):
    global fails
    print(('ok    ' if ok else 'FAIL  ') + what)
    if not ok:
        fails += 1

def body(src, head):
    i = src.find(head)
    if i < 0:
        return ''
    j = src.find('\n{', i)
    depth, k = 0, j + 1
    while k < len(src):
        if src[k] == '{': depth += 1
        elif src[k] == '}':
            depth -= 1
            if depth == 0: return src[j:k + 1]
        k += 1
    return ''

arm('nvk_image carries the creation key and the image and memory it made', 'nvmtl_texrc_key rck; void *rc_img, *rc_mem;' in hdr)
arm('exactly one caller asks for a recyclable texture, and it is the general colour path of newTextureWithDescriptor',
    len(re.findall(r'nvmtl_vk_image_create_typed_recyclable\(', dev)) == 1
    and re.search(r'if \(nvmtl_vk_image_create_typed_recyclable\(\(uint32_t\)d\.width, \(uint32_t\)d\.height, vkf, bpp, a8, \(uint32_t\)d\.mipmapLevelCount, wantStex', dev) is not None)
others = [m for m in re.finditer(r'nvmtl_vk_image_create_typed_recyclable\(', vk)]
arm('nothing else in the Vulkan side asks for one', len(others) == 1)   # its own definition

b = body(vk, 'static int nvmtl_vk_image_create_typed_lifetime_body(uint32_t w, uint32_t h, uint32_t vkfmt, uint32_t bpp, int a8, uint32_t mips, int storage, uint32_t mtl_type, uint32_t layers, nvk_image *out)\n{')
arm('the creation reads and clears the request before the device can make images of its own',
    b.find('const int recyclable = t_texrc_create;') >= 0 and b.find('t_texrc_create = 0;') < b.find('nvmtl_vk_init()'))
take, mk, mut = b.find('nvmtl_texrc_reuse(&rck, out)'), b.find('nvmtl_opaque_CreateImage'), b.find('nvmtl_vk_image_mutable(&ici, out);')
arm('the key is taken after the create info is final and the take comes before any image is created', 0 <= mut < b.find('nvmtl_texrc_key_of(') < take < mk)
arm('the storage-twin registration and the allocation happen only on the fresh path',
    take < b.find('nvmtl_storage_view_add') and take < b.find('nvmtl_alloc_opaque_image_mem') and take < b.find('image_pool_bind'))
arm('only an image with its own VRAM is marked recyclable',
    'if (recyclable && !out->pool && !out->placed && !out->sysmem && out->mem) { out->rck = rck; out->rc_img = out->img; out->rc_mem = out->mem; }' in b)
arm('the view is made the same way for a taken image', b.find('nvmtl_opaque_CreateImageView(g_dev, &vci, NULL, &out->view)') > b.find('out->rc_mem = out->mem'))

reap = body(vk, 'static void nvmtl_image_grace_reap(void)\n{')
arm('the grace reap lets its destroy park', 't_texrc_may_park = !nvmtl_cl_forced(&e->mark); nvmtl_vk_image_destroy_now(&e->img);' in reap)
arm('the grace reap ages the cache out', 'nvmtl_texrc_tick();' in reap)
d = body(vk, 'void nvmtl_vk_image_destroy(nvk_image *i)\n{')
arm('a death nothing can read may park, and only after the grace said so (not when its entry could not be made)',
    re.search(r'free\(e\);[^\n]*\n\s*t_texrc_may_park = 1;\n\s*\}', d) is not None and d.count('t_texrc_may_park') == 1)
dn = body(vk, 'static void nvmtl_vk_image_destroy_now(nvk_image *i)\n{')
arm('the destroy reads and clears the permission first, so nothing it releases on the way inherits it',
    dn.find('const int may_park = t_texrc_may_park;') >= 0 and dn.find('t_texrc_may_park = 0;') < dn.find('nvmtl_vk_image_upload_drain(i);'))
arm('the destroy parks only after the upload is drained and the views (DLSS ones too) are gone, before the image is destroyed',
    dn.find('nvmtl_vk_image_upload_drain(i);') < dn.find('nvmtl_dlss_views_drop') < dn.find('if (may_park && !was_sparse && nvmtl_texrc_park(i)) return;') < dn.find('pvkDestroyImage(g_dev, (VkImage)'))
arm('a sparse image is never parked: the destroy notes it before it frees the sparse record the park would read',
    dn.find('const int was_sparse = i->sparse != NULL;') >= 0 and dn.find('const int was_sparse') < dn.find('free(i->sparse)')
    and 'if (may_park && !was_sparse && nvmtl_texrc_park(i)) return;' in dn)
for head, what in (('int nvmtl_vk_image_reback(nvk_image *i, int to_sysmem, int a8)\n{', 're-backed'),
                   ('int nvmtl_vk_image_migrate(nvk_image *i, int to_sysmem, int a8, nvk_image *old_out)\n{', 'migrated')):
    rb = body(vk, head)
    arm('a %s image loses its recycle mark (freed handles may come back equal)' % what,
        rb.find('i->rc_img = i->rc_mem = NULL;') > rb.find('i->img = img; i->mem = mem;') >= 0)
park = body(vk, 'static int nvmtl_texrc_park(nvk_image *i)\n{')
arm('the park checks the grace and the switch', 'nvmtl_texrc_on(), nvmtl_image_grace_on()' in park)
arm('nvmtl_texrc_park is called from the destroy alone', vk.count('nvmtl_texrc_park(') == 3)   # declaration, definition, destroy_now
arm('the park uncounts nothing', 'g_vram_bytes' not in park and 'discharge' not in park)
fr = body(vk, 'static void nvmtl_texrc_free(const nvmtl_texrc_entry *e)\n{')
arm('a real free uncounts and discharges what destroy_now would',
    'g_alloc_bytes -= e->bytes; g_vram_bytes -= e->bytes; g_img_align -= (e->bytes + 65535ull) & ~65535ull;' in fr and 'nvmtl_vk_budget_discharge(e->bytes);' in fr
    and 'pvkDestroyImage' in fr and 'pvkFreeMemory' in fr)
re_ = body(vk, 'static int nvmtl_texrc_reuse(const nvmtl_texrc_key *key, nvk_image *out)\n{')
arm('a take uncounts what nvmtl_commit_image counts again, and charges nothing',
    'g_alloc_bytes -= e.bytes; g_vram_bytes -= e.bytes; g_img_align -= (e.bytes + 65535ull) & ~65535ull;' in re_ and 'charge' not in re_)

w = body(vk, 'static int nvmtl_vk_vram_wall(uint64_t want, uint64_t *freev_out, uint64_t *bud_out, uint64_t *used_out, int *measured_out)\n{')
g = w.find('nvmtl_texrc_give_back_all()')
arm('the VRAM gate gives the cache back before res1 reclaims and res2 evicts', 0 <= g < w.find('nvmtl_reclaim_hook') and g < w.find('nvmtl_res2_evict_hook'))
room = body(vk, 'int nvmtl_vk_vram_room(uint64_t want)\n{')
arm('the residency pager\'s room check gives the cache back before it answers no', 'nvmtl_texrc_give_back_all()' in room)
am = body(vk, 'static VkResult nvmtl_vk_alloc_image_mem(const VkMemoryRequirements *mr, VkDeviceMemory *mem, uint32_t *sysmem, const char *what)\n{')
arm('an allocation RM refuses retries once with the cache given back, before res2 evicts',
    0 <= am.find('nvmtl_texrc_give_back_all()') < am.find('nvmtl_res2_evict_hook'))

arm('the grace reap does not let an image released by force park (a command buffer from before its death may still read it)',
    'else { t_texrc_may_park = !nvmtl_cl_forced(&e->mark); nvmtl_vk_image_destroy_now(&e->img); }' in reap)

# An RM refusal behind an open soft gate gives the parked images back and retries once, before the fallback to system
# memory, for the other VRAM allocations too.
bb = body(vk, 'static int nvmtl_vk_buffer_create_body(size_t size, int host_visible, nvk_buffer *out)\n{')
ga, al, fb = bb.find('nvmtl_texrc_give_back_all()'), bb.find('nvmtl_opaque_AllocateMemory(g_dev, &mai, NULL, &out->mem)'), bb.find('REFUSED VRAM')
arm('a Private buffer RM refuses retries with the cache given back before it goes to system memory', 0 <= al < ga < fb
    and 'if (!bgate && br == VK_ERROR_OUT_OF_DEVICE_MEMORY && nvmtl_texrc_give_back_all())' in bb)
sp = body(vk, 'static int nvmtl_vk_buffer_create_sparse(size_t size, nvk_buffer *out)\n{')
ga, al, fb = sp.find('nvmtl_texrc_give_back_all()'), sp.find('ar = pvkAllocateMemory(g_dev, &mai, NULL, &m);'), sp.find('nvmtl_vk_sysmem_type')
arm('a stable-address buffer RM refuses retries with the cache given back before it goes to system memory', 0 <= al < ga < fb
    and 'if (ar == VK_ERROR_OUT_OF_DEVICE_MEMORY && nvmtl_texrc_give_back_all())' in sp)
hb = body(vk, 'static int nvmtl_vk_heap_create_body(size_t size, int host_visible, nvk_heap *out)\n{')
ga, al, fb = hb.find('nvmtl_texrc_give_back_all()'), hb.find('pvkAllocateMemory(g_dev, &mai, NULL, &mem)'), hb.find('!(g_memp.memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)')
arm('a private placement heap RM refuses retries with the cache given back before it goes to system memory', 0 <= al < ga < fb
    and 'if (!hgate && !host_visible && r_ == VK_ERROR_OUT_OF_DEVICE_MEMORY && nvmtl_texrc_give_back_all())' in hb
    and ga < hb.find('nvmtl_vk_budget_charge(size)'))

# The residency pager's pressure pass gives the parked images back before it measures what it has to page out.
objs = (root / 'plugin' / 'NVMTLObjects.m').read_text()
rp = body(objs.replace('static void nvmtl_res2_pass(void) {', 'static void nvmtl_res2_pass(void)\n{'), 'static void nvmtl_res2_pass(void)\n{')
pw = rp[rp.find('if (want) {'):] if 'if (want) {' in rp else ''
arm('the pressure pass gives the cache back before it computes its target and what is free',
    0 <= pw.find('nvmtl_vk_texrc_give_back()') < pw.find('const uint64_t target') and pw.find('nvmtl_vk_texrc_give_back()') < pw.find('const uint64_t f0'))
arm('nvmtl_vk.h declares the give-back for the pager', 'uint32_t nvmtl_vk_texrc_give_back(void);' in hdr)
gb = body(vk, 'uint32_t nvmtl_vk_texrc_give_back(void)\n{')
arm('the pager\'s give-back marks the budget stale only when it freed something',
    'const uint32_t n = nvmtl_texrc_give_back_all();' in gb and 'if (n) g_budget_stale = 1;' in gb)

# The fault report's mark for a kept image is set before the entry can be freed by another thread, and cleared when the
# cache frees the image, before the handle can be handed out again.
pk = body(vk, 'static int nvmtl_texrc_park(nvk_image *i)\n{')
arm('a park marks the dead record kept before the entry is in the cache',
    0 <= pk.find('nvmtl_im_parked_dead(&g_im, i->img, g_cmd_submitted)') < pk.find('nvmtl_texrc_put(') and pk.find('nvmtl_im_parked_dead') < pk.find('memset(i, 0'))
fr = body(vk, 'static void nvmtl_texrc_free(const nvmtl_texrc_entry *e)\n{')
arm('a freed parked image is a plain death again in the fault report, cleared before its handle is destroyed',
    0 <= fr.find('nvmtl_im_kept_freed(&g_im, e->img)') < fr.find('pvkDestroyImage'))

on = body(vk, 'static int nvmtl_texrc_on(void)\n{')
arm('the switch is NVMTL_TEX_RECYCLE, read once, and WindowServer is told apart',
    'static int on = -1;' in on and 'getenv("NVMTL_TEX_RECYCLE")' in on and 'nvmtl_texrc_enabled(e, getprogname())' in on)

print('FAILED %d' % fails if fails else 'ok')
sys.exit(1 if fails else 0)
