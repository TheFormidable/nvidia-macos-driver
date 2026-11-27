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
 */
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan_core.h>

#include <dlfcn.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "nvmtl_air_nir_abi.h"

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

int
main(int argc, char **argv)
{
   if (argc != 4) {
      fprintf(stderr, "usage: airsweep NVK.dylib LLVM.dylib LIST\n");
      return 2;
   }
   void *lib = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
   void *llvm = dlopen(argv[2], RTLD_NOW | RTLD_LOCAL);
   if (!lib || !llvm) {
      fprintf(stderr, "airsweep: %s\n", dlerror());
      return 2;
   }
#define SYM(var, name)                                                                                           \
   *(void **)&var = dlsym(llvm, name);                                                                           \
   if (!var) {                                                                                                   \
      fprintf(stderr, "airsweep: %s has no %s\n", argv[2], name);                                                \
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
      fprintf(stderr, "airsweep: %s lacks the exports\n", argv[1]);
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
   PFN_vkCreateDescriptorSetLayout cdsl = (PFN_vkCreateDescriptorSetLayout)gdpa(dev, "vkCreateDescriptorSetLayout");
   PFN_vkCreatePipelineLayout cpl = (PFN_vkCreatePipelineLayout)gdpa(dev, "vkCreatePipelineLayout");
   PFN_vkCreateComputePipelines ccp = (PFN_vkCreateComputePipelines)gdpa(dev, "vkCreateComputePipelines");
   PFN_vkDestroyPipeline dp = (PFN_vkDestroyPipeline)gdpa(dev, "vkDestroyPipeline");
   PFN_vkDestroyDevice dd = (PFN_vkDestroyDevice)gdpa(dev, "vkDestroyDevice");
   VkDescriptorSetLayoutBinding bind = { NVMTL_AIR_NIR_TABLE_BINDING, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC, 1,
                                         VK_SHADER_STAGE_COMPUTE_BIT, NULL };
   VkDescriptorSetLayoutCreateInfo dl = { VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO, .bindingCount = 1,
                                          .pBindings = &bind };
   VkDescriptorSetLayout sl;
   cdsl(dev, &dl, NULL, &sl);
   VkPushConstantRange pr = { VK_SHADER_STAGE_COMPUTE_BIT, 0, NVMTL_AIR_NIR_PUSH_BYTES };
   VkPipelineLayoutCreateInfo pl = { VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, .setLayoutCount = 1,
                                     .pSetLayouts = &sl, .pushConstantRangeCount = 1, .pPushConstantRanges = &pr };
   VkPipelineLayout layout;
   cpl(dev, &pl, NULL, &layout);

   FILE *list = fopen(argv[3], "r");
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
      uint32_t fc_idx[64], fc_sz[64], nfc = 0;
      static uint8_t fc_bytes[64][64];
      const uint8_t *fc_pay[64];
      for (char *tok = fcs ? strtok(fcs, ",") : NULL; tok && nfc < 64; tok = strtok(NULL, ",")) {
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
         struct nvmtl_air_nir_request req = { sizeof req, 0, argv[2], BufferStart(bc), BufferSize(bc), name,
                                              { 64, 1, 1 } };
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
