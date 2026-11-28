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
 * road is off with an NVK that has no frontend (an older one, or the loader's). Since 3.0.24 WindowServer takes it too,
 * behind its crash marker (nvmtl_air_nir_ws_arm), and a GPU fault in any process turns it off there for the rest of the
 * process and for its next launches with the same driver build (nvmtl_air_nir_gpu_fault, the fallback list).
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
static int g_air_nir_v5;                           // the NVK reads version 5: the reflection and the samplers' state
static int g_air_nir_v6;                           // the NVK reads version 6: [[color(n)]] inputs (framebuffer fetch)
static int g_air_nir_v7;                           // the NVK reads version 7: argument buffers, the heap's typed dummies
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

// The per-process fallback (nvmtl_air_nir_gpu_fault): set when a GPU fault in this process turned the road off
static _Atomic int g_air_nir_fallen;
static int g_air_nir_started;                      // the road was on when the process decided it
static char g_air_nir_off_path[PATH_MAX];           // this build's fallback list (nvmtl_air_nir.h), "" for none
static char g_air_nir_build[17];                    // this build's identity, hex (the list's and the marker's names)
static int g_air_nir_ws;                            // WindowServer (or the process a development plugin is told to treat so)
static char g_air_nir_ws_marker[PATH_MAX];          // WindowServer's crash marker (nvmtl_air_nir_ws_arm)

// The test hook (development plugins only): NVK_FAULT_INJECT=ws:<seconds>:<process> runs WindowServer's crash-marker
// logic in the process named, its clean minute the seconds given, so a test exercises it in a normal process. NVK
// takes no mode "ws" and ignores it.
static const char *nvmtl_air_nir_ws_hook(double *secs)
{
#if !NVMTL_RELEASE
    static char who[64]; static double s; static dispatch_once_t once;
    dispatch_once(&once, ^{
        const char *e = getenv("NVK_FAULT_INJECT");
        if (!e || strncmp(e, "ws:", 3)) return;
        char *end = NULL; const double v = strtod(e + 3, &end);
        if (!end || *end != ':' || !(v > 0) || !end[1] || strlen(end + 1) >= sizeof who) return;
        s = v; snprintf(who, sizeof who, "%s", end + 1);
    });
    if (secs) *secs = s;
    return who[0] ? who : NULL;
#else
    (void)secs; return NULL;
#endif
}

// The process the crash marker guards: WindowServer, or the one the test hook names
static int nvmtl_air_nir_is_ws(const char *me)
{
    if (me && !strcmp(me, "WindowServer")) return 1;
    const char *who = nvmtl_air_nir_ws_hook(NULL);
    return who && me && !strcmp(who, me);
}

// The seconds on the road after which WindowServer's start counts as clean: 60, or the test hook's
static double nvmtl_air_nir_ws_clean_s(void)
{
    double s = 0;
    return nvmtl_air_nir_ws_hook(&s) && s > 0 ? s : 60;
}

// Before WindowServer's first pipeline on the road: the crash marker, and a minute later, when no GPU fault turned
// the road off in between, its removal. A start that dies in that minute leaves it for the next one (nvmtl_air_nir_on).
static void nvmtl_air_nir_ws_arm(void)
{
    if (!g_air_nir_ws || !g_air_nir_ws_marker[0]) return;
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        const int fd = open(g_air_nir_ws_marker, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
        if (fd < 0) { nvlog("air-nir: WindowServer's crash marker %s was not written (%s)", g_air_nir_ws_marker, strerror(errno)); return; }
        char line[64]; const int n = snprintf(line, sizeof line, "pid %d\n", getpid());
        (void)!write(fd, line, (size_t)n); close(fd);
        const double clean = nvmtl_air_nir_ws_clean_s();
        nvlog_notice("AIR to NIR in %s: the crash marker is set until %.0f s on the road pass cleanly (%s)", getprogname() ?: "?", clean, g_air_nir_ws_marker);
        dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(clean * NSEC_PER_SEC)), dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
            if (atomic_load(&g_air_nir_fallen)) return;   // the fallback list keeps the road off now
            if (unlink(g_air_nir_ws_marker) == 0)
                nvlog_notice("AIR to NIR in %s: %.0f s on the road passed cleanly; the crash marker is cleared", getprogname() ?: "?", clean);
        });
    });
}

// This build's fallback list: in the plugin's shader-cache folder, named by a digest of this plugin's and this NVK's
// identity; the lists of other builds there are removed. 1 when this process's name is on it.
static int nvmtl_air_nir_off_listed_here(const char *ids)
{
    NSString *dir = nvmtl_spvcache_dir();
    char plug[33] = "unknown";
    nvmtl_air_nir_image_uuid((const void *)&nvmtl_air_nir_off_listed_here, plug);
    NSString *who = [NSString stringWithFormat:@"plugin %s %s", plug, ids ?: ""];
    const char *w = who.UTF8String ?: "";
    unsigned char h[CC_SHA256_DIGEST_LENGTH]; CC_SHA256(w, (CC_LONG)strlen(w), h);
    char build[17], name[96];
    for (int i = 0; i < 8; i++) snprintf(build + 2 * i, 3, "%02x", h[i]);
    memcpy(g_air_nir_build, build, sizeof build);
    if (!dir || nvmtl_air_nir_off_name(name, sizeof name, build) ||
        snprintf(g_air_nir_off_path, sizeof g_air_nir_off_path, "%s/%s", dir.fileSystemRepresentation, name) >= (int)sizeof g_air_nir_off_path) {
        g_air_nir_off_path[0] = 0; return 0; }
    char mk[96];
    if (g_air_nir_ws && (nvmtl_air_nir_ws_marker_name(mk, sizeof mk, build) ||
        snprintf(g_air_nir_ws_marker, sizeof g_air_nir_ws_marker, "%s/%s", dir.fileSystemRepresentation, mk) >= (int)sizeof g_air_nir_ws_marker))
        g_air_nir_ws_marker[0] = 0;
    NSString *d = [dir copy]; NSString *keep = @(build);
    nvmtl_cache_io(^{
        DIR *dh = opendir(d.fileSystemRepresentation); if (!dh) return;
        struct dirent *de;
        while ((de = readdir(dh)))
            if (nvmtl_air_nir_off_stale(de->d_name, keep.UTF8String) ||
                (!strncmp(de->d_name, "airnir-ws-", 10) && strncmp(de->d_name + 10, keep.UTF8String, keep.length)))
                (void)unlink([d stringByAppendingPathComponent:@(de->d_name)].fileSystemRepresentation);
        closedir(dh);
    });
    int fd = open(g_air_nir_off_path, O_RDONLY | O_CLOEXEC); if (fd < 0) return 0;
    char text[16384]; const ssize_t n = read(fd, text, sizeof text); close(fd);
    return n > 0 && nvmtl_air_nir_off_listed(text, (size_t)n, getprogname());
}

