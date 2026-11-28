/*
 * NullMoth NVIDIA driver for macOS
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 *
 * airsweep NVK.dylib LLVM.dylib LIST: every compute kernel of the AIR files named in LIST (one path per line: text IR
 * as the plugin's AIR cache keeps it, .ll, or bitcode, .air or .bc) through NVK's AIR-to-NIR export and
 * vkCreateComputePipelines with the plugin's layout, as NAK compiles it. Nothing is dispatched (macOS only).
 *
 * Text IR is parsed with the LLVM library named (the plugin's air-runtime, which wrote the cache) and handed to the
 * export as bitcode it writes. Each kernel of a module is translated on its own (request.entry), for threadgroups of
 * 64 threads, with 1024 bytes for each [[threadgroup(n)]] argument.
 *
 * A line of LIST may name one kernel and its function-constant values: "path<TAB>entry<TAB>index=hexbytes,index=hexbytes"
 * (the request then carries them, NVMTL_AIR_NIR_FC_VALUES, as the plugin would for that function); a line with only a
 * path translates every kernel of the file without function-constant values.
 *
 * One line per kernel on stdout, tab-separated: "BEGIN<TAB>file" before a file is read (so a caller can tell which file
 * a crash was in), then "<status><TAB>file<TAB>kernel<TAB>message", status OK, REFUSED<n> or PIPELINE<vkresult>, and
 * "NOTEXT<TAB>file<TAB>-<TAB>reason" for a file that does not parse. tools/air-nir/sweep.py runs it and counts.
 *
 * airsweep --reflect NVK.dylib LLVM.dylib TRANSLATOR.dylib LIST (round 5): every kernel, vertex and fragment function
 * of the files through the frontend as the plugin asks it at newFunctionWithName: (the text IR, its function-constant
 * values, NVMTL_AIR_NIR_REFLECT; translation only, no pipeline), and through the translator's reflection
 * (nvmtl_reflect, or nvmtl_reflect_fc with the values), one line each:
 * "RFL<TAB>status<TAB>file<TAB>function<TAB>stage<TAB>message<TAB>frontend JSON or -<TAB>translator JSON or ERR:why",
 * status the frontend's (0 translated). sweep.py --reflect compares the two.
 *
 * Round 9: a function the frontend refuses for want of the buffers the translator keeps as storage-buffer descriptors
 * (result.descriptor_wanted) is asked again as the plugin asks it (NVMTLAirNir.m nvmtl_air_nir_shader): the translator's
 * module (nvmtl_translate or nvmtl_translate_fc, the plugin's capabilities), lowered by nvmtl_lower_buffer_addresses, and
 * its set-0 storage buffers below 32 (nvmtl_air_nir_kept_buffers) handed over with NVMTL_AIR_NIR_DESCRIPTORS; a line
 * "DSC<TAB>file<TAB>function<TAB>stage<TAB>wanted<TAB>kept or ERR<TAB>status" comes before its RFL line, which is then
 * the second answer's. AIRSWEEP_KEPT_ALL=1 does the same for every vertex and fragment function the frontend
 * translates without asking, so a function the translator keeps a buffer of that the frontend did not ask about shows
 * (DSC with wanted 0 and kept not 0).
 */
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan_core.h>

#include <dlfcn.h>
#include <stdbool.h>
#include <unistd.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "nvmtl_air_nir_abi.h"
#include "nvmtl_air_nir.h"   /* plugin/: nvmtl_air_nir_kept_buffers */

#define STYPE_NIR ((VkStructureType)1000290001)
typedef struct {
   VkStructureType sType;
   const void *pNext;
   void *nir;
} NirInfo;

