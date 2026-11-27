/*
 * NullMoth NVIDIA driver for macOS
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 *
 * NVMTLAirNir.m - compute pipelines through NVK's AIR-to-NIR frontend (nvk/air, docs/AIR-TO-NIR.md phase 2).
 *
 * newComputePipelineStateWithFunction: asks here first for every compute kernel. NVK reads the kernel's AIR (the text IR
 * the plugin keeps, with the function's function-constant values) and builds its NIR, and the pipeline is created from
 * that NIR with the layout every compute pipeline has. A kernel the frontend refuses, one whose function-constant set is
 * incomplete, one NVK will not make a pipeline of, or one whose constexpr samplers would not sit where the translator's
 * reflection binds them, takes the SPIR-V road exactly as before, and the reason is logged once per function. So do the
 * dispatches that bind a pixel-coordinate argument sampler (its state is known only then; the translator specializes for
 * it) and those of a threadgroup size the road does not run. NVMTL_AIR_NIR=0 sends every kernel to the SPIR-V road; the
 * road is off in WindowServer for now, and off with an NVK that has no frontend (an older one, or the loader's).
 *
 * The function still gets its SPIR-V when it is made (the fallback, the reflection and the hazard model read it), so in
 * this phase the road adds the frontend's work to the translator's rather than replacing it.
 *
 * Variants: a threadgroup-size variant is a copy of the kernel's NIR with the size changed (nvmtl_compute_variant); a
 * threadgroup-memory variant is translated again with the lengths (nvmtl_air_nir_variant); the align guard's build is
 * translated again with no buffer access promising more than 4 bytes (nvmtl_air_nir_safe_build).
 *
 * Cache: the frontend's result and the serialized NIR are kept in the plugin's shader cache (the spvcache folder, stage
 * "nir1", named and swept by the translator's build tag as every entry there is), keyed like the function-constant
 * SPIR-V entries (the AIR's digest, the entry, the constants) and by the threadgroup size, the threadgroup-memory lengths,
 * the request's flags and the identity of this NVK and of the LLVM library; a refusal is kept too. NVK's own pipeline
 * cache keeps the compiled binary, as it does the SPIR-V road's.
 *
 * Imported at the end of NVMTLDevice.m, after nvmtl_vk.c and NVMTLLibraryLoad.m, whose statics it uses.
 */
#include <mach-o/loader.h>

static _Thread_local int g_air_nir_force_spirv;   // set while the SPIR-V twin of a kernel on the road is made
static _Thread_local int g_air_nir_hit;           // the last shader nvmtl_air_nir_make took came from the shader cache
static char g_air_nir_llvm[PATH_MAX];              // air-runtime/libLLVM.dylib beside this plugin
static NSString *g_air_nir_ids;                    // this NVK's and this LLVM's identity, in every cache key

