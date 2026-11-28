/*
 * NullMoth NVIDIA driver for macOS
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 *
 * airtest FILE.bc...: each AIR bitcode file through NVK's AIR-to-NIR export (nvk/air/nvk_air_to_nir.c), then
 * vkCreateComputePipelines with the NIR and the layout the plugin uses for translated kernels (set 0 binding 640 the
 * buffer-address table, 48 bytes of push constants), as NAK compiles it. Prints the export's message and NAK's
 * instruction count per file, and "ok N, refused N, failed N"; exit 2 if any file failed (a pipeline NVK would not
 * create), 0 otherwise: a refusal is the frontend saying no, which the plugin answers with the SPIR-V translator.
 *
 * NVK_HOST_W names the host NVK build (tools/air-nir/run.sh); AIRTEST_LLVM the LLVM library (default LLVM 18's),
 * AIRTEST_LSX the threadgroup width (64).
 */
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <vulkan/vulkan.h>

#include "nvmtl_air_nir_abi.h"

typedef struct {
   VkStructureType sType;
   const void *pNext;
   void *nir;
} NirInfo;
#define STYPE_NIR ((VkStructureType)1000290001)

#define CK(x) do { VkResult r_ = (x); if (r_ != VK_SUCCESS) { fprintf(stderr, "%s = %d\n", #x, r_); exit(1); } } while (0)

static void *
slurp(const char *p, size_t *n)
{
   FILE *f = fopen(p, "rb");
   if (!f)
      return NULL;
   fseek(f, 0, SEEK_END);
   *n = ftell(f);
   fseek(f, 0, SEEK_SET);
   void *b = malloc(*n);
   if (fread(b, 1, *n, f) != *n) { free(b); b = NULL; }
   fclose(f);
   return b;
}