typedef struct sweep_ctx *Ctx;
typedef struct sweep_mod *Mod;
typedef struct sweep_val *Val;
typedef struct sweep_buf *Buf;
static Ctx (*ContextCreate)(void);
static void (*ContextDispose)(Ctx);
static Buf (*BufferCopy)(const char *, size_t, const char *);
static int (*ParseIR)(Ctx, Buf, Mod *, char **);
static int (*ParseBitcode)(Ctx, Buf, Mod *);
static Buf (*WriteBitcode)(Mod);
static const char *(*BufferStart)(Buf);
static size_t (*BufferSize)(Buf);
static void (*DisposeBuffer)(Buf);
static void (*DisposeModule)(Mod);
static void (*DisposeMessage)(char *);
static unsigned (*NamedMDCount)(Mod, const char *);
static void (*NamedMDOps)(Mod, const char *, Val *);
static unsigned (*MDCount)(Val);
static void (*MDOps)(Val, Val *);
static Val (*IsAFunction)(Val);
static const char *(*ValueName)(Val, size_t *);
static void (*SetDiagHandler)(Ctx, void (*)(void *, void *), void *);

static void
quiet(void *info, void *data)
{
   (void)info;
   (void)data;
}

static void *
slurp(const char *p, size_t *n)
{
   FILE *f = fopen(p, "rb");
   if (!f)
      return NULL;
   fseek(f, 0, SEEK_END);
   long len = ftell(f);
   fseek(f, 0, SEEK_SET);
   char *b = len > 0 ? malloc((size_t)len + 1) : NULL;
   if (b && fread(b, 1, (size_t)len, f) != (size_t)len) {
      free(b);
      b = NULL;
   }
   fclose(f);
   if (b)
      b[len] = 0;
   *n = b ? (size_t)len : 0;
   return b;
}

static void
clean(char *s)
{
   for (; *s; s++) {
      if (*s == '\t' || *s == '\n' || *s == '\r')
         *s = ' ';
   }
}

/* The set layout the plugin's would hold for what the kernel reads: binding 640 and each texture, storage texture and
 * sampler binding the result's masks name, with the descriptor types the plugin's set layout gives those bands. */
static PFN_vkCreateDescriptorSetLayout cdsl;
static PFN_vkCreatePipelineLayout cpl;
static PFN_vkDestroyDescriptorSetLayout ddsl;
static PFN_vkDestroyPipelineLayout dpl;