// 1 when this process takes the road: NVMTL_AIR_NIR is not 0, NVK exports the frontend (opened directly), the plugin's
// air-runtime is beside it, the process is not on this build's fallback list, and for WindowServer, no start before
// this one left its crash marker. Decided once, and said once; a GPU fault later in the process turns it off for good
// (nvmtl_air_nir_gpu_fault).
static int nvmtl_air_nir_on(void)
{
    static dispatch_once_t once; static int on;
    dispatch_once(&once, ^{
        const char *e = getenv("NVMTL_AIR_NIR"), *me = getprogname();
        const char *off = NULL;
        Dl_info info; struct stat st;
        g_air_nir_ws = nvmtl_air_nir_is_ws(me);
        if (!nvmtl_air_nir_switch(e, me)) off = "NVMTL_AIR_NIR=0";
        else if (nvmtl_vk_init() || !nvmtl_vk_air_nir_exports()) off = "this NVK has no AIR-to-NIR frontend, or was not opened directly";
        else if (!dladdr((const void *)&nvmtl_air_nir_on, &info) || !info.dli_fname ||
                 snprintf(g_air_nir_llvm, sizeof g_air_nir_llvm, "%s/air-runtime/libLLVM.dylib",
                          [@(info.dli_fname) stringByDeletingLastPathComponent].fileSystemRepresentation) >= (int)sizeof g_air_nir_llvm ||
                 stat(g_air_nir_llvm, &st) != 0)
            off = "the plugin's air-runtime is not beside it";
        else {
            char uuid[33] = "unknown";
            nvmtl_air_nir_image_uuid((const void *)pnvk_air_to_nir, uuid);
            g_air_nir_v5 = nvmtl_vk_air_nir_version() >= 5;
            g_air_nir_v6 = nvmtl_vk_air_nir_version() >= 6;
            g_air_nir_v7 = nvmtl_vk_air_nir_version() >= 7;
            g_air_nir_ids = [NSString stringWithFormat:@"nvk %s llvm %llu.%ld.%llu", uuid, (unsigned long long)st.st_size,
                             (long)st.st_mtimespec.tv_sec, (unsigned long long)st.st_ino];
            if (nvmtl_air_nir_off_listed_here(g_air_nir_ids.UTF8String))
                off = "the process faulted the GPU on the road before, with this driver build: it is on the fallback list";
            else if (g_air_nir_ws && g_air_nir_ws_marker[0] && access(g_air_nir_ws_marker, F_OK) == 0) {
                // the start before this one did not pass a clean minute on the road: on the list for the rest of the build
                char line[256];
                const int fd = open(g_air_nir_off_path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
                if (fd >= 0 && nvmtl_air_nir_off_line(line, sizeof line, me)) (void)!write(fd, line, strlen(line));
                if (fd >= 0) close(fd);
                (void)unlink(g_air_nir_ws_marker);
                off = "its crash marker was left by a start that did not pass a clean minute on the road: on the fallback list for this driver build";
            } else on = 1;
        }
        g_air_nir_started = on;
        nvlog_notice("compute and render pipelines: AIR to NIR %s in %s (%s%s%s%s)", on ? "ON - a kernel or pipeline it does not take stays on the SPIR-V road" : "off",
                     me ? me : "?", off ? off : (e ? "NVMTL_AIR_NIR set, not 0" : "NVMTL_AIR_NIR unset"),
                     on && !g_air_nir_v5 ? "; this NVK's frontend is older than version 5: every function is made with the translator, and a "
                                           "pixel-coordinate sampler's variant stays on the SPIR-V road" : "",
                     !on && g_air_nir_off_path[0] ? ", " : "", !on && g_air_nir_off_path[0] ? g_air_nir_off_path : "");
    });
    return on && !atomic_load_explicit(&g_air_nir_fallen, memory_order_acquire);
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

// Whether a fragment stage's [[color(n)]] inputs (framebuffer fetch) are the road's: with an NVK of version 6, unless
// the process emulates fetch by copies (NVMTL_FETCH_COPY, WindowServer's nvfetchcopy=1), which rewrites the SPIR-V
// road's module (nvmtl_fetch_copy.h): such a pipeline stays on that road. The road's pipeline then binds the pass's
// colour views (or the dummy image with fetch off) at set 3 exactly as the SPIR-V road's does (nvmtl_vk_pipeline_create_nir).
static int nvmtl_air_nir_fetch_ok(void) { return g_air_nir_v6 && !nvmtl_vk_fetch_copy_active(); }

// A render stage (varyings given) is made with the device's robustness, as every render pipeline is; a kernel without
// buffer robustness when compute pipelines are made so
static uint32_t nvmtl_air_nir_flags(uint32_t extra, BOOL render)
{
    // version 6: arrays of textures (an element chosen at run time through the bindless heap, as the translator reads it);
    // version 7: argument buffers, with the heap's five bindings (sampled and storage images, samplers, texel buffers),
    // and the typed dummies the translator is told of (nvmtl_translate_set_caps bit 3, NVMTLLibraryLoad.m)
    extern int nvmtl_typed_dummies_wanted(void);
    extern int nvmtl_vk_bindless_heap_bindings(void);
    return NVMTL_AIR_NIR_TEXT_IR | NVMTL_AIR_NIR_FC_VALUES | NVMTL_AIR_NIR_TG_DEFAULT
         | (nvmtl_table_ubo() ? 0 : NVMTL_AIR_NIR_TABLE_SSBO) | (g_compute_unchecked && !render ? NVMTL_AIR_NIR_UNCHECKED : 0)
         | (render ? NVMTL_AIR_NIR_VARYINGS : 0) | (render && nvmtl_air_nir_fetch_ok() ? NVMTL_AIR_NIR_COLOR_INPUTS : 0)
         | (g_air_nir_v6 ? NVMTL_AIR_NIR_TEXTURE_ARRAYS : 0) | (g_air_nir_v6 && nvmtl_vk_bindless_heap() ? NVMTL_AIR_NIR_HEAP : 0)
         | (g_air_nir_v7 && nvmtl_vk_bindless_heap_bindings() == 5 ? NVMTL_AIR_NIR_ARGBUF : 0)
         | (g_air_nir_v7 && nvmtl_typed_dummies_wanted() ? NVMTL_AIR_NIR_TYPED_DUMMIES : 0) | extra;
}

// The size the kernel is translated for: the translator's default (the module's SpecId defaults, as the SPIR-V road's
// creation-time pipeline is compiled for, nvmtl_cvar_seed.h), or 1x1x1. Every other size is a copy (nvmtl_compute_variant).
// A function newFunctionWithName: made without the translator (phase 4) has no SPIR-V yet: its size is its reflection's
// (the frontend's, the translator's TransformOptions default, which its SpecIds default to).
static void nvmtl_air_nir_local(NVMTLFunction *f, uint32_t local[3])
{
    uint32_t d[3];
    NSArray *ls = f->_spirvLazy && [f->_nrefl[@"local_size"] isKindOfClass:[NSArray class]] ? f->_nrefl[@"local_size"] : nil;
    if (f->_spirv.length >= 20 && nvmtl_cvar_seed_size((const uint32_t *)f->_spirv.bytes, f->_spirv.length / 4, d) &&
        nvmtl_air_nir_size_ok(d, g_lim.maxComputeWorkGroupSize, g_lim.maxComputeWorkGroupInvocations))
        memcpy(local, d, sizeof d);
    else if (ls.count == 3 && (d[0] = [ls[0] unsignedIntValue], d[1] = [ls[1] unsignedIntValue], d[2] = [ls[2] unsignedIntValue], 1) &&
             nvmtl_air_nir_size_ok(d, g_lim.maxComputeWorkGroupSize, g_lim.maxComputeWorkGroupInvocations))
        memcpy(local, d, sizeof d);
    else local[0] = local[1] = local[2] = 1;
}

// The frontend itself: the function's AIR text, its constants, the lengths, a render stage's varyings (user semantic to
// Location), the pixel-coordinate argument samplers' state words (slot to [word0, word1]); the status, and the result in
// *res (with NVMTL_AIR_NIR_REFLECT, res->reflection is the caller's to free())
static int nvmtl_air_nir_translate(NVMTLFunction *f, NSDictionary *tgLen, uint32_t flags, const uint32_t local[3],
                                   NSDictionary<NSString *, NSNumber *> *varyings, NSDictionary *samplers,
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
    const NSUInteger nv = varyings.count;
    NSMutableData *vk = [NSMutableData dataWithLength:(nv ? nv : 1) * sizeof(const char *)];
    NSMutableData *vl = [NSMutableData dataWithLength:(nv ? nv : 1) * sizeof(uint32_t)];
    NSArray *names = [varyings.allKeys sortedArrayUsingSelector:@selector(compare:)];
    for (NSUInteger i = 0; i < nv; i++) {
        ((const char **)vk.mutableBytes)[i] = [names[i] UTF8String];
        ((uint32_t *)vl.mutableBytes)[i] = [varyings[names[i]] unsignedIntValue];
    }
    req.varying_count = (uint32_t)nv; req.varying_keys = (const char *const *)vk.bytes; req.varying_locations = vl.bytes;
    const NSUInteger ns = samplers.count;
    NSMutableData *ss = [NSMutableData dataWithLength:(ns ? ns : 1) * sizeof(uint32_t)];
    NSMutableData *sw = [NSMutableData dataWithLength:(ns ? ns : 1) * 2 * sizeof(uint64_t)];
    NSUInteger at = 0;
    for (NSString *k in [samplers.allKeys sortedArrayUsingSelector:@selector(compare:)]) {
        NSArray *w = samplers[k];
        if (![w isKindOfClass:[NSArray class]] || w.count != 2) continue;
        ((uint32_t *)ss.mutableBytes)[at] = (uint32_t)k.integerValue;
        ((uint64_t *)sw.mutableBytes)[2 * at] = [w[0] unsignedLongLongValue];
        ((uint64_t *)sw.mutableBytes)[2 * at + 1] = [w[1] unsignedLongLongValue];
        at++;
    }
    if (at) req.flags |= NVMTL_AIR_NIR_SAMPLER_STATES;
    req.sampler_state_count = (uint32_t)at; req.sampler_state_slots = ss.bytes; req.sampler_state_words = sw.bytes;
    return pnvk_air_to_nir((void *)g_dev, &req, res);
}

// An entry in the shader cache and the memo (refusals included: the frontend is not asked about them again)
static void nvmtl_air_nir_store(NSString *path, NSString *memoKey, int32_t status, const struct nvmtl_air_nir_result *res,
                                const void *blob, size_t n)
{
    NSMutableData *e = [NSMutableData dataWithLength:nvmtl_air_nir_entry_size(n)];
    struct nvmtl_air_nir_result r = *res;
    r.reflection = NULL; r.reflection_size = 0;   /* only this process's pointer: the reflection has its own entry */
    nvmtl_air_nir_entry_pack(e.mutableBytes, status, &r, blob, n);
    if (memoKey) [nvmtl_memo() setObject:e forKey:memoKey cost:e.length];
    if (!path) return;
    NSString *file = [path copy];
    nvmtl_cache_io(^{ (void)nvmtl_cache_write(file, e); });
}

// 0 and the kernel's NIR in *nir (the caller owns it) with its result in *res, or -1 and why the road does not take it.
// *path and *memoKey name its cache entry, for a refusal found later (NVK would not make the pipeline).
// A render stage (varyings non-nil) adds its stage, its varyings and what else keys it (stageKey) to the entry's key.
// The shader-cache entry of f's NIR translated with these flags, size, lengths, varyings and sampler states
static void nvmtl_air_nir_key(NVMTLFunction *f, NSDictionary *tgLen, uint32_t flags, const uint32_t local[3], NSDictionary *varyings,
                              NSDictionary *stageKey, NSDictionary *samplers, NSString **path, NSString **memoKey)
{
    *path = nil; *memoKey = nil;
    NVMTLAirDigest *ad = nvmtl_air_state(f->_air);
    NSMutableDictionary *k = [@{ @"abi": @7, @"entry": f->_fname ?: @"", @"constants": f->_fc ?: @{}, @"threadgroup": tgLen ?: @{},
                         @"size": @[ @(local[0]), @(local[1]), @(local[2]) ], @"flags": @(flags), @"ids": g_air_nir_ids ?: @"" } mutableCopy];
    if (varyings) { k[@"varyings"] = varyings; [k addEntriesFromDictionary:stageKey ?: @{}]; }
    if (samplers.count) k[@"samplers"] = samplers;
    NSData *tail = [NSJSONSerialization dataWithJSONObject:k options:NSJSONWritingSortedKeys error:NULL];
    if (ad && tail) {
        *path = nvmtl_spvcache_path_air(ad, tail, @"nir1");
        *memoKey = *path ?: nvmtl_spvcache_memo_key_air(ad, tail, @"nir1");
    }
}

static int nvmtl_air_nir_shader(NVMTLFunction *f, NSDictionary *tgLen, uint32_t extra, const uint32_t local[3],
                                NSDictionary *varyings, NSDictionary *stageKey, NSDictionary *samplers,
                                struct nvmtl_air_nir_result *res, void **nir, NSString **why,
                                NSString **path, NSString **memoKey, BOOL *hit)
{
    *nir = NULL; *path = nil; *memoKey = nil; *hit = NO;
    const uint32_t flags = nvmtl_air_nir_flags(extra, varyings != nil);
    nvmtl_air_nir_key(f, tgLen, flags, local, varyings, stageKey, samplers, path, memoKey);
    NSData *e = *memoKey ? [nvmtl_memo() objectForKey:*memoKey] : nil;
    if (!e) e = nvmtl_cache_read(*path);
    int32_t status = 0; const uint8_t *blob = NULL; size_t bn = 0;
    if (e && !nvmtl_air_nir_entry_unpack(e.bytes, e.length, &status, res, &blob, &bn)) {
        res->reflection = NULL; res->reflection_size = 0;   /* a pointer of the process that wrote the entry */
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
    const int st = nvmtl_air_nir_translate(f, tgLen, flags, local, varyings, samplers, res);
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
    NSDictionary *refl = nvmtl_function_reflection(f);   /* the frontend's for a function made without the translator */
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
static NVMTLComputePipelineState *nvmtl_air_nir_make(NVMTLFunction *f, NSDictionary *tgLen, NSDictionary *samplers, NSString **why,
                                                     uint64_t *ns)
{
    const uint64_t t0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    uint32_t local[3];
    nvmtl_air_nir_local(f, local);
    struct nvmtl_air_nir_result res; void *nir = NULL; NSString *path = nil, *memoKey = nil; BOOL hit = NO;
    if (nvmtl_air_nir_shader(f, tgLen, 0, local, nil, nil, samplers, &res, &nir, why, &path, &memoKey, &hit)) return nil;
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
    nvmtl_air_nir_ws_arm();
    NVMTLComputePipelineState *ps = [NVMTLComputePipelineState new];
    if (nvmtl_vk_compute_pipeline_create_nir(nir, &res, local, &ps->_p)) {
        pnvk_air_nir_free(nir);
        struct nvmtl_air_nir_result no = res;
        snprintf(no.message, sizeof no.message, "NVK would not make a pipeline of its NIR");
        nvmtl_air_nir_store(path, memoKey, NVMTL_AN_NO_PIPELINE, &no, NULL, 0);
        *why = @(no.message);
        return nil;
    }
    ps->_p.nir_owner = (void *)CFBridgingRetain(@[ f, tgLen ?: @{}, samplers ?: @{} ]);
    ps->_function = f; ps->_road = 1; ps->_nirTg = tgLen.count ? [tgLen copy] : nil; ps->_attrT = nvmtl_air_max_tg(f);
    if (ns) *ns = clock_gettime_nsec_np(CLOCK_UPTIME_RAW) - t0;
    g_air_nir_hit = hit;
    return ps;
}

// ---- what the rest of the plugin calls -----------------------------------------------------------------------------

static NSDictionary *nvmtl_air_nir_sampler_words_all(NSDictionary *states, NSString **why);

// Whether newComputePipelineState asks the road for f; NO with *why left nil when the road is off for the process
BOOL nvmtl_air_nir_wanted(NVMTLFunction *f, BOOL replaced, NSString **why)
{
    if (g_air_nir_force_spirv || !f || !nvmtl_air_nir_on()) return NO;
    if (!f->_air.length) { *why = @"the function has no AIR"; return NO; }
    if (f->_spirvOnly) { *why = @"its AIR is the translator's rewrite (a mesh, object, tile or stage-in kernel)"; return NO; }
    if (replaced || getenv("NVMTL_BDA_PROBE_FILE")) { *why = @"a replacement module is configured for it (NVMTL_BDA_PROBE_FILE)"; return NO; }
    NSData *sp = g_app_profile ? nvmtl_function_spirv(f, "the application profile reads it") : nil;
    if (g_app_profile && sp.length) {
        size_t en = 0; uint32_t *edited = nvmtl_app_profile_edit_module(g_app_profile, sp.bytes, sp.length, &en);
        if (edited) { free(edited); *why = @"the application profile edits its SPIR-V"; return NO; }
    }
    return YES;
}

NVMTLComputePipelineState *nvmtl_air_nir_pipeline(NVMTLFunction *f, NSDictionary *tgLen, NSString **why)
{
    return nvmtl_air_nir_make(f, tgLen, nil, why, NULL);
}

// The threadgroup-memory variant of a kernel on the road; nil (and *why) sends the variant to the SPIR-V road
NVMTLComputePipelineState *nvmtl_air_nir_variant(NVMTLComputePipelineState *ps, NSDictionary *samplers, NSDictionary *tgLen, NSString **why)
{
    if (!ps || ps->_road != 1 || !ps->_function) return nil;
    if (atomic_load_explicit(&g_air_nir_fallen, memory_order_acquire)) { *why = @"a GPU fault turned the road off in this process"; return nil; }
    NSDictionary *words = nil;
    if (samplers.count && !g_air_nir_v5) { *why = @"a pixel-coordinate sampler is bound and this NVK's frontend takes no sampler state (older than version 5)"; return nil; }
    if (samplers.count && !(words = nvmtl_air_nir_sampler_words_all(samplers, why))) return nil;
    if (!tgLen.count && !words.count) return nil;
    uint64_t ns = 0;
    NVMTLComputePipelineState *v = nvmtl_air_nir_make(ps->_function, tgLen, words, why, &ns);
    if (v) nvmtl_air_nir_note(ps->_function, 1, YES, nil, ns);
    return v;
}

// The pipeline a dispatch of threadgroup size l runs: ps, or for a size the road does not run (over the device's
// per-dimension limit, which the SPIR-V road linearizes) or once a GPU fault turned the road off in the process
// (nvmtl_air_nir_gpu_fault), the kernel's SPIR-V twin, made once
NVMTLComputePipelineState *nvmtl_air_nir_for_size(NVMTLComputePipelineState *ps, const uint32_t l[3])
{
    if (!ps || ps->_road != 1) return ps;
    const BOOL fallen = atomic_load_explicit(&g_air_nir_fallen, memory_order_acquire);
    if (!fallen && nvmtl_vk_air_nir_size_ok(&ps->_p, l)) return ps;
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
            if (fallen)
                nvmtl_air_nir_line("air-nir: kernel %s dispatched after a GPU fault turned the road off: %s", f->_fname.UTF8String ?: "?",
                                   t ? "its SPIR-V twin runs it" : "no SPIR-V twin, the road's pipeline runs it");
            else
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
    if (![o isKindOfClass:[NSArray class]] || o.count != 3) return -1;
    NVMTLFunction *f = o[0]; NSDictionary *tg = [o[1] count] ? o[1] : nil, *sw = [o[2] count] ? o[2] : nil;
    struct nvmtl_air_nir_result res; void *nir = NULL; NSString *path = nil, *memoKey = nil, *why = nil; BOOL hit = NO;
    if (nvmtl_air_nir_shader(f, tg, NVMTL_AIR_NIR_ALIGN4, base->nir_local, nil, nil, sw, &res, &nir, &why, &path, &memoKey, &hit)) {
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

// The pipeline a render encoder binds for ps: ps, or once a GPU fault turned the road off in the process, a road
// pipeline's SPIR-V twin, made at its first bind from the descriptor it was made with (the road is off by then, so
// that is the SPIR-V road). Pipelines already in command buffers keep theirs.
int nvmtl_air_nir_fallen(void) { return atomic_load_explicit(&g_air_nir_fallen, memory_order_relaxed); }

NVMTLRenderPipelineState *nvmtl_air_nir_render_bound(NVMTLRenderPipelineState *ps)
{
    if (!ps || ps->_road != 1 || !atomic_load_explicit(&g_air_nir_fallen, memory_order_acquire)) return ps;
    id twin;
    @synchronized (ps) {
        twin = ps->_spvTwin;
        if (!twin) { @autoreleasepool {
            MTLRenderPipelineDescriptor *d = [ps->_descriptor isKindOfClass:[MTLRenderPipelineDescriptor class]] ? ps->_descriptor : nil;
            NSError *e = nil;
            id t = d ? [(id<MTLDevice>)gNVMTLMainDevice newRenderPipelineStateWithDescriptor:d error:&e] : nil;
            if (![t isKindOfClass:[NVMTLRenderPipelineState class]] || ((NVMTLRenderPipelineState *)t)->_road) t = nil;
            nvmtl_air_nir_line("air-nir: pipeline %s / %s bound after a GPU fault turned the road off: %s", ps->_vname.UTF8String ?: "?",
                               ps->_fname.UTF8String ?: "?", t ? "its SPIR-V twin draws" : "no SPIR-V twin, the road's pipeline draws");
            twin = ps->_spvTwin = t ?: (id)[NSNull null];
        } }
    }
    return twin == (id)[NSNull null] ? ps : (NVMTLRenderPipelineState *)twin;
}

// A GPU fault in this process (nvmtl_vk.c: a channel rebuild dropped a submission, or the device was lost). When the
// road was on and made any pipeline, it is turned off for the rest of the process: new pipelines and variants take the
// SPIR-V road, the road's pipelines switch to their SPIR-V twins at their next bind or dispatch
// (nvmtl_air_nir_render_bound, nvmtl_air_nir_for_size), and the process name goes on this build's fallback list so its
// next launches start on the SPIR-V road. Said once in the system log. NVMTL_AIR_NIR=0 keeps the road off everywhere.
void nvmtl_air_nir_gpu_fault(const char *what)
{
    if (!g_air_nir_started) return;
    const unsigned long long made = atomic_load(&g_air_nir_roads[1]) + atomic_load(&g_air_nir_variants[1]) + atomic_load(&g_air_nir_rroads[1]);
    if (!made || atomic_exchange(&g_air_nir_fallen, 1)) return;
    const char *me = getprogname() ?: "?";
    nvlog_notice("AIR to NIR off in %s from now on: a GPU fault (%s) after %llu pipeline(s) and variant(s) on the road; new "
                 "pipelines take the SPIR-V road, the road's switch to their SPIR-V twins at their next use, and %s %s",
                 me, what ? what : "?", made, g_air_nir_off_path[0] ? "its next launches start on the SPIR-V road with this driver build:" :
                 "its next launches are not told (no shader-cache folder)", g_air_nir_off_path[0] ? g_air_nir_off_path : "");
    char line[256];
    if (!g_air_nir_off_path[0] || !nvmtl_air_nir_off_line(line, sizeof line, me)) return;
    // written here, not on the cache queue: a process whose device is lost may be about to end
    const int fd = open(g_air_nir_off_path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    const size_t n = strlen(line);
    if (fd < 0 || write(fd, line, n) != (ssize_t)n) nvlog("air-nir: the fallback list %s was not written (%s)", g_air_nir_off_path, strerror(errno));
    if (fd >= 0) close(fd);
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
    // A stage newFunctionWithName: made without the translator (phase 4) has no SPIR-V to read: the frontend translated it,
    // and it refuses a post-tessellation vertex function and [[stencil]] outputs, and [[color(n)]] inputs unless it was
    // asked for them (nvmtl_air_nir_fetch_ok), so none applies.
    uint32_t tpt = 0, tcps = 0;
    if (!vf->_spirvLazy && nvmtl_spirv_tess_info(vf->_spirv.bytes, vf->_spirv.length, &tpt, &tcps)) { *why = @"tessellation"; return NO; }
    if (nvmtl_gl_uniform_index(vf) < 32 || nvmtl_gl_uniform_index(ff) < 32) { *why = @"the GL uniform buffer"; return NO; }
    if (!ff->_spirvLazy && nvmtl_spirv_fetch_mask(ff->_spirv.bytes, ff->_spirv.length) && !nvmtl_air_nir_fetch_ok()) {
        *why = g_air_nir_v6 ? @"it reads [[color(n)]] and framebuffer fetch is emulated by copies here (NVMTL_FETCH_COPY)"
                            : @"it reads [[color(n)]] (framebuffer fetch) and this NVK's frontend is older than version 6"; return NO; }
    if (!ff->_spirvLazy && nvmtl_spirv_stencil_emu(ff->_spirv.bytes, ff->_spirv.length)) { *why = @"it exports stencil (emulated on the SPIR-V road)"; return NO; }
    if (samplePayload && samplePayload != [NSNull null]) { *why = @"it reads programmable sample positions"; return NO; }
    if (g_app_profile) {
        NVMTLFunction *fs[2] = { vf, ff };
        for (int i = 0; i < 2; i++) {
            NSData *sp = nvmtl_function_spirv(fs[i], "the application profile reads it");
            size_t en = 0; uint32_t *edited = sp.length ? nvmtl_app_profile_edit_module(g_app_profile, sp.bytes, sp.length, &en) : NULL;
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
                             vf->_samplerStates, &vres, &vn, &stageWhy, &path, &memoKey, &vhit)) {
        *why = [NSString stringWithFormat:@"vertex %@: %@", vf->_fname, stageWhy ?: @"?"]; return -1; }
    if (nvmtl_air_nir_shader(ff, nil, NVMTL_AIR_NIR_ALIGN4, one, fv, @{ @"stage": @"fragment" },
                             ff->_samplerStates, &fres, &fn, &stageWhy, &path, &memoKey, &fhit)) {
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
        if (!rc) nvmtl_air_nir_ws_arm();
        if (!rc && nvmtl_vk_pipeline_create_nir(vn, &vres, fn, &fres, rt, vin, samples, out)) {
            *why = @"NVK would not make a pipeline of the two stages' NIR"; rc = -1; }
    }
    pnvk_air_nir_free(vn); pnvk_air_nir_free(fn);
    g_air_nir_hit = vhit && fhit;
    return rc;
}

// ---- phase 4: functions made without the translator --------------------------------------------------------------
//
// newFunctionWithName: (and its function-constant variants) ask the frontend first (nvmtl_air_nir_lazy): a kernel, vertex
// or fragment function it translates, and whose reflection it gives in the translator's form (nvk/air: the bindings,
// varyings, vertex attributes and default threadgroup size the plugin reads), is made with that reflection and no SPIR-V.
// The translator runs the first time something needs SPIR-V of it (nvmtl_function_spirv: a pipeline or variant the road
// does not make, a rewrite of the SPIR-V road's, a pipeline NVK refuses), and then exactly as newFunctionWithName: would
// have run it. A function the frontend refuses or does not reflect is made as before. NVMTL_AIR_NIR=0, a process the
// fallback list names, an application profile and the check run (NVMTL_AIR_NIR_REFL_CHECK=1, development builds) make
// every function as before.

static _Atomic unsigned long long g_lazy_made, g_lazy_spirv, g_lazy_ns, g_lazy_spirv_ns;

// What the check run compares (development builds): NVMTL_AIR_NIR_REFL_CHECK=1 makes every function as before and
// compares the frontend's reflection of it with the translator's, key by key
static int nvmtl_air_nir_refl_check_on(void)
{
#if NVMTL_RELEASE
    return 0;
#else
    static int on = -1;
    if (on < 0) { const char *e = getenv("NVMTL_AIR_NIR_REFL_CHECK"); on = e && e[0] == '1'; }
    return on;
#endif
}

static void nvmtl_air_nir_lazy_report(void)
{
    const unsigned long long n = atomic_load(&g_lazy_made), k = atomic_load(&g_lazy_spirv);
    if (!n) return;
    nvlog_notice("functions in %s: %llu made without the translator (%.1f ms of the frontend), %llu of them translated later "
                 "(%.1f ms of the translator)", getprogname() ?: "?", n, (double)atomic_load(&g_lazy_ns) / 1e6, k,
                 (double)atomic_load(&g_lazy_spirv_ns) / 1e6);
}

// A pixel-coordinate MTLSamplerDescriptor's state (NVMTLSamplerState _pixelState: the translator's names) as the AIR
// constexpr-sampler words the frontend reads (reflect::StaticSamplerState::from_air_words, the other way); NO for a
// state the words cannot hold (MirrorClampToEdge)
static BOOL nvmtl_air_nir_sampler_words(NSDictionary *st, uint64_t w[2])
{
    if (![st isKindOfClass:[NSDictionary class]]) return NO;
    NSUInteger (^pick)(NSString *, NSArray *) = ^NSUInteger(NSString *key, NSArray *names) {
        id v = st[key]; NSUInteger i = [v isKindOfClass:[NSString class]] ? [names indexOfObject:v] : NSNotFound; return i; };
    const NSUInteger border = pick(@"border_color", @[ @"TransparentBlack", @"OpaqueBlack", @"OpaqueWhite" ]);
    const NSUInteger fmin = pick(@"min_filter", @[ @"Nearest", @"Linear", @"Bicubic" ]), fmag = pick(@"mag_filter", @[ @"Nearest", @"Linear", @"Bicubic" ]);
    const NSUInteger mip = pick(@"mip_filter", @[ @"None", @"Nearest", @"Linear" ]);
    const NSUInteger cmp = pick(@"compare_function", @[ @"None", @"Less", @"LessEqual", @"Greater", @"GreaterEqual", @"Equal", @"NotEqual", @"Always", @"Never" ]);
    const NSUInteger red = pick(@"reduction", @[ @"WeightedAverage", @"Minimum", @"Maximum" ]);
    if (border == NSNotFound || fmin == NSNotFound || fmag == NSNotFound || mip == NSNotFound || cmp == NSNotFound || red == NSNotFound) return NO;
    uint64_t a = 0;
    NSString *const keys[3] = { @"address_mode_s", @"address_mode_t", @"address_mode_r" };
    for (int k = 0; k < 3; k++) {
        const NSUInteger m = pick(keys[k], @[ @"ClampToZero", @"ClampToEdge", @"Repeat", @"MirroredRepeat", @"ClampToBorder" ]);
        if (m == NSNotFound) return NO;
        a |= (uint64_t)(m == 4 ? 0 : m) << (3 * k);   // ClampToBorder is code 0 with a border colour (that is not transparent black)
    }
    float (^num)(NSString *, float) = ^float(NSString *key, float d) { id v = st[key]; return [v isKindOfClass:[NSNumber class]] ? [v floatValue] : d; };
    uint16_t (^half)(float) = ^uint16_t(float x) {
        if (!(x == x)) x = 0;
        if (x > 65504.0f) x = 65504.0f;
        if (x < -65504.0f) x = -65504.0f;
        _Float16 h = (_Float16)x; uint16_t b; memcpy(&b, &h, sizeof b); return b; };
    const float an = num(@"max_anisotropy", 1);
    const uint64_t aniso = an >= 1 && an <= 16 ? (uint64_t)an - 1 : 0;
    w[0] = a | ((uint64_t)fmag << 9) | ((uint64_t)fmin << 11) | ((uint64_t)mip << 13) |
           ((uint64_t)([st[@"coordinates"] isEqual:@"Pixel"] ? 1 : 0) << 15) | ((uint64_t)cmp << 16) | (aniso << 20) |
           ((uint64_t)half(num(@"lod_min_clamp", 0)) << 24) | ((uint64_t)half(num(@"lod_max_clamp", 65504.0f)) << 40) |
           ((uint64_t)border << 56) | ((uint64_t)red << 58);
    w[1] = half(num(@"lod_bias", 0));
    return YES;
}

// Every bound pixel-coordinate sampler as words, slot to [word0, word1]; nil and *why when one cannot be carried
static NSDictionary *nvmtl_air_nir_sampler_words_all(NSDictionary *states, NSString **why)
{
    NSMutableDictionary *out = [NSMutableDictionary dictionaryWithCapacity:states.count];
    for (NSString *k in states) {
        uint64_t w[2];
        if (!nvmtl_air_nir_sampler_words(states[k], w)) {
            if (why) *why = [NSString stringWithFormat:@"the pixel-coordinate sampler at slot %@ has a state the AIR words do not hold", k];
            return nil;
        }
        out[[NSString stringWithFormat:@"%ld", (long)k.integerValue]] = @[ @(w[0]), @(w[1]) ];
    }
    return out;
}

// The frontend's reflection of f, as newFunctionWithName: asks for it: the function translated with NVMTL_AIR_NIR_REFLECT
// as its pipeline asks (a kernel at the size its reflection gives, 64x1x1, with the flags and the threadgroup-memory
// lengths of nvmtl_air_nir_make, its NIR stored where the pipeline looks it up; a vertex or fragment function with its own
// Locations), the reflection kept in the shader cache ("nrf1": the AIR, the entry, the constants, the stage, this NVK and
// LLVM). nil and *why when the frontend refuses the function, or translates it but does not reflect it.
static NSDictionary *nvmtl_air_nir_function_reflection(NVMTLFunction *f, NSString **why)
{
    NVMTLAirDigest *ad = nvmtl_air_state(f->_air);
    const BOOL kernel = [f->_stage isEqualToString:@"kernel"];
    // a kernel as nvmtl_air_nir_make asks; a render stage with the device's robustness and no buffer alignment above 4,
    // as nvmtl_air_nir_render_pipeline asks, but with its own Locations (the pipeline hands it the linked ones)
    const uint32_t flags = kernel ? nvmtl_air_nir_flags(0, NO) : (nvmtl_air_nir_flags(NVMTL_AIR_NIR_ALIGN4, YES) & ~NVMTL_AIR_NIR_VARYINGS);
    NSDictionary *k = @{ @"abi": @7, @"entry": f->_fname ?: @"", @"constants": f->_fc ?: @{}, @"stage": f->_stage ?: @"",
                         @"ids": g_air_nir_ids ?: @"", @"flags": @(flags) };
    NSData *tail = [NSJSONSerialization dataWithJSONObject:k options:NSJSONWritingSortedKeys error:NULL];
    NSString *path = ad && tail ? nvmtl_spvcache_path_air(ad, tail, @"nrf1") : nil;
    NSString *memoKey = path ?: (ad && tail ? nvmtl_spvcache_memo_key_air(ad, tail, @"nrf1") : nil);
    NSData *e = memoKey ? [nvmtl_memo() objectForKey:memoKey] : nil;
    if (!e) e = nvmtl_cache_read(path);
    if (e.length >= 1) {
        const char tag = ((const char *)e.bytes)[0];
        NSData *body = [e subdataWithRange:NSMakeRange(1, e.length - 1)];
        if (tag == 'R') {
            NSDictionary *d = [NSJSONSerialization JSONObjectWithData:body options:0 error:NULL];
            if ([d isKindOfClass:[NSDictionary class]] && d.count) {
                if (memoKey) [nvmtl_memo() setObject:e forKey:memoKey cost:e.length];
                nvmtl_cache_touch(path);
                return d;
            }
        } else if (tag == 'N') {
            if (memoKey) [nvmtl_memo() setObject:e forKey:memoKey cost:e.length];
            *why = [[NSString alloc] initWithData:body encoding:NSUTF8StringEncoding] ?: @"refused before";
            return nil;
        }
    }
    uint32_t local[3] = { 1, 1, 1 };
    if (kernel) { local[0] = 64; }
    struct nvmtl_air_nir_result res;
    const int st = nvmtl_air_nir_translate(f, nil, flags | NVMTL_AIR_NIR_REFLECT, local, nil, nil, &res);
    NSString *kpath = nil, *kmemo = nil;
    if (kernel) nvmtl_air_nir_key(f, nil, flags, local, nil, nil, nil, &kpath, &kmemo);
    NSDictionary *d = nil;
    NSString *no = nil;
    if (st == NVMTL_AIR_NIR_OK) {
        static NSString *const stages[3] = { @"kernel", @"vertex", @"fragment" };
        if (res.stage > 2 || ![stages[res.stage] isEqualToString:f->_stage ?: @""]) no = @"it is not the stage it was made as";
        else if (!res.reflection) no = [NSString stringWithFormat:@"the frontend does not reflect it: %s", res.reflection_why];
        else {
            d = [NSJSONSerialization JSONObjectWithData:[NSData dataWithBytesNoCopy:res.reflection length:res.reflection_size freeWhenDone:NO]
                                                options:0 error:NULL];
            if (![d isKindOfClass:[NSDictionary class]] || !d.count) { d = nil; no = @"its reflection does not parse"; }
        }
        if (kernel && res.nir) {
            void *data = NULL; size_t n = 0;
            if (!pnvk_air_nir_serialize(res.nir, &data, &n) && data && n) nvmtl_air_nir_store(kpath, kmemo, NVMTL_AIR_NIR_OK, &res, data, n);
            free(data);
        }
        if (d) {
            NSMutableData *ent = [NSMutableData dataWithBytes:"R" length:1];
            [ent appendBytes:res.reflection length:res.reflection_size];
            if (memoKey) [nvmtl_memo() setObject:ent forKey:memoKey cost:ent.length];
            if (path) { NSString *file = [path copy]; nvmtl_cache_io(^{ (void)nvmtl_cache_write(file, ent); }); }
        }
    } else {
        no = @(res.message[0] ? res.message : "the frontend said no");
        if (kernel && (st == NVMTL_AIR_NIR_UNSUPPORTED || st == NVMTL_AIR_NIR_BAD_AIR)) nvmtl_air_nir_store(kpath, kmemo, st, &res, NULL, 0);
    }
    if (no && (st == NVMTL_AIR_NIR_OK || st == NVMTL_AIR_NIR_UNSUPPORTED || st == NVMTL_AIR_NIR_BAD_AIR)) {
        NSMutableData *ent = [NSMutableData dataWithBytes:"N" length:1];
        [ent appendData:[no dataUsingEncoding:NSUTF8StringEncoding] ?: [NSData data]];
        if (memoKey) [nvmtl_memo() setObject:ent forKey:memoKey cost:ent.length];
        if (path) { NSString *file = [path copy]; nvmtl_cache_io(^{ (void)nvmtl_cache_write(file, ent); }); }
    }
    if (res.nir) pnvk_air_nir_free(res.nir);
    free(res.reflection);
    if (no) *why = no;
    return d;
}

// Every key path where the frontend's reflection (fe) differs from the translator's (tr), the frontend's keys only; the
// bindings are matched as a set, the translator's BufferAddressTable left out (nothing in the plugin reads it).
// tools/air-nir/sweep.py --reflect compares the same way.
static NSArray *nvmtl_air_nir_refl_sorted(NSArray *a)
{
    return [a sortedArrayUsingComparator:^NSComparisonResult(NSDictionary *x, NSDictionary *y) {
        NSString *(^key)(NSDictionary *) = ^NSString *(NSDictionary *b) {
            id d = [b isKindOfClass:[NSDictionary class]] ? b[@"descriptor"] : nil;
            return [NSString stringWithFormat:@"%@|%08ld|%08ld|%08ld", [b isKindOfClass:[NSDictionary class]] ? b[@"kind"] : @"",
                    [b[@"metal_index"] isKindOfClass:[NSNumber class]] ? [b[@"metal_index"] longValue] : -1L,
                    [b[@"param_index"] isKindOfClass:[NSNumber class]] ? [b[@"param_index"] longValue] : -1L,
                    [d isKindOfClass:[NSDictionary class]] && [d[@"binding"] isKindOfClass:[NSNumber class]] ? [d[@"binding"] longValue] : -1L]; };
        return [key(x) compare:key(y)]; }];
}
static void nvmtl_air_nir_refl_compare(id fe, id tr, NSString *path, NSMutableOrderedSet *out)
{
    if ([fe isKindOfClass:[NSDictionary class]]) {
        if (![tr isKindOfClass:[NSDictionary class]]) { [out addObject:path.length ? path : @"(top)"]; return; }
        for (NSString *k in fe) {
            if ([k isEqualToString:@"reflection_source"]) continue;
            nvmtl_air_nir_refl_compare(fe[k], tr[k], path.length ? [path stringByAppendingFormat:@".%@", k] : k, out);
        }
        return;
    }
    if ([fe isKindOfClass:[NSArray class]]) {
        if (![tr isKindOfClass:[NSArray class]]) { [out addObject:path]; return; }
        NSArray *a = fe, *b = tr;
        if ([path isEqualToString:@"bindings"]) {
            b = [b filteredArrayUsingPredicate:[NSPredicate predicateWithBlock:^BOOL(id o, NSDictionary *u) {
                return !([o isKindOfClass:[NSDictionary class]] && [o[@"kind"] isEqual:@"BufferAddressTable"]); }]];
            a = nvmtl_air_nir_refl_sorted(a); b = nvmtl_air_nir_refl_sorted(b);
        }
        if (a.count != b.count) { [out addObject:[path stringByAppendingString:@"#count"]]; return; }
        for (NSUInteger i = 0; i < a.count; i++) nvmtl_air_nir_refl_compare(a[i], b[i], [path stringByAppendingString:@"[]"], out);
        return;
    }
    if ([fe isKindOfClass:[NSNumber class]] && [tr isKindOfClass:[NSNumber class]]) {
        const double x = [fe doubleValue], y = [tr doubleValue];
        if (x != y && fabs(x - y) > 1e-6 * fmax(1.0, fmax(fabs(x), fabs(y)))) [out addObject:path];
        return;
    }
    if (!(fe == tr || [fe isEqual:tr])) [out addObject:path];
}

// The check run (NVMTL_AIR_NIR_REFL_CHECK=1, development builds): f was made as before; its reflection by the frontend
// against the translator's, and its default threadgroup size against its SPIR-V's, one line per function
void nvmtl_air_nir_refl_check(NVMTLFunction *f)
{
    if (!nvmtl_air_nir_refl_check_on() || !f || !f->_air.length || !nvmtl_air_nir_on()) return;
    if (![f->_stage isEqualToString:@"kernel"] && ![f->_stage isEqualToString:@"vertex"] && ![f->_stage isEqualToString:@"fragment"]) return;
    @autoreleasepool {
        NSString *why = nil;
        NSDictionary *fe = nvmtl_air_nir_function_reflection(f, &why);
        if (!fe) { nvmtl_air_nir_line("air-nir: reflection check %s: not compared, %s", f->_fname.UTF8String ?: "?", why.UTF8String ?: "?"); return; }
        char e[512] = { 0 };
        NSData *d = (nvmtl_xlate_ready() && xlate_refl) ? nvmtl_reflect_air_cached(f, f->_air, f->_stage, e, sizeof e) : nil;
        NSDictionary *tr = d ? [NSJSONSerialization JSONObjectWithData:d options:0 error:NULL] : nil;
        if (![tr isKindOfClass:[NSDictionary class]]) { nvmtl_air_nir_line("air-nir: reflection check %s: the translator gives none (%s)", f->_fname.UTF8String ?: "?", e); return; }
        NSMutableOrderedSet *diff = [NSMutableOrderedSet orderedSet];
        nvmtl_air_nir_refl_compare(fe, tr, @"", diff);
        NSString *local = @"";
        uint32_t s[3];
        NSArray *ls = fe[@"local_size"];
        if ([f->_stage isEqualToString:@"kernel"] && [ls isKindOfClass:[NSArray class]] && ls.count == 3) {
            const int seeded = f->_spirv.length >= 20 && nvmtl_cvar_seed_size((const uint32_t *)f->_spirv.bytes, f->_spirv.length / 4, s);
            if (!seeded || s[0] != [ls[0] unsignedIntValue] || s[1] != [ls[1] unsignedIntValue] || s[2] != [ls[2] unsignedIntValue])
                local = seeded ? [NSString stringWithFormat:@"; SPIR-V default size %ux%ux%u, the frontend's %@x%@x%@", s[0], s[1], s[2], ls[0], ls[1], ls[2]]
                               : @"; the SPIR-V has no default size (its creation pipeline is 1x1x1)";
        }
        nvmtl_air_nir_line("air-nir: reflection check %s (%s): %s%s%s", f->_fname.UTF8String ?: "?", f->_stage.UTF8String ?: "?",
                           diff.count ? "DIFFERENT at " : "equal", diff.count ? [diff.array componentsJoinedByString:@" "].UTF8String : "",
                           local.UTF8String);
    }
}

// Whether newFunctionWithName: leaves the translator out of f (see above). YES: f has the frontend's reflection and its
// SPIR-V is made when something needs it.
BOOL nvmtl_air_nir_lazy(NVMTLFunction *f)
{
    if (!f || !f->_air.length || nvmtl_air_nir_refl_check_on() || g_app_profile || getenv("NVMTL_BDA_PROBE_FILE")) return NO;
    if (![f->_stage isEqualToString:@"kernel"] && ![f->_stage isEqualToString:@"vertex"] && ![f->_stage isEqualToString:@"fragment"]) return NO;
    // OpenCL's (AMOGL): a by-value constant argument or an __entry_ wrapper, deferred to newFunctionWithPluginData as before
    if ([f->_fname hasPrefix:@"__entry_"] || [f->_air rangeOfString:@"!\"air.constant\"" options:NSLiteralSearch].location != NSNotFound) return NO;
    if (!nvmtl_air_nir_on() || !g_air_nir_v5) return NO;
    const uint64_t t0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
    NSString *why = nil;
    NSDictionary *d;
    @autoreleasepool { d = nvmtl_air_nir_function_reflection(f, &why); }
    const uint64_t ns = clock_gettime_nsec_np(CLOCK_UPTIME_RAW) - t0;
    if (!d) {
        nvmtl_air_nir_line("air-nir: %s %s is made with the translator: %s", f->_stage.UTF8String ?: "?", f->_fname.UTF8String ?: "?",
                           why.UTF8String ?: "?");
        return NO;
    }
    f->_spirv = nil; f->_spirvLazy = YES; f->_nrefl = d; f->_refl = d;
    static dispatch_once_t once;
    dispatch_once(&once, ^{ atexit(nvmtl_air_nir_lazy_report); });
    atomic_fetch_add(&g_lazy_ns, ns);
    const unsigned long long n = atomic_fetch_add(&g_lazy_made, 1) + 1;
    if (!(n & (n - 1)))
        nvmtl_air_nir_line("air-nir: %llu function(s) made without the translator so far (%s %s: %.2f ms)", n, f->_stage.UTF8String ?: "?",
                           f->_fname.UTF8String ?: "?", (double)ns / 1e6);
    return YES;
}

// f's SPIR-V: made now, the first time something needs it, when newFunctionWithName: left the translator out, exactly as it
// would have made it (nvmtlEntrySpirv, or nvmtl_translate_with_fc_cached with the constants); why says what needs it
NSData *nvmtl_function_spirv(NVMTLFunction *f, const char *why)
{
    if (!f) return nil;
    @synchronized (f) {
        if (!f->_spirvLazy) return f->_spirv;
        const uint64_t t0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
        NSData *spv = nil; NSString *e = nil;
        if (f->_fc.count) {
            NSArray *keys = [f->_fc.allKeys sortedArrayUsingComparator:^NSComparisonResult(NSString *a, NSString *b) {
                return a.integerValue < b.integerValue ? NSOrderedAscending : a.integerValue > b.integerValue ? NSOrderedDescending : NSOrderedSame; }];
            const NSUInteger n = keys.count;
            NSMutableData *packed = [NSMutableData dataWithLength:n * (2 * sizeof(uint32_t) + sizeof(uint8_t *))];
            NSMutableData *bytes = [NSMutableData data];
            uint32_t *idx = packed.mutableBytes, *sz = idx + n;
            const uint8_t **pay = (const uint8_t **)(sz + n);
            NSMutableArray *offs = [NSMutableArray arrayWithCapacity:n];
            for (NSUInteger k = 0; k < n; k++) {
                NSArray *a = f->_fc[keys[k]];
                idx[k] = (uint32_t)[keys[k] integerValue]; sz[k] = (uint32_t)a.count; [offs addObject:@(bytes.length)];
                for (NSNumber *b in a) { const uint8_t v = (uint8_t)b.unsignedCharValue; [bytes appendBytes:&v length:1]; }
            }
            for (NSUInteger k = 0; k < n; k++) pay[k] = (const uint8_t *)bytes.bytes + [offs[k] unsignedLongValue];
            nvmtl_fc_expect_link(1);
            spv = nvmtl_translate_with_fc_cached(f->_air, f->_stage, f->_fname, f->_fc, idx, sz, pay, n, &e);
            nvmtl_fc_expect_link(0);
        } else if ([f->_lib isKindOfClass:[NVMTLLibrary class]]) {
            NSError *err = nil;
            spv = [(NVMTLLibrary *)f->_lib nvmtlEntrySpirv:f->_fname error:&err];
            e = err.localizedDescription;
        } else e = @"the function keeps no library to translate it from";
        f->_spirv = spv; f->_spirvLazy = NO;
        const uint64_t ns = clock_gettime_nsec_np(CLOCK_UPTIME_RAW) - t0;
        atomic_fetch_add(&g_lazy_spirv, 1); atomic_fetch_add(&g_lazy_spirv_ns, ns);
        if (spv) nvmtl_air_nir_line("air-nir: %s %s translated to SPIR-V now (%.2f ms): %s", f->_stage.UTF8String ?: "?",
                                    f->_fname.UTF8String ?: "?", (double)ns / 1e6, why ?: "?");
        else nvlog("air-nir: %s %s does NOT translate to SPIR-V (%s), which %s needed: %s", f->_stage.UTF8String ?: "?",
                   f->_fname.UTF8String ?: "?", e.UTF8String ?: "?", why ?: "?", "its pipeline fails as newFunctionWithName: would have");
        return f->_spirv;
    }
}

// The stages of a render pipeline on the road for a draw that binds pixel-coordinate samplers (vS, fS: slot to the
// translator's state names): copies of vf and ff whose requests carry the samplers' state words, which only the road
// makes (_roadOnly). nil and *why when a state cannot be carried, or when no stage has AIR.
NSArray *nvmtl_air_nir_sampler_stages(NVMTLFunction *vf, NVMTLFunction *ff, NSDictionary *vS, NSDictionary *fS, NSString **why)
{
    if (!vf || !ff || !vf->_air.length || !ff->_air.length) { if (why) *why = @"a stage has no AIR"; return nil; }
    if (!nvmtl_air_nir_on() || !g_air_nir_v5) { if (why) *why = @"this NVK's frontend takes no sampler state (older than version 5)"; return nil; }
    NSDictionary *vw = vS.count ? nvmtl_air_nir_sampler_words_all(vS, why) : @{}, *fw = fS.count ? nvmtl_air_nir_sampler_words_all(fS, why) : @{};
    if (!vw || !fw) return nil;
    NVMTLFunction *(^copy)(NVMTLFunction *, NSDictionary *) = ^NVMTLFunction *(NVMTLFunction *s, NSDictionary *w) {
        NVMTLFunction *c = [NVMTLFunction new];
        c->_spirv = s->_spirv; c->_spirvLazy = NO; c->_fname = s->_fname; c->_stage = s->_stage; c->_air = s->_air; c->_fc = s->_fc;
        c->_lib = s->_lib; c->_fcv = s->_fcv; c->_refl = s->_refl; c->_nrefl = s->_nrefl; c->_airNames = s->_airNames;
        c->_arguments = s->_arguments; c->_embedded = s->_embedded; c->_specName = s->_specName;
        c->_samplerStates = w.count ? w : nil; c->_roadOnly = YES;
        return c; };
    return @[ copy(vf, vw), copy(ff, fw) ];
}