int
main(int argc, char **argv)
{
   const char *llvm = getenv("AIRTEST_LLVM") ? getenv("AIRTEST_LLVM") : "/usr/lib/llvm-18/lib/libLLVM.so.1";
   unsigned lsx = getenv("AIRTEST_LSX") ? atoi(getenv("AIRTEST_LSX")) : 64;

   VkApplicationInfo app = { VK_STRUCTURE_TYPE_APPLICATION_INFO, .apiVersion = VK_API_VERSION_1_3 };
   VkInstanceCreateInfo ici = { VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app };
   VkInstance inst;
   CK(vkCreateInstance(&ici, NULL, &inst));
   uint32_t n = 1;
   VkPhysicalDevice pd;
   vkEnumeratePhysicalDevices(inst, &n, &pd);
   if (!n) { fprintf(stderr, "no device\n"); return 1; }
   float prio = 1;
   VkDeviceQueueCreateInfo qci = { VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = 0, .queueCount = 1, .pQueuePriorities = &prio };
   const char *exts[] = { "VK_KHR_pipeline_executable_properties" };
   VkPhysicalDevicePipelineExecutablePropertiesFeaturesKHR pef = { VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PIPELINE_EXECUTABLE_PROPERTIES_FEATURES_KHR, .pipelineExecutableInfo = VK_TRUE };
   VkDeviceCreateInfo dci = { VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .pNext = &pef, .queueCreateInfoCount = 1, .pQueueCreateInfos = &qci, .enabledExtensionCount = 1, .ppEnabledExtensionNames = exts };
   VkDevice dev;
   CK(vkCreateDevice(pd, &dci, NULL, &dev));
   PFN_vkGetPipelineExecutableStatisticsKHR stats = (void *)vkGetDeviceProcAddr(dev, "vkGetPipelineExecutableStatisticsKHR");

   char path[2048];
   snprintf(path, sizeof path, "%s/src/build/src/nouveau/vulkan/libvulkan_nouveau.so", getenv("NVK_HOST_W"));
   void *h = dlopen(path, RTLD_NOW | RTLD_NOLOAD);
   if (!h) { fprintf(stderr, "NVK not loaded: %s\n", dlerror()); return 1; }
   nvmtl_air_to_nir_v1_fn to_nir = (nvmtl_air_to_nir_v1_fn)dlsym(h, "nvmtl_air_to_nir_v1");
   nvmtl_air_nir_free_v1_fn free_nir = (nvmtl_air_nir_free_v1_fn)dlsym(h, "nvmtl_air_nir_free_v1");
   if (!to_nir || !free_nir) { fprintf(stderr, "no export\n"); return 1; }

   VkDescriptorSetLayoutBinding bind = { 640, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
   VkDescriptorSetLayoutCreateInfo dl = { VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO, .bindingCount = 1, .pBindings = &bind };
   VkDescriptorSetLayout sl;
   CK(vkCreateDescriptorSetLayout(dev, &dl, NULL, &sl));
   VkPushConstantRange pr = { VK_SHADER_STAGE_COMPUTE_BIT, 0, NVMTL_AIR_NIR_PUSH_BYTES };
   VkPipelineLayoutCreateInfo pl = { VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, .setLayoutCount = 1, .pSetLayouts = &sl, .pushConstantRangeCount = 1, .pPushConstantRanges = &pr };
   VkPipelineLayout layout;
   CK(vkCreatePipelineLayout(dev, &pl, NULL, &layout));

   int ok = 0, refused = 0, failed = 0;
   for (int i = 1; i < argc; i++) {
      size_t sz;
      void *bc = slurp(argv[i], &sz);
      const char *base = strrchr(argv[i], '/') ? strrchr(argv[i], '/') + 1 : argv[i];
      if (!bc) { printf("%-48s cannot read\n", base); failed++; continue; }
      struct nvmtl_air_nir_request req = { sizeof req, 0, llvm, bc, sz, NULL, { lsx, 1, 1 } };
      for (unsigned k = 0; k < NVMTL_AIR_NIR_MAX_BUFFERS; k++)
         req.threadgroup_lengths[k] = 1024;
      struct nvmtl_air_nir_result res = { sizeof res };
      int st = to_nir(dev, &req, &res);
      if (st) {
         printf("%-48s REFUSED(%d) %s\n", base, st, res.message);
         refused++;
         free(bc);
         continue;
      }
      NirInfo ni = { STYPE_NIR, NULL, res.nir };
      VkComputePipelineCreateInfo cpi = { VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
         .flags = VK_PIPELINE_CREATE_CAPTURE_STATISTICS_BIT_KHR,
         .stage = { VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, &ni, 0, VK_SHADER_STAGE_COMPUTE_BIT, VK_NULL_HANDLE, "main", NULL },
         .layout = layout };
      VkPipeline pipe;
      VkResult r = vkCreateComputePipelines(dev, VK_NULL_HANDLE, 1, &cpi, NULL, &pipe);
      free_nir(res.nir);
      free(bc);
      if (r != VK_SUCCESS) {
         printf("%-48s PIPELINE FAILED %d (%s)\n", base, r, res.message);
         failed++;
         continue;
      }
      char line[512] = "";
      VkPipelineExecutableInfoKHR ei = { VK_STRUCTURE_TYPE_PIPELINE_EXECUTABLE_INFO_KHR, .pipeline = pipe, .executableIndex = 0 };
      uint32_t ns = 0;
      if (stats && stats(dev, &ei, &ns, NULL) == VK_SUCCESS && ns) {
         VkPipelineExecutableStatisticKHR *s = calloc(ns, sizeof *s);
         for (uint32_t k = 0; k < ns; k++)
            s[k].sType = VK_STRUCTURE_TYPE_PIPELINE_EXECUTABLE_STATISTIC_KHR;
         stats(dev, &ei, &ns, s);
         for (uint32_t k = 0; k < ns; k++) {
            if (strcmp(s[k].name, "Instruction count") && strcmp(s[k].name, "GPRs") && strcmp(s[k].name, "Static cycle count"))
               continue;
            char tmp[128];
            snprintf(tmp, sizeof tmp, " %.64s=%llu", s[k].name, (unsigned long long)s[k].value.u64);
            strncat(line, tmp, sizeof line - strlen(line) - 1);
         }
         free(s);
      }
      printf("%-48s OK  %s |%s\n", base, res.message, line);
      vkDestroyPipeline(dev, pipe, NULL);
      ok++;
   }
   printf("ok %d, refused %d, failed %d\n", ok, refused, failed);
   vkDestroyPipelineLayout(dev, layout, NULL);
   vkDestroyDescriptorSetLayout(dev, sl, NULL);
   vkDestroyDevice(dev, NULL);
   vkDestroyInstance(inst, NULL);
   return failed ? 2 : 0;
}
