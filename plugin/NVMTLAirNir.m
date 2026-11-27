/*
 * NullMoth NVIDIA driver for macOS
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 *
 * NVMTLAirNir.m - compute pipelines (docs/AIR-TO-NIR.md phase 2) and render pipelines (phase 3) through NVK's AIR-to-NIR
 * frontend (nvk/air). The render road is at the end of this file.
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
        nvlog_notice("compute and render pipelines: AIR to NIR %s in %s (%s)", on ? "ON - a kernel or pipeline it does not take stays on the SPIR-V road" : "off",
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

// A render stage (varyings given) is made with the device's robustness, as every render pipeline is; a kernel without
// buffer robustness when compute pipelines are made so
static uint32_t nvmtl_air_nir_flags(uint32_t extra, BOOL render)
{
    return NVMTL_AIR_NIR_TEXT_IR | NVMTL_AIR_NIR_FC_VALUES | NVMTL_AIR_NIR_TG_DEFAULT
         | (nvmtl_table_ubo() ? 0 : NVMTL_AIR_NIR_TABLE_SSBO) | (g_compute_unchecked && !render ? NVMTL_AIR_NIR_UNCHECKED : 0)
         | (render ? NVMTL_AIR_NIR_VARYINGS : 0) | extra;
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

// The frontend itself: the function's AIR text, its constants, the lengths, a render stage's varyings (user semantic to
// Location); the status, and the result in *res
static int nvmtl_air_nir_translate(NVMTLFunction *f, NSDictionary *tgLen, uint32_t flags, const uint32_t local[3],
                                   NSDictionary<NSString *, NSNumber *> *varyings, struct nvmtl_air_nir_result *res)
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
    const NSUInteger nv = varyings.count;
    NSMutableData *vk = [NSMutableData dataWithLength:(nv ? nv : 1) * sizeof(const char *)];
    NSMutableData *vl = [NSMutableData dataWithLength:(nv ? nv : 1) * sizeof(uint32_t)];
    NSArray *names = [varyings.allKeys sortedArrayUsingSelector:@selector(compare:)];
    for (NSUInteger i = 0; i < nv; i++) {
        ((const char **)vk.mutableBytes)[i] = [names[i] UTF8String];
        ((uint32_t *)vl.mutableBytes)[i] = [varyings[names[i]] unsignedIntValue];
    }
    req.varying_count = (uint32_t)nv; req.varying_keys = (const char *const *)vk.bytes; req.varying_locations = vl.bytes;
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
// A render stage (varyings non-nil) adds its stage, its varyings and what else keys it (stageKey) to the entry's key.
static int nvmtl_air_nir_shader(NVMTLFunction *f, NSDictionary *tgLen, uint32_t extra, const uint32_t local[3],
                                NSDictionary *varyings, NSDictionary *stageKey,
                                struct nvmtl_air_nir_result *res, void **nir, NSString **why,
                                NSString **path, NSString **memoKey, BOOL *hit)
{
    *nir = NULL; *path = nil; *memoKey = nil; *hit = NO;
    const uint32_t flags = nvmtl_air_nir_flags(extra, varyings != nil);
    NVMTLAirDigest *ad = nvmtl_air_state(f->_air);
    NSMutableDictionary *k = [@{ @"abi": @4, @"entry": f->_fname ?: @"", @"constants": f->_fc ?: @{}, @"threadgroup": tgLen ?: @{},
                         @"size": @[ @(local[0]), @(local[1]), @(local[2]) ], @"flags": @(flags), @"ids": g_air_nir_ids ?: @"" } mutableCopy];
    if (varyings) { k[@"varyings"] = varyings; [k addEntriesFromDictionary:stageKey ?: @{}]; }
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
    const int st = nvmtl_air_nir_translate(f, tgLen, flags, local, varyings, res);
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

// The constexpr-sampler slots the translator's reflection gives the function (the ones the dispatch binds), or -1; and
// with words, each one's AIR state words as reflection read them (raw_words), for the comparison with the frontend's
static int nvmtl_air_nir_reflected_statics_w(NVMTLFunction *f, uint32_t slots[32], uint64_t (*words)[2], unsigned *n)
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
        if (words) {
            NSDictionary *st = [b[@"static_sampler"] isKindOfClass:[NSDictionary class]] ? b[@"static_sampler"] : nil;
            NSArray *raw = [st[@"raw_words"] isKindOfClass:[NSArray class]] ? st[@"raw_words"] : nil;
            words[*n][0] = raw.count == 2 && [raw[0] isKindOfClass:[NSNumber class]] ? [raw[0] unsignedLongLongValue] : ~0ull;
            words[*n][1] = raw.count == 2 && [raw[1] isKindOfClass:[NSNumber class]] ? [raw[1] unsignedLongLongValue] : ~0ull;
        }
        slots[(*n)++] = slot.unsignedIntValue;
    }
    return 0;
}
static int nvmtl_air_nir_reflected_statics(NVMTLFunction *f, uint32_t slots[32], unsigned *n)
{
    return nvmtl_air_nir_reflected_statics_w(f, slots, NULL, n);
}

// The constexpr samplers' state words the frontend read against reflection's, slot by slot: logged once per function,
// whether they agree or not (the plugin binds reflection's state either way). 0 when they all agree.
static int nvmtl_air_nir_statics_compare(NVMTLFunction *f, const struct nvmtl_air_nir_result *res)
{
    uint32_t slots[32]; uint64_t words[32][2]; unsigned n = 0;
    if (!res->static_sampler_count || nvmtl_air_nir_reflected_statics_w(f, slots, words, &n)) return 0;
    char line[512]; size_t at = 0; int differ = 0;
    for (unsigned k = 0; k < res->static_sampler_count && k < 32; k++) {
        unsigned j = 0;
        while (j < n && slots[j] != res->static_samplers[k].slot) j++;
        const int same = j < n && words[j][0] == res->static_samplers[k].words[0] && words[j][1] == res->static_samplers[k].words[1];
        differ |= !same;
        if (at < sizeof line - 1)
            at += (size_t)snprintf(line + at, sizeof line - at, " slot %u %s", res->static_samplers[k].slot, same ? "same" : "DIFFERENT");
        if (!same && at < sizeof line - 1)
            at += (size_t)snprintf(line + at, sizeof line - at, " (frontend %#llx %#llx, reflection %#llx %#llx)",
                                   (unsigned long long)res->static_samplers[k].words[0], (unsigned long long)res->static_samplers[k].words[1],
                                   j < n ? (unsigned long long)words[j][0] : 0ull, j < n ? (unsigned long long)words[j][1] : 0ull);
    }
    static NSMutableSet *said; static os_unfair_lock lock = OS_UNFAIR_LOCK_INIT;
    os_unfair_lock_lock(&lock);
    if (!said) said = [NSMutableSet new];
    const BOOL first = said.count < 1024 && ![said containsObject:f->_fname ?: @"?"];
    if (first) [said addObject:f->_fname ?: @"?"];
    os_unfair_lock_unlock(&lock);
    if (first) nvmtl_air_nir_line("air-nir: %s: constexpr sampler state words against the translator's reflection:%s", f->_fname.UTF8String ?: "?", line);
    return differ;
}

// A pipeline state of the kernel on the road, translated with tgLen (nil: none), or nil and why not
// (no autorelease pool of its own: *why is autoreleased, and must outlive this function)
static NVMTLComputePipelineState *nvmtl_air_nir_make(NVMTLFunction *f, NSDictionary *tgLen, NSString **why, uint64_t *ns)
{
    const uint64_t t0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    uint32_t local[3];
    nvmtl_air_nir_local(f, local);
    struct nvmtl_air_nir_result res; void *nir = NULL; NSString *path = nil, *memoKey = nil; BOOL hit = NO;
    if (nvmtl_air_nir_shader(f, tgLen, 0, local, nil, nil, &res, &nir, why, &path, &memoKey, &hit)) return nil;
    if (res.stage != NVMTL_AIR_NIR_STAGE_KERNEL) { pnvk_air_nir_free(nir); *why = @"it is not a kernel"; return nil; }
    if (res.static_sampler_count) {
        uint32_t slots[32]; unsigned n = 0;
        (void)nvmtl_air_nir_statics_compare(f, &res);
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
    if (nvmtl_air_nir_shader(f, tg, NVMTL_AIR_NIR_ALIGN4, base->nir_local, nil, nil, &res, &nir, &why, &path, &memoKey, &hit)) {
        nvmtl_air_nir_line("air-nir: kernel %s: no build without alignment promises: %s", f->_fname.UTF8String ?: "?", why.UTF8String ?: "?");
        return -1;
    }
    if (nvmtl_vk_compute_pipeline_create_nir(nir, &res, base->nir_local, out)) { pnvk_air_nir_free(nir); return -1; }
    out->ag_mask = 0;
    out->nir_owner = (void *)CFBridgingRetain(o);
    return 0;
} }

// ---- render pipelines (phase 3) -----------------------------------------------------------------------------------
//
// newRenderPipelineStateWithDescriptor: asks here first: both stages through the frontend, the pipeline from their NIR
// with the SPIR-V road's layout and state (nvmtl_vk_pipeline_create_nir). When either stage is refused, when the pipeline
// needs one of the SPIR-V road's rewrites of render stages, or when NVK will not make it, the whole pipeline takes the
// SPIR-V road exactly as before, and the reason is logged once per pipeline. The varyings take the Locations the
// translator's reflection gives them, the fragment's moved onto the vertex outputs of the same name by the link the SPIR-V
// road applies to its module (nvmtl_varying_remap), so both roads link a pipeline the same way.

static _Atomic unsigned long long g_air_nir_rroads[2], g_air_nir_rfallbacks;

void nvmtl_air_nir_render_note(NVMTLFunction *vf, NVMTLFunction *ff, BOOL road, NSString *why, uint64_t ns)
{
    atomic_fetch_add(&g_air_nir_rroads[road ? 1 : 0], 1);
    if (why) atomic_fetch_add(&g_air_nir_rfallbacks, 1);
    if (vf && (road || why)) {
        static NSMutableSet *said; static os_unfair_lock lock = OS_UNFAIR_LOCK_INIT;
        NSString *key = [NSString stringWithFormat:@"%@/%@%d", vf->_fname ?: @"?", ff ? ff->_fname ?: @"?" : @"-", road ? 1 : 0];
        os_unfair_lock_lock(&lock);
        if (!said) said = [NSMutableSet new];
        const BOOL first = said.count < 1024 && ![said containsObject:key];
        if (first) [said addObject:key];
        os_unfair_lock_unlock(&lock);
        if (first && road)
            nvmtl_air_nir_line("air-nir: pipeline %s / %s: AIR to NIR (%.2f ms, %s)", vf->_fname.UTF8String ?: "?",
                               ff ? ff->_fname.UTF8String ?: "?" : "-", (double)ns / 1e6,
                               g_air_nir_hit ? "both stages' NIR from the shader cache" : "translated");
        else if (first)
            nvmtl_air_nir_line("air-nir: pipeline %s / %s stays on the SPIR-V road: %s", vf->_fname.UTF8String ?: "?",
                               ff ? ff->_fname.UTF8String ?: "?" : "-", why.UTF8String ?: "?");
    }
    const unsigned long long n = atomic_load(&g_air_nir_rroads[0]) + atomic_load(&g_air_nir_rroads[1]);
    if (n && !(n & (n - 1)))
        nvlog_notice("render roads in %s: AIR to NIR %llu pipeline(s), SPIR-V %llu pipeline(s); %llu took the SPIR-V road "
                     "after AIR to NIR said no", getprogname() ?: "?", atomic_load(&g_air_nir_rroads[1]),
                     atomic_load(&g_air_nir_rroads[0]), atomic_load(&g_air_nir_rfallbacks));
}

// Whether newRenderPipelineState asks the road for the pipeline of vf and ff; NO with *why left nil when the road is off
// for the process. Every pipeline whose stages the SPIR-V road rewrites goes that road.
BOOL nvmtl_air_nir_render_wanted(NVMTLFunction *vf, NVMTLFunction *ff, BOOL noFrag, uint32_t a8mask, BOOL amplified,
                                 id samplePayload, NSString **why)
{
    if (!vf || !nvmtl_air_nir_on()) return NO;
    if (noFrag || !ff) { *why = @"it has no fragment function (the SPIR-V road's empty one stands in)"; return NO; }
    if (!vf->_air.length || !ff->_air.length) { *why = @"a stage has no AIR (a variant made from SPIR-V)"; return NO; }
    if (vf->_spirvOnly || ff->_spirvOnly) { *why = @"a stage's AIR is the translator's rewrite (a stage-in layout)"; return NO; }
    if (getenv("NVMTL_BDA_PROBE_FILE")) { *why = @"a replacement module may be configured (NVMTL_BDA_PROBE_FILE)"; return NO; }
    if (a8mask) { *why = @"it renders to an A8 target (its fragment stage is the translator's A8 variant)"; return NO; }
    if (amplified) { *why = @"vertex amplification (its stages are the translator's multiview variants)"; return NO; }
    uint32_t tpt = 0, tcps = 0;
    if (nvmtl_spirv_tess_info(vf->_spirv.bytes, vf->_spirv.length, &tpt, &tcps)) { *why = @"tessellation"; return NO; }
    if (nvmtl_gl_uniform_index(vf) < 32 || nvmtl_gl_uniform_index(ff) < 32) { *why = @"the GL uniform buffer"; return NO; }
    if (nvmtl_spirv_fetch_mask(ff->_spirv.bytes, ff->_spirv.length)) { *why = @"it reads [[color(n)]] (framebuffer fetch)"; return NO; }
    if (nvmtl_spirv_stencil_emu(ff->_spirv.bytes, ff->_spirv.length)) { *why = @"it exports stencil (emulated on the SPIR-V road)"; return NO; }
    if (samplePayload && samplePayload != [NSNull null]) { *why = @"it reads programmable sample positions"; return NO; }
    if (g_app_profile) {
        NVMTLFunction *fs[2] = { vf, ff };
        for (int i = 0; i < 2; i++) {
            size_t en = 0; uint32_t *edited = fs[i]->_spirv.length ? nvmtl_app_profile_edit_module(g_app_profile, fs[i]->_spirv.bytes, fs[i]->_spirv.length, &en) : NULL;
            if (edited) { free(edited); *why = @"the application profile edits a stage's SPIR-V"; return NO; }
        }
    }
    return YES;
}

// A stage's varyings as the translator's reflection gives them, user semantic (or field name) to Location, a
// fragment's moved by the link; nil (and *why) when they cannot be handed over
static NSDictionary *nvmtl_air_nir_varyings(NVMTLFunction *f, const uint32_t *remap, NSString **why)
{
    NSDictionary *r = nvmtl_function_reflection(f);
    NSArray *vs = [r isKindOfClass:[NSDictionary class]] ? r[@"varyings"] : nil;
    if (![vs isKindOfClass:[NSArray class]]) { *why = [NSString stringWithFormat:@"%@ has no reflected varyings", f->_fname]; return nil; }
    NSMutableDictionary *d = [NSMutableDictionary new];
    for (id v in vs) {
        NSNumber *l = [v isKindOfClass:[NSDictionary class]] ? ((NSDictionary *)v)[@"location"] : nil;
        NSString *k = nvmtl_varying_key(v);
        if (!k || ![l isKindOfClass:[NSNumber class]] || l.unsignedIntValue >= 128 || !nvmtl_varying_one_location(v)) {
            *why = [NSString stringWithFormat:@"a varying of %@ is not one named Location", f->_fname]; return nil; }
        const uint32_t L = remap ? remap[l.unsignedIntValue] : l.unsignedIntValue;
        if (L >= 32 || d[k]) { *why = [NSString stringWithFormat:@"a varying of %@ is past Location 31 or named twice", f->_fname]; return nil; }
        d[k] = @(L);
    }
    return d;
}

// The pipeline of vf and ff from their NIR into *out, or -1 and why not. Render stages promise no buffer alignment
// above 4 bytes (NVMTL_AIR_NIR_ALIGN4), as the SPIR-V road builds every render module whose accesses promise more SAFE.
int nvmtl_air_nir_render_pipeline(NVMTLFunction *vf, NVMTLFunction *ff, const nvk_rt *rt, const nvk_vertex_input *vin,
                                  uint32_t samples, NSString *vdesc, nvk_pipeline *out, NSString **why)
{
    uint32_t remap[128]; uint8_t have[128];
    const int link = nvmtl_varying_remap(vf, ff, remap, have, NULL);
    NSDictionary *vv = nvmtl_air_nir_varyings(vf, NULL, why), *fv = vv ? nvmtl_air_nir_varyings(ff, link > 0 ? remap : NULL, why) : nil;
    if (!vv || !fv) return -1;
    const uint32_t one[3] = { 1, 1, 1 };
    struct nvmtl_air_nir_result vres, fres; void *vn = NULL, *fn = NULL; NSString *path = nil, *memoKey = nil; BOOL vhit = NO, fhit = NO;
    NSString *stageWhy = nil;
    if (nvmtl_air_nir_shader(vf, nil, NVMTL_AIR_NIR_ALIGN4, one, vv, @{ @"stage": @"vertex", @"vertex_descriptor": vdesc ?: @"" },
                             &vres, &vn, &stageWhy, &path, &memoKey, &vhit)) {
        *why = [NSString stringWithFormat:@"vertex %@: %@", vf->_fname, stageWhy ?: @"?"]; return -1; }
    if (nvmtl_air_nir_shader(ff, nil, NVMTL_AIR_NIR_ALIGN4, one, fv, @{ @"stage": @"fragment" },
                             &fres, &fn, &stageWhy, &path, &memoKey, &fhit)) {
        pnvk_air_nir_free(vn);
        *why = [NSString stringWithFormat:@"fragment %@: %@", ff->_fname, stageWhy ?: @"?"]; return -1; }
    int rc = -1;
    if (vres.stage != NVMTL_AIR_NIR_STAGE_VERTEX || fres.stage != NVMTL_AIR_NIR_STAGE_FRAGMENT)
        *why = @"a stage is not the function the descriptor names it as";
    else {
        rc = 0;
        NVMTLFunction *fs[2] = { vf, ff }; const struct nvmtl_air_nir_result *rs[2] = { &vres, &fres };
        for (int i = 0; i < 2 && !rc; i++) {
            if (!rs[i]->static_sampler_count) continue;
            uint32_t slots[32]; unsigned n = 0;
            (void)nvmtl_air_nir_statics_compare(fs[i], rs[i]);
            if (nvmtl_air_nir_reflected_statics(fs[i], slots, &n) || !nvmtl_air_nir_statics_match(rs[i], slots, n)) {
                *why = [NSString stringWithFormat:@"the constexpr samplers of %@ would not sit at the slots the translator's reflection binds", fs[i]->_fname];
                rc = -1;
            }
        }
        if (!rc && nvmtl_vk_pipeline_create_nir(vn, &vres, fn, &fres, rt, vin, samples, out)) {
            *why = @"NVK would not make a pipeline of the two stages' NIR"; rc = -1; }
    }
    pnvk_air_nir_free(vn); pnvk_air_nir_free(fn);
    g_air_nir_hit = vhit && fhit;
    return rc;
}