// The log of this road: the plugin's log file, never the system log (a refusal message names what the frontend does not
// translate, and a release plugin sends every line with such words to the system log of every process).
static void nvmtl_air_nir_line(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void nvmtl_air_nir_line(const char *fmt, ...)
{
    char t[640]; va_list a; va_start(a, fmt); vsnprintf(t, sizeof t, fmt, a); va_end(a);
#if NVMTL_RELEASE
    if (!nvmtl_process_env_on()) return;
#else
    syslog(LOG_NOTICE, "NVMTL: %s", t);
#endif
    FILE *f = fopen(nvmtl_log_path(), "a"); if (f) { fprintf(f, "pid %d NVMTL: %s\n", getpid(), t); fclose(f); }
}

// The UUID of the Mach-O image that holds addr, as hex (an NVK build is told from another by it)
static int nvmtl_air_nir_image_uuid(const void *addr, char out[33])
{
    Dl_info i;
    if (!addr || !dladdr(addr, &i) || !i.dli_fbase) return -1;
    const struct mach_header_64 *h = (const struct mach_header_64 *)i.dli_fbase;
    if (h->magic != MH_MAGIC_64) return -1;
    const uint8_t *p = (const uint8_t *)(h + 1);
    for (uint32_t k = 0; k < h->ncmds; k++) {
        const struct load_command *lc = (const struct load_command *)p;
        if (lc->cmd == LC_UUID) {
            const struct uuid_command *u = (const struct uuid_command *)p;
            for (int b = 0; b < 16; b++) snprintf(out + 2 * b, 3, "%02x", u->uuid[b]);
            return 0;
        }
        p += lc->cmdsize;
    }
    return -1;
}

// 1 when this process takes the road: NVMTL_AIR_NIR is not 0, it is not WindowServer, NVK exports the frontend (opened
// directly), and the plugin's air-runtime is beside it. Decided once, and said once.
static int nvmtl_air_nir_on(void)
{
    static dispatch_once_t once; static int on;
    dispatch_once(&once, ^{
        const char *e = getenv("NVMTL_AIR_NIR"), *me = getprogname();
        const char *off = NULL;
        Dl_info info; struct stat st;
        if (!nvmtl_air_nir_switch(e, me)) off = (e && e[0] == '0') ? "NVMTL_AIR_NIR=0" : "WindowServer stays on the SPIR-V road for now";
        else if (nvmtl_vk_init() || !nvmtl_vk_air_nir_exports()) off = "this NVK has no AIR-to-NIR frontend, or was not opened directly";
        else if (!dladdr((const void *)&nvmtl_air_nir_on, &info) || !info.dli_fname ||
                 snprintf(g_air_nir_llvm, sizeof g_air_nir_llvm, "%s/air-runtime/libLLVM.dylib",
                          [@(info.dli_fname) stringByDeletingLastPathComponent].fileSystemRepresentation) >= (int)sizeof g_air_nir_llvm ||
                 stat(g_air_nir_llvm, &st) != 0)
            off = "the plugin's air-runtime is not beside it";
        else {
            char uuid[33] = "unknown";
            nvmtl_air_nir_image_uuid((const void *)pnvk_air_to_nir, uuid);
            g_air_nir_ids = [NSString stringWithFormat:@"nvk %s llvm %llu.%ld.%llu", uuid, (unsigned long long)st.st_size,
                             (long)st.st_mtimespec.tv_sec, (unsigned long long)st.st_ino];
            on = 1;
        }
        nvlog_notice("compute: AIR to NIR %s in %s (%s)", on ? "ON - a kernel it does not take stays on the SPIR-V road" : "off",
                     me ? me : "?", off ? off : (e ? "NVMTL_AIR_NIR set, not 0" : "NVMTL_AIR_NIR unset"));
    });
    return on;
}

// ---- the road's counts and its reasons --------------------------------------------------------------------------

static _Atomic unsigned long long g_air_nir_roads[2], g_air_nir_variants[2], g_air_nir_fallbacks;
// Each compute pipeline counts on its road; the first time a function takes the road, or falls back with a reason, a
// line says so (up to 1024 functions), and the counts are logged at every power of two of their sum.
void nvmtl_air_nir_note(NVMTLFunction *f, int road, BOOL variant, NSString *why, uint64_t ns)
{
    road = road ? 1 : 0;
    atomic_fetch_add(variant ? &g_air_nir_variants[road] : &g_air_nir_roads[road], 1);
    if (why) atomic_fetch_add(&g_air_nir_fallbacks, 1);
    if (f && (road || why)) {
        static NSMutableSet *said; static os_unfair_lock lock = OS_UNFAIR_LOCK_INIT;
        NSString *key = [NSString stringWithFormat:@"%@%s%d", f->_fname ?: @"?", variant ? "/v" : "", road];
        os_unfair_lock_lock(&lock);
        if (!said) said = [NSMutableSet new];
        const BOOL first = said.count < 1024 && ![said containsObject:key];
        if (first) [said addObject:key];
        os_unfair_lock_unlock(&lock);
        if (first && road)
            nvmtl_air_nir_line("air-nir: kernel %s%s: AIR to NIR (%.2f ms, %s)", f->_fname.UTF8String ?: "?",
                               variant ? " (threadgroup-memory variant)" : "", (double)ns / 1e6,
                               g_air_nir_hit ? "its NIR from the shader cache" : "translated");
        else if (first)
            nvmtl_air_nir_line("air-nir: kernel %s%s stays on the SPIR-V road: %s", f->_fname.UTF8String ?: "?",
                               variant ? " (variant)" : "", why.UTF8String ?: "?");
    }
    const unsigned long long n = atomic_load(&g_air_nir_roads[0]) + atomic_load(&g_air_nir_roads[1]) +
                                 atomic_load(&g_air_nir_variants[0]) + atomic_load(&g_air_nir_variants[1]);
    if (n && !(n & (n - 1)))
        nvlog_notice("compute roads in %s: AIR to NIR %llu pipeline(s) and %llu variant(s), SPIR-V %llu pipeline(s) and %llu "
                     "variant(s); %llu took the SPIR-V road after AIR to NIR said no", getprogname() ?: "?",
                     atomic_load(&g_air_nir_roads[1]), atomic_load(&g_air_nir_variants[1]), atomic_load(&g_air_nir_roads[0]),
                     atomic_load(&g_air_nir_variants[0]), atomic_load(&g_air_nir_fallbacks));
}

// ---- the frontend, through the shader cache ----------------------------------------------------------------------

static uint32_t nvmtl_air_nir_flags(uint32_t extra)
{
    return NVMTL_AIR_NIR_TEXT_IR | NVMTL_AIR_NIR_FC_VALUES | NVMTL_AIR_NIR_TG_DEFAULT
         | (nvmtl_table_ubo() ? 0 : NVMTL_AIR_NIR_TABLE_SSBO) | (g_compute_unchecked ? NVMTL_AIR_NIR_UNCHECKED : 0) | extra;
}

// The size the kernel is translated for: the translator's default (the module's SpecId defaults, as the SPIR-V road's
// creation-time pipeline is compiled for, nvmtl_cvar_seed.h), or 1x1x1. Every other size is a copy (nvmtl_compute_variant).
static void nvmtl_air_nir_local(NVMTLFunction *f, uint32_t local[3])
{
    uint32_t d[3];
    if (f->_spirv.length >= 20 && nvmtl_cvar_seed_size((const uint32_t *)f->_spirv.bytes, f->_spirv.length / 4, d) &&
        nvmtl_air_nir_size_ok(d, g_lim.maxComputeWorkGroupSize, g_lim.maxComputeWorkGroupInvocations))
        memcpy(local, d, sizeof d);
    else local[0] = local[1] = local[2] = 1;
}

// The frontend itself: the function's AIR text, its constants, the lengths; the status, and the result in *res
static int nvmtl_air_nir_translate(NVMTLFunction *f, NSDictionary *tgLen, uint32_t flags, const uint32_t local[3],
                                   struct nvmtl_air_nir_result *res)
{
    memset(res, 0, sizeof *res); res->size = sizeof *res;
    const char *text = f->_air.UTF8String;
    if (!text) { snprintf(res->message, sizeof res->message, "the AIR is not UTF-8"); return NVMTL_AIR_NIR_BAD_AIR; }
    struct nvmtl_air_nir_request req = { sizeof req, flags, g_air_nir_llvm, text, strlen(text), f->_fname.UTF8String,
                                         { local[0], local[1], local[2] } };
    for (NSString *k in tgLen) {
        const NSInteger i = k.integerValue; const NSUInteger len = [tgLen[k] unsignedIntegerValue];
        if (i >= 0 && i < (NSInteger)NVMTL_AIR_NIR_MAX_BUFFERS && len <= UINT32_MAX) req.threadgroup_lengths[i] = (uint32_t)len;
    }
    const NSUInteger nfc = f->_fc.count;
    NSMutableData *packed = [NSMutableData dataWithLength:(nfc ? nfc : 1) * (2 * sizeof(uint32_t) + sizeof(uint8_t *))];
    NSMutableData *bytes = [NSMutableData data];
    uint32_t *idx = packed.mutableBytes, *sz = idx + (nfc ? nfc : 1);
    const uint8_t **pay = (const uint8_t **)(sz + (nfc ? nfc : 1));
    NSMutableArray *offsets = [NSMutableArray array];
    uint32_t n = 0;
    for (NSString *k in f->_fc) {
        NSArray *a = f->_fc[k];
        if (![a isKindOfClass:[NSArray class]] || !a.count || a.count > 256) {
            snprintf(res->message, sizeof res->message, "function constant %s cannot be handed over", k.UTF8String ?: "?");
            return NVMTL_AIR_NIR_BAD_REQUEST;
        }
        idx[n] = (uint32_t)k.integerValue; sz[n] = (uint32_t)a.count; [offsets addObject:@(bytes.length)];
        for (NSNumber *b in a) { const uint8_t v = (uint8_t)b.unsignedCharValue; [bytes appendBytes:&v length:1]; }
        n++;
    }
    for (uint32_t k = 0; k < n; k++) pay[k] = (const uint8_t *)bytes.bytes + [offsets[k] unsignedLongValue];
    req.fc_count = n; req.fc_indices = idx; req.fc_sizes = sz; req.fc_payloads = pay;
    return pnvk_air_to_nir((void *)g_dev, &req, res);
}

// An entry in the shader cache and the memo (refusals included: the frontend is not asked about them again)
static void nvmtl_air_nir_store(NSString *path, NSString *memoKey, int32_t status, const struct nvmtl_air_nir_result *res,
                                const void *blob, size_t n)
{
    NSMutableData *e = [NSMutableData dataWithLength:nvmtl_air_nir_entry_size(n)];
    nvmtl_air_nir_entry_pack(e.mutableBytes, status, res, blob, n);
    if (memoKey) [nvmtl_memo() setObject:e forKey:memoKey cost:e.length];
    if (!path) return;
    NSString *file = [path copy];
    nvmtl_cache_io(^{ (void)nvmtl_cache_write(file, e); });
}

// 0 and the kernel's NIR in *nir (the caller owns it) with its result in *res, or -1 and why the road does not take it.
// *path and *memoKey name its cache entry, for a refusal found later (NVK would not make the pipeline).
static int nvmtl_air_nir_shader(NVMTLFunction *f, NSDictionary *tgLen, uint32_t extra, const uint32_t local[3],
                                struct nvmtl_air_nir_result *res, void **nir, NSString **why,
                                NSString **path, NSString **memoKey, BOOL *hit)
{
    *nir = NULL; *path = nil; *memoKey = nil; *hit = NO;
    const uint32_t flags = nvmtl_air_nir_flags(extra);
    NVMTLAirDigest *ad = nvmtl_air_state(f->_air);
    NSDictionary *k = @{ @"abi": @3, @"entry": f->_fname ?: @"", @"constants": f->_fc ?: @{}, @"threadgroup": tgLen ?: @{},
                         @"size": @[ @(local[0]), @(local[1]), @(local[2]) ], @"flags": @(flags), @"ids": g_air_nir_ids ?: @"" };
    NSData *tail = [NSJSONSerialization dataWithJSONObject:k options:NSJSONWritingSortedKeys error:NULL];
    if (ad && tail) {
        *path = nvmtl_spvcache_path_air(ad, tail, @"nir1");
        *memoKey = *path ?: nvmtl_spvcache_memo_key_air(ad, tail, @"nir1");
    }
    NSData *e = *memoKey ? [nvmtl_memo() objectForKey:*memoKey] : nil;
    if (!e) e = nvmtl_cache_read(*path);
    int32_t status = 0; const uint8_t *blob = NULL; size_t bn = 0;
    if (e && !nvmtl_air_nir_entry_unpack(e.bytes, e.length, &status, res, &blob, &bn)) {
        if (status != NVMTL_AIR_NIR_OK) { *why = @(res->message[0] ? res->message : "refused before"); if (*memoKey) [nvmtl_memo() setObject:e forKey:*memoKey cost:e.length]; return -1; }
        *nir = pnvk_air_nir_deserialize((void *)g_dev, blob, bn);
        if (*nir) {
            *hit = YES;
            if (*memoKey) [nvmtl_memo() setObject:e forKey:*memoKey cost:e.length];
            nvmtl_cache_touch(*path);
            return 0;
        }
        nvlog("air-nir: a cached shader of %s does not read back - translated again", f->_fname.UTF8String ?: "?");
    }
    const int st = nvmtl_air_nir_translate(f, tgLen, flags, local, res);
    if (st != NVMTL_AIR_NIR_OK) {
        *why = @(res->message[0] ? res->message : "the frontend said no");
        if (st == NVMTL_AIR_NIR_UNSUPPORTED || st == NVMTL_AIR_NIR_BAD_AIR) nvmtl_air_nir_store(*path, *memoKey, st, res, NULL, 0);
        if (res->nir) pnvk_air_nir_free(res->nir);
        return -1;
    }
    *nir = res->nir; res->nir = NULL;
    void *data = NULL; size_t n = 0;
    if (!pnvk_air_nir_serialize(*nir, &data, &n) && data && n) nvmtl_air_nir_store(*path, *memoKey, NVMTL_AIR_NIR_OK, res, data, n);
    free(data);
    return 0;
}

// The constexpr-sampler slots the translator's reflection gives the function (the ones the dispatch binds), or -1
static int nvmtl_air_nir_reflected_statics(NVMTLFunction *f, uint32_t slots[32], unsigned *n)
{
    *n = 0;
    char e[512] = { 0 };
    NSData *d = (f->_air && f->_stage.length && nvmtl_xlate_ready() && xlate_refl) ? nvmtl_reflect_air_cached(f, f->_air, f->_stage, e, sizeof e) : nil;
    NSDictionary *refl = d ? [NSJSONSerialization JSONObjectWithData:d options:0 error:NULL] : nil;
    NSArray *all = [refl isKindOfClass:[NSDictionary class]] ? refl[@"bindings"] : nil;
    if (![all isKindOfClass:[NSArray class]]) return -1;
    for (NSDictionary *b in all) {
        if (![b isKindOfClass:[NSDictionary class]] || ![b[@"kind"] isEqual:@"StaticSampler"]) continue;
        NSNumber *slot = b[@"metal_index"];
        if (![slot isKindOfClass:[NSNumber class]] || *n >= 32) return -1;
        slots[(*n)++] = slot.unsignedIntValue;
    }
    return 0;
}

// A pipeline state of the kernel on the road, translated with tgLen (nil: none), or nil and why not
// (no autorelease pool of its own: *why is autoreleased, and must outlive this function)
static NVMTLComputePipelineState *nvmtl_air_nir_make(NVMTLFunction *f, NSDictionary *tgLen, NSString **why, uint64_t *ns)
{
    const uint64_t t0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    uint32_t local[3];
    nvmtl_air_nir_local(f, local);
    struct nvmtl_air_nir_result res; void *nir = NULL; NSString *path = nil, *memoKey = nil; BOOL hit = NO;
    if (nvmtl_air_nir_shader(f, tgLen, 0, local, &res, &nir, why, &path, &memoKey, &hit)) return nil;
    if (res.static_sampler_count) {
        uint32_t slots[32]; unsigned n = 0;
        if (nvmtl_air_nir_reflected_statics(f, slots, &n) || !nvmtl_air_nir_statics_match(&res, slots, n)) {
            pnvk_air_nir_free(nir);
            *why = @"its constexpr samplers would not sit at the slots the translator's reflection binds";
            return nil;
        }
    }
    NVMTLComputePipelineState *ps = [NVMTLComputePipelineState new];
    if (nvmtl_vk_compute_pipeline_create_nir(nir, &res, local, &ps->_p)) {
        pnvk_air_nir_free(nir);
        struct nvmtl_air_nir_result no = res;
        snprintf(no.message, sizeof no.message, "NVK would not make a pipeline of its NIR");
        nvmtl_air_nir_store(path, memoKey, NVMTL_AN_NO_PIPELINE, &no, NULL, 0);
        *why = @(no.message);
        return nil;
    }
    ps->_p.nir_owner = (void *)CFBridgingRetain(@[ f, tgLen ?: @{} ]);
    ps->_function = f; ps->_road = 1; ps->_nirTg = tgLen.count ? [tgLen copy] : nil; ps->_attrT = nvmtl_air_max_tg(f);
    if (ns) *ns = clock_gettime_nsec_np(CLOCK_UPTIME_RAW) - t0;
    g_air_nir_hit = hit;
    return ps;
}

// ---- what the rest of the plugin calls -----------------------------------------------------------------------------

// Whether newComputePipelineState asks the road for f; NO with *why left nil when the road is off for the process
BOOL nvmtl_air_nir_wanted(NVMTLFunction *f, BOOL replaced, NSString **why)
{
    if (g_air_nir_force_spirv || !f || !nvmtl_air_nir_on()) return NO;
    if (!f->_air.length) { *why = @"the function has no AIR"; return NO; }
    if (f->_spirvOnly) { *why = @"its AIR is the translator's rewrite (a mesh, object, tile or stage-in kernel)"; return NO; }
    if (replaced || getenv("NVMTL_BDA_PROBE_FILE")) { *why = @"a replacement module is configured for it (NVMTL_BDA_PROBE_FILE)"; return NO; }
    if (g_app_profile && f->_spirv.length) {
        size_t en = 0; uint32_t *edited = nvmtl_app_profile_edit_module(g_app_profile, f->_spirv.bytes, f->_spirv.length, &en);
        if (edited) { free(edited); *why = @"the application profile edits its SPIR-V"; return NO; }
    }
    return YES;
}

NVMTLComputePipelineState *nvmtl_air_nir_pipeline(NVMTLFunction *f, NSDictionary *tgLen, NSString **why)
{
    return nvmtl_air_nir_make(f, tgLen, why, NULL);
}

// The threadgroup-memory variant of a kernel on the road; nil (and *why) sends the variant to the SPIR-V road
NVMTLComputePipelineState *nvmtl_air_nir_variant(NVMTLComputePipelineState *ps, NSDictionary *samplers, NSDictionary *tgLen, NSString **why)
{
    if (!ps || ps->_road != 1 || !ps->_function) return nil;
    if (samplers.count) { *why = @"a pixel-coordinate sampler is bound (its state reaches the translator only)"; return nil; }
    if (!tgLen.count) return nil;
    uint64_t ns = 0;
    NVMTLComputePipelineState *v = nvmtl_air_nir_make(ps->_function, tgLen, why, &ns);
    if (v) nvmtl_air_nir_note(ps->_function, 1, YES, nil, ns);
    return v;
}

// The pipeline a dispatch of threadgroup size l runs: ps, or for a size the road does not run (over the device's
// per-dimension limit, which the SPIR-V road linearizes) the kernel's SPIR-V twin, made once
NVMTLComputePipelineState *nvmtl_air_nir_for_size(NVMTLComputePipelineState *ps, const uint32_t l[3])
{
    if (!ps || ps->_road != 1 || nvmtl_vk_air_nir_size_ok(&ps->_p, l)) return ps;
    id twin;
    @synchronized (ps) {
        twin = ps->_spvTwin;
        if (!twin) { @autoreleasepool {
            NVMTLFunction *f = ps->_function;
            NVMTLComputePipelineState *t = nil; NSString *why = nil;
            if (ps->_nirTg.count) {
                NSData *spirv = nvmtl_translate_variant(f, nil, ps->_nirTg, &why);
                t = spirv ? [NVMTLComputePipelineState new] : nil;
                if (t && nvmtl_vk_compute_pipeline_create(spirv.bytes, spirv.length, &t->_p)) t = nil;
                if (t) t->_function = f;
            } else {
                NSError *e = nil;
                g_air_nir_force_spirv = 1;
                t = (NVMTLComputePipelineState *)[(id<MTLDevice>)gNVMTLMainDevice newComputePipelineStateWithFunction:f error:&e];
                g_air_nir_force_spirv = 0;
                if (![t isKindOfClass:[NVMTLComputePipelineState class]]) t = nil;
            }
            nvmtl_air_nir_line("air-nir: kernel %s dispatched with threadgroup %ux%ux%u, which the road does not run: %s",
                               f->_fname.UTF8String ?: "?", l[0], l[1], l[2], t ? "its SPIR-V twin runs it" : "no SPIR-V twin either");
            twin = ps->_spvTwin = t ?: (id)[NSNull null];
        } }
    }
    return twin == (id)[NSNull null] ? ps : (NVMTLComputePipelineState *)twin;
}

// The align guard's build of a kernel on the road (nvmtl_align_guard_pick): translated again with no buffer access
// promising more than 4 bytes, at the same size and lengths. 0 on success.
int nvmtl_air_nir_safe_build(const nvk_pipeline *base, nvk_pipeline *out)
{ @autoreleasepool {
    NSArray *o = (__bridge NSArray *)base->nir_owner;
    if (![o isKindOfClass:[NSArray class]] || o.count != 2) return -1;
    NVMTLFunction *f = o[0]; NSDictionary *tg = [o[1] count] ? o[1] : nil;
    struct nvmtl_air_nir_result res; void *nir = NULL; NSString *path = nil, *memoKey = nil, *why = nil; BOOL hit = NO;
    if (nvmtl_air_nir_shader(f, tg, NVMTL_AIR_NIR_ALIGN4, base->nir_local, &res, &nir, &why, &path, &memoKey, &hit)) {
        nvmtl_air_nir_line("air-nir: kernel %s: no build without alignment promises: %s", f->_fname.UTF8String ?: "?", why.UTF8String ?: "?");
        return -1;
    }
    if (nvmtl_vk_compute_pipeline_create_nir(nir, &res, base->nir_local, out)) { pnvk_air_nir_free(nir); return -1; }
    out->ag_mask = 0;
    out->nir_owner = (void *)CFBridgingRetain(o);
    return 0;
} }