static VkPipelineLayout
layout_for(VkDevice dev, const struct nvmtl_air_nir_result *res, VkDescriptorSetLayout *sl)
{
   VkDescriptorSetLayoutBinding b[300];
   unsigned n = 0;
   b[n++] = (VkDescriptorSetLayoutBinding){ NVMTL_AIR_NIR_TABLE_BINDING, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC, 1,
                                            VK_SHADER_STAGE_COMPUTE_BIT, NULL };
   for (unsigned i = 0; i < 128; i++) {
      if ((res->texture_mask[i / 32] >> (i % 32)) & 1)
         b[n++] = (VkDescriptorSetLayoutBinding){ 32 + i, ((res->texbuf_mask[i / 32] >> (i % 32)) & 1) ?
                                                  VK_DESCRIPTOR_TYPE_UNIFORM_TEXEL_BUFFER : VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE,
                                                  1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
      if ((res->storage_mask[i / 32] >> (i % 32)) & 1)
         b[n++] = (VkDescriptorSetLayoutBinding){ 480 + i, ((res->stexbuf_mask[i / 32] >> (i % 32)) & 1) ?
                                                  VK_DESCRIPTOR_TYPE_STORAGE_TEXEL_BUFFER : VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,
                                                  1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
   }
   for (unsigned i = 0; i < 32; i++) {
      if ((res->sampler_mask >> i) & 1)
         b[n++] = (VkDescriptorSetLayoutBinding){ 160 + i, VK_DESCRIPTOR_TYPE_SAMPLER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
   }
   VkDescriptorSetLayoutCreateInfo dl = { VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO, .bindingCount = n, .pBindings = b };
   cdsl(dev, &dl, NULL, sl);
   VkPushConstantRange pr = { VK_SHADER_STAGE_COMPUTE_BIT, 0, NVMTL_AIR_NIR_PUSH_BYTES };
   VkPipelineLayoutCreateInfo pl = { VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, .setLayoutCount = 1,
                                     .pSetLayouts = sl, .pushConstantRangeCount = 1, .pPushConstantRanges = &pr };
   VkPipelineLayout layout;
   cpl(dev, &pl, NULL, &layout);
   return layout;
}

typedef int (*refl_fn)(const char *, const char *, uint8_t **, size_t *, char *, size_t);
typedef int (*refl_fc_fn)(const char *, const char *, const uint32_t *, const uint32_t *, const uint8_t *const *, size_t,
                          uint8_t **, size_t *, char *, size_t);
typedef void (*xfree_fn)(uint8_t *, size_t);
typedef int (*lower_fn)(const uint8_t *, size_t, uint8_t **, size_t *, char *, size_t);
static refl_fn xrefl, xtrans;
static refl_fc_fn xrefl_fc, xtrans_fc;
static lower_fn xlower;
static xfree_fn xfree;

/* the buffers the translator's module of the function keeps as storage-buffer descriptors once lowered, as the plugin
 * finds them (nvmtl_vk_descriptor_buffers): 0, or -1 and why */
static int
kept_buffers(const char *text, const char *stage, const uint32_t *fi, const uint32_t *fs, const uint8_t *const *fp, size_t nfc,
             uint32_t *mask, uint8_t bits[32], uint32_t *guarded, char *e, size_t en)
{
   uint8_t *spv = NULL, *low = NULL;
   size_t n = 0, ln = 0;
   *mask = 0;
   if (!xtrans || !xtrans_fc || !xlower) {
      snprintf(e, en, "the translator has no nvmtl_translate or nvmtl_lower_buffer_addresses");
      return -1;
   }
   if (nfc ? xtrans_fc(text, stage, fi, fs, fp, nfc, &spv, &n, e, en) : xtrans(text, stage, &spv, &n, e, en))
      return -1;
   int rc = xlower(spv, n, &low, &ln, e, en) ? -1 : 0;
   if (!rc && (nvmtl_air_nir_kept_buffers((const uint32_t *)low, ln / 4, mask) ||
               nvmtl_air_nir_kept_forms((const uint32_t *)low, ln / 4, bits, guarded))) {
      snprintf(e, en, "the lowered module has a buffer of another form");
      rc = -1;
   }
   if (xfree) {
      xfree(spv, n);
      if (low)
         xfree(low, ln);
   }
   return rc;
}

int
main(int argc, char **argv)
{
   const bool reflect = argc == 6 && !strcmp(argv[1], "--reflect");
   if (reflect) {
      void *x = dlopen(argv[4], RTLD_NOW | RTLD_LOCAL);
      xrefl = x ? (refl_fn)dlsym(x, "nvmtl_reflect") : NULL;
      xrefl_fc = x ? (refl_fc_fn)dlsym(x, "nvmtl_reflect_fc") : NULL;
      xfree = x ? (xfree_fn)dlsym(x, "nvmtl_translate_free") : NULL;
      xtrans = x ? (refl_fn)dlsym(x, "nvmtl_translate") : NULL;
      xtrans_fc = x ? (refl_fc_fn)dlsym(x, "nvmtl_translate_fc") : NULL;
      xlower = x ? (lower_fn)dlsym(x, "nvmtl_lower_buffer_addresses") : NULL;
      void (*caps)(uint32_t) = x ? (void (*)(uint32_t))dlsym(x, "nvmtl_translate_set_caps") : NULL;
      if (caps)
         caps(3u | 4u | 8u | 16u);   /* as the plugin sets them (NVMTLLibraryLoad.m), typed dummies on, the device's
                                       * minimum-LOD clamp (this machine's has it) */
      if (!xrefl || !xrefl_fc) {
         fprintf(stderr, "airsweep: %s has no nvmtl_reflect\n", argv[4]);
         return 2;
      }
   }
   if (!reflect && argc != 4) {
      fprintf(stderr, "usage: airsweep NVK.dylib LLVM.dylib LIST | airsweep --reflect NVK.dylib LLVM.dylib TRANSLATOR.dylib LIST\n");
      return 2;
   }
   const char *nvk_path = argv[reflect ? 2 : 1], *llvm_path = argv[reflect ? 3 : 2], *list_path = argv[reflect ? 5 : 3];
   void *lib = dlopen(nvk_path, RTLD_NOW | RTLD_LOCAL);
   void *llvm = dlopen(llvm_path, RTLD_NOW | RTLD_LOCAL);
   if (!lib || !llvm) {
      fprintf(stderr, "airsweep: %s\n", dlerror());
      return 2;
   }
#define SYM(var, name)                                                                                           \
   *(void **)&var = dlsym(llvm, name);                                                                           \
   if (!var) {                                                                                                   \
      fprintf(stderr, "airsweep: %s has no %s\n", llvm_path, name);                                              \
      return 2;                                                                                                  \
   }
   SYM(ContextCreate, "LLVMContextCreate");
   SYM(ContextDispose, "LLVMContextDispose");
   SYM(BufferCopy, "LLVMCreateMemoryBufferWithMemoryRangeCopy");
   SYM(ParseIR, "LLVMParseIRInContext");
   SYM(ParseBitcode, "LLVMParseBitcodeInContext2");
   SYM(WriteBitcode, "LLVMWriteBitcodeToMemoryBuffer");
   SYM(BufferStart, "LLVMGetBufferStart");
   SYM(BufferSize, "LLVMGetBufferSize");
   SYM(DisposeBuffer, "LLVMDisposeMemoryBuffer");
   SYM(DisposeModule, "LLVMDisposeModule");
   SYM(DisposeMessage, "LLVMDisposeMessage");
   SYM(NamedMDCount, "LLVMGetNamedMetadataNumOperands");
   SYM(NamedMDOps, "LLVMGetNamedMetadataOperands");
   SYM(MDCount, "LLVMGetMDNodeNumOperands");
   SYM(MDOps, "LLVMGetMDNodeOperands");
   SYM(IsAFunction, "LLVMIsAFunction");
   SYM(ValueName, "LLVMGetValueName2");
   SYM(SetDiagHandler, "LLVMContextSetDiagnosticHandler");
#undef SYM

   PFN_vkGetInstanceProcAddr gipa = (PFN_vkGetInstanceProcAddr)dlsym(lib, "vk_icdGetInstanceProcAddr");
   nvmtl_air_to_nir_v1_fn to_nir = (nvmtl_air_to_nir_v1_fn)dlsym(lib, "nvmtl_air_to_nir_v1");
   nvmtl_air_nir_free_v1_fn free_nir = (nvmtl_air_nir_free_v1_fn)dlsym(lib, "nvmtl_air_nir_free_v1");
   if (!gipa || !to_nir || !free_nir) {
      fprintf(stderr, "airsweep: %s lacks the exports\n", nvk_path);
      return 2;
   }
   PFN_vkCreateInstance create_instance = (PFN_vkCreateInstance)gipa(NULL, "vkCreateInstance");
   VkApplicationInfo app = { VK_STRUCTURE_TYPE_APPLICATION_INFO, .pApplicationName = "airsweep",
                             .apiVersion = VK_API_VERSION_1_3 };
   VkInstanceCreateInfo ici = { VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app };
   VkInstance inst;
   if (create_instance(&ici, NULL, &inst) != VK_SUCCESS)
      return 2;
   PFN_vkEnumeratePhysicalDevices enumerate = (PFN_vkEnumeratePhysicalDevices)gipa(inst, "vkEnumeratePhysicalDevices");
   PFN_vkCreateDevice create_device = (PFN_vkCreateDevice)gipa(inst, "vkCreateDevice");
   PFN_vkGetDeviceProcAddr gdpa = (PFN_vkGetDeviceProcAddr)gipa(inst, "vkGetDeviceProcAddr");
   uint32_t count = 1;
   VkPhysicalDevice pd;
   enumerate(inst, &count, &pd);
   if (!count)
      return 2;
   float prio = 1;
   VkDeviceQueueCreateInfo qci = { VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = 0, .queueCount = 1,
                                   .pQueuePriorities = &prio };
   VkDeviceCreateInfo dci = { VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1, .pQueueCreateInfos = &qci };
   VkDevice dev;
   if (create_device(pd, &dci, NULL, &dev) != VK_SUCCESS)
      return 2;
   cdsl = (PFN_vkCreateDescriptorSetLayout)gdpa(dev, "vkCreateDescriptorSetLayout");
   cpl = (PFN_vkCreatePipelineLayout)gdpa(dev, "vkCreatePipelineLayout");
   ddsl = (PFN_vkDestroyDescriptorSetLayout)gdpa(dev, "vkDestroyDescriptorSetLayout");
   dpl = (PFN_vkDestroyPipelineLayout)gdpa(dev, "vkDestroyPipelineLayout");
   PFN_vkCreateComputePipelines ccp = (PFN_vkCreateComputePipelines)gdpa(dev, "vkCreateComputePipelines");
   PFN_vkDestroyPipeline dp = (PFN_vkDestroyPipeline)gdpa(dev, "vkDestroyPipeline");
   PFN_vkDestroyDevice dd = (PFN_vkDestroyDevice)gdpa(dev, "vkDestroyDevice");

   FILE *list = fopen(list_path, "r");
   if (!list)
      return 2;
   char path[16384];
   while (fgets(path, sizeof path, list)) {
      path[strcspn(path, "\r\n")] = 0;
      if (!path[0])
         continue;
      /* path [TAB entry [TAB index=hex,...]] */
      char *only = NULL, *fcs = NULL;
      if ((only = strchr(path, '\t'))) {
         *only++ = 0;
         if ((fcs = strchr(only, '\t')))
            *fcs++ = 0;
      }
      static uint32_t fc_idx[1024], fc_sz[1024];
      static uint8_t fc_bytes[1024][64];
      static const uint8_t *fc_pay[1024];
      uint32_t nfc = 0;
      for (char *tok = fcs ? strtok(fcs, ",") : NULL; tok && nfc < 1024; tok = strtok(NULL, ",")) {
         char *eq = strchr(tok, '=');
         if (!eq)
            continue;
         fc_idx[nfc] = (uint32_t)strtoul(tok, NULL, 10);
         size_t len = strlen(eq + 1) / 2;
         if (len > 64)
            len = 64;
         for (size_t k = 0; k < len; k++) {
            unsigned v = 0;
            sscanf(eq + 1 + 2 * k, "%2x", &v);
            fc_bytes[nfc][k] = (uint8_t)v;
         }
         fc_sz[nfc] = (uint32_t)len;
         fc_pay[nfc] = fc_bytes[nfc];
         nfc++;
      }
      printf("BEGIN\t%s\n", path);
      fflush(stdout);
      size_t size;
      char *data = slurp(path, &size);
      if (!data) {
         printf("NOTEXT\t%s\t-\tcannot read\n", path);
         continue;
      }
      const size_t n = strlen(path);
      bool text = n > 3 && !strcmp(path + n - 3, ".ll");
      Ctx ctx = ContextCreate();
      SetDiagHandler(ctx, quiet, NULL);
      Mod mod = NULL;
      char *err = NULL;
      Buf in = BufferCopy(data, size, "air");   /* LLVM's copy ends in the 0 its text parser wants */
      if (text ? ParseIR(ctx, in, &mod, &err) : ParseBitcode(ctx, in, &mod)) {
         char why[200];
         snprintf(why, sizeof why, "%s", err ? err : "does not parse");
         clean(why);
         printf("NOTEXT\t%s\t-\t%s\n", path, why);
         if (text && only) {
            /* a text this LLVM refuses whose list line names its kernel: the frontend's own verdict on the text, as
             * the plugin hands it over (NVMTL_AIR_NIR_TEXT_IR), no pipeline (round 6: a global defined twice) */
            struct nvmtl_air_nir_request req = { sizeof req, NVMTL_AIR_NIR_TEXT_IR, llvm_path, data, size, only,
                                                 { 64, 1, 1 } };
            struct nvmtl_air_nir_result res = { sizeof res };
            int st = to_nir(dev, &req, &res);
            clean(res.message);
            printf("%s%d\t%s\t%s\t%s\n", st ? "TEXTREFUSED" : "TEXTOK", st, path, only, res.message);
            if (res.nir)
               free_nir(res.nir);
         }
         if (err)
            DisposeMessage(err);
         if (!text)
            DisposeBuffer(in);
         ContextDispose(ctx);
         free(data);
         continue;
      }
      if (!text)
         DisposeBuffer(in);   /* LLVMParseIRInContext took its buffer; the bitcode reader did not */
      if (reflect) {
         static const char *const lists[3][2] = { { "air.kernel", "kernel" }, { "air.vertex", "vertex" },
                                                  { "air.fragment", "fragment" } };
         for (unsigned l = 0; l < 3; l++) {
            unsigned nf = NamedMDCount(mod, lists[l][0]);
            Val *fns = calloc(nf + 1, sizeof *fns);
            NamedMDOps(mod, lists[l][0], fns);
            for (unsigned k = 0; k < nf; k++) {
               unsigned c = MDCount(fns[k]);
               Val *ops = calloc(c + 1, sizeof *ops);
               MDOps(fns[k], ops);
               if (!c || !ops[0] || !IsAFunction(ops[0])) {
                  free(ops);
                  continue;
               }
               size_t len = 0;
               const char *nm = ValueName(ops[0], &len);
               char name[512];
               snprintf(name, sizeof name, "%.*s", (int)len, nm ? nm : "");
               free(ops);
               if (only && strcmp(name, only))
                  continue;
               /* a function the translator's reflection or the frontend works on for over 60 s ends the process
                * (SIGALRM): sweep.py records the file as a crash and goes on after it */
               alarm(60);
               /* as the plugin asks at newFunctionWithName: (NVMTLAirNir.m nvmtl_air_nir_flags, no varyings), with a
                * version-9 NVK and the bindless heap with its typed dummies: [[color(n)]] inputs, arrays of textures
                * (round 8), argument buffers (round 9), a vertex function's post-tessellation form (round 10), null
                * textures (round 11) */
               struct nvmtl_air_nir_request req = { sizeof req, NVMTL_AIR_NIR_TEXT_IR | NVMTL_AIR_NIR_TG_DEFAULT |
                                                    NVMTL_AIR_NIR_REFLECT | NVMTL_AIR_NIR_COLOR_INPUTS |
                                                    NVMTL_AIR_NIR_TEXTURE_ARRAYS | NVMTL_AIR_NIR_HEAP |
                                                    NVMTL_AIR_NIR_ARGBUF | NVMTL_AIR_NIR_TYPED_DUMMIES |
                                                    (l > 0 ? NVMTL_AIR_NIR_TESS : 0) | NVMTL_AIR_NIR_MIN_LOD |
                                                    NVMTL_AIR_NIR_NULL_TEXTURES |
                                                    /* round 12: kept buffers the frontend is sure of (AIRSWEEP_NO_KEPT_SELF=1:
                                                     * always the translator's module, as before) */
                                                    (getenv("AIRSWEEP_NO_KEPT_SELF") ? 0 : NVMTL_AIR_NIR_KEPT_SELF) |
                                                    (fcs ? NVMTL_AIR_NIR_FC_VALUES : 0) |
                                                    /* a kernel as the plugin's compute pipelines are made by default,
                                                     * without buffer robustness (AIRSWEEP_ROBUST_COMPUTE=1: with it) */
                                                    (l == 0 && !getenv("AIRSWEEP_ROBUST_COMPUTE") ? NVMTL_AIR_NIR_UNCHECKED : 0),
                                                    llvm_path, data, size, name, { 64, 1, 1 } };
               if (fcs) {
                  req.fc_count = nfc;
                  req.fc_indices = fc_idx;
                  req.fc_sizes = fc_sz;
                  req.fc_payloads = fc_pay;
               }
               struct nvmtl_air_nir_result res = { sizeof res };
               int st = to_nir(dev, &req, &res);
               if (res.nir)
                  free_nir(res.nir);
               const bool render = l > 0;
               /* round 12: kept buffers the frontend decided itself, against the translator's module as the plugin lowers
                * it: "KSELF<TAB>file<TAB>function<TAB>stage<TAB>frontend's<TAB>translator's (mask, then each kept one's
                * elem bits, guarded)<TAB>SAME|DIFFERENT|ERR:why" */
               if (!st && (res.resource_flags & NVMTL_AIR_NIR_RES_KEPT_SELF)) {
                  uint32_t kept = 0, guarded = 0;
                  uint8_t bits[32] = { 0 };
                  char ke[512] = { 0 };
                  const int k = kept_buffers(data, lists[l][1], fc_idx, fc_sz, (const uint8_t *const *)fc_pay, fcs ? nfc : 0,
                                             &kept, bits, &guarded, ke, sizeof ke);
                  bool same = !k && kept == res.descriptor_mask && !guarded;
                  for (unsigned b = 0; same && b < 32; b++)
                     same = !(kept >> b & 1) || bits[b] == 32;
                  clean(name);
                  clean(ke);
                  printf("KSELF\t%s\t%s\t%s\t%#x\t%#x", path, name, lists[l][1], res.descriptor_mask, kept);
                  for (unsigned b = 0; !k && b < 32; b++)
                     if (kept >> b & 1)
                        printf(" %u:%u", b, bits[b]);
                  printf(" g%#x\t%s%s\n", guarded, k ? "ERR:" : same ? "SAME" : "DIFFERENT", k ? ke : "");
               }
               if ((st && res.descriptor_wanted) ||
                   (!st && render && getenv("AIRSWEEP_KEPT_ALL") && !(res.resource_flags & NVMTL_AIR_NIR_RES_KEPT_SELF))) {
                  const uint32_t wanted = st ? res.descriptor_wanted : 0;
                  uint32_t kept = 0, guarded = 0;
                  uint8_t bits[32];
                  char ke[512] = { 0 };
                  const int k = kept_buffers(data, lists[l][1], fc_idx, fc_sz, (const uint8_t *const *)fc_pay, fcs ? nfc : 0,
                                             &kept, bits, &guarded, ke, sizeof ke);
                  clean(name);
                  clean(ke);
                  printf("DSC\t%s\t%s\t%s\t%#x\t", path, name, lists[l][1], wanted);
                  if (k)
                     printf("ERR:%s\t%d\n", ke, st);
                  else if (!st)
                     printf("%#x\t%d\n", kept, st);
                  else {
                     free(res.reflection);
                     req.flags |= NVMTL_AIR_NIR_DESCRIPTORS;
                     req.descriptor_buffers = kept;
                     memcpy(req.descriptor_elem_bits, bits, sizeof req.descriptor_elem_bits);
                     req.descriptor_store_guarded = guarded;
                     memset(&res, 0, sizeof res);
                     res.size = sizeof res;
                     st = to_nir(dev, &req, &res);
                     if (res.nir)
                        free_nir(res.nir);
                     printf("%#x\t%d\n", kept, st);
                  }
               }
               uint8_t *j = NULL;
               size_t jn = 0;
               char e[512] = { 0 };
               int rc = fcs ? xrefl_fc(data, lists[l][1], fc_idx, fc_sz, fc_pay, nfc, &j, &jn, e, sizeof e)
                            : xrefl(data, lists[l][1], &j, &jn, e, sizeof e);
               clean(res.message);
               clean(res.reflection_why);
               clean(e);
               clean(name);
               printf("RFL\t%d\t%s\t%s\t%s\t%s\t", st, path, name, lists[l][1],
                      st || getenv("AIRSWEEP_MESSAGES") ? res.message : res.reflection ? "-" : res.reflection_why);
               if (res.reflection)
                  fwrite(res.reflection, 1, res.reflection_size, stdout);
               else
                  fputs("-", stdout);
               fputs("\t", stdout);
               if (rc == 0 && j) {
                  for (size_t q = 0; q < jn; q++)
                     putchar(j[q] == '\t' || j[q] == '\n' ? ' ' : j[q]);
               } else
                  printf("ERR:%s", e[0] ? e : "?");
               putchar('\n');
               free(res.reflection);
               if (j && xfree)
                  xfree(j, jn);
               alarm(0);
            }
            free(fns);
         }
         fflush(stdout);
         DisposeModule(mod);
         ContextDispose(ctx);
         free(data);
         continue;
      }
      Buf bc = WriteBitcode(mod);
      unsigned kernels = NamedMDCount(mod, "air.kernel");
      Val *nodes = calloc(kernels + 1, sizeof *nodes);
      NamedMDOps(mod, "air.kernel", nodes);
      for (unsigned k = 0; k < kernels; k++) {
         unsigned c = MDCount(nodes[k]);
         Val *ops = calloc(c + 1, sizeof *ops);
         MDOps(nodes[k], ops);
         if (!c || !ops[0] || !IsAFunction(ops[0])) {
            free(ops);
            continue;
         }
         size_t len = 0;
         const char *nm = ValueName(ops[0], &len);
         char name[512];
         snprintf(name, sizeof name, "%.*s", (int)len, nm ? nm : "");
         free(ops);
         if (only && strcmp(name, only))
            continue;
         /* arrays of textures by known index (an element chosen at run time needs the heap, which this layout has not) */
         struct nvmtl_air_nir_request req = { sizeof req, NVMTL_AIR_NIR_TEXTURE_ARRAYS, llvm_path, BufferStart(bc),
                                              BufferSize(bc), name, { 64, 1, 1 } };
         for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++)
            req.threadgroup_lengths[i] = 1024;
         if (fcs) {
            req.flags |= NVMTL_AIR_NIR_FC_VALUES;
            req.fc_count = nfc;
            req.fc_indices = fc_idx;
            req.fc_sizes = fc_sz;
            req.fc_payloads = fc_pay;
         }
         struct nvmtl_air_nir_result res = { sizeof res };
         int st = to_nir(dev, &req, &res);
         clean(res.message);
         clean(name);
         if (st) {
            printf("REFUSED%d\t%s\t%s\t%s\n", st, path, name, res.message);
            continue;
         }
         VkDescriptorSetLayout sl;
         VkPipelineLayout layout = layout_for(dev, &res, &sl);
         NirInfo ni = { STYPE_NIR, NULL, res.nir };
         VkComputePipelineCreateInfo cpi = {
            VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
            .stage = { VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, &ni, 0, VK_SHADER_STAGE_COMPUTE_BIT,
                       VK_NULL_HANDLE, "main", NULL },
            .layout = layout,
         };
         VkPipeline pipe;
         VkResult r = ccp(dev, VK_NULL_HANDLE, 1, &cpi, NULL, &pipe);
         free_nir(res.nir);
         dpl(dev, layout, NULL);
         ddsl(dev, sl, NULL);
         if (r != VK_SUCCESS) {
            printf("PIPELINE%d\t%s\t%s\t%s\n", r, path, name, res.message);
            continue;
         }
         dp(dev, pipe, NULL);
         printf("OK\t%s\t%s\t%s\n", path, name, res.message);
      }
      fflush(stdout);
      free(nodes);
      DisposeBuffer(bc);
      DisposeModule(mod);
      ContextDispose(ctx);
      free(data);
   }
   fclose(list);
   dd(dev, NULL);
   return 0;
}
