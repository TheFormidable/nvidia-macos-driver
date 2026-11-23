/* pipetest: build ONE graphics pipeline (dynamic rendering) from a vertex and a fragment SPIR-V file and print every
 * pipeline executable statistic (VK_KHR_pipeline_executable_properties) and the VK_EXT_pipeline_creation_feedback
 * flags and durations.
 *
 * usage: pipetest [options] <vert.spv> <frag.spv>
 *   -s N   N combined image samplers in set 0, each its own binding: bindings 0 .. N-1           (default 0)
 *   -a M   one binding holding an array of M combined image samplers: binding N (after the -s ones) (default 0 = none)
 *   -u     one uniform buffer binding: the next binding after the above                             (default none)
 *   -b K   K storage buffers, each its own binding, after the uniform buffer                        (default 0)
 *   -p B   a push-constant range of B bytes, all graphics stages                                    (default 0)
 *   -P     pass an (empty, new) VkPipelineCache object; default VK_NULL_HANDLE (the driver's own cache)
 *   -n R   create the pipeline R times in this process (each one is reported)                       (default 1)
 *   -i     also capture and print the internal representations (CAPTURE_INTERNAL_REPRESENTATIONS)
 *   -q     quiet: only the feedback lines and a one-line statistics summary per stage
 * Every binding is visible to all graphics stages. One color attachment, VK_FORMAT_R8G8B8A8_UNORM, no vertex input
 * (use gl_VertexIndex), triangle list, entry point "main" in both modules.
 * So with -s 2 -a 4 -u: binding 0,1 = sampler2D, binding 2 = sampler2D[4], binding 3 = uniform block.
 * Exit status: 0 on success, 1 if instance/device setup fails, 2 if a shader module or the pipeline creation fails,
 * 3 on bad arguments. */
#include <vulkan/vulkan.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define CHECK(x, code) do { VkResult r_ = (x); if (r_ != VK_SUCCESS) { \
   fprintf(stderr, "pipetest: %s failed: VkResult %d\n", #x, (int)r_); exit(code); } } while (0)

static void *read_file(const char *path, size_t *size)
{
   FILE *f = fopen(path, "rb");
   if (!f) { perror(path); exit(3); }
   fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
   void *buf = malloc(n);
   if (fread(buf, 1, n, f) != (size_t)n) { perror(path); exit(3); }
   fclose(f);
   *size = n;
   return buf;
}

static const char *stage_name(VkShaderStageFlags s)
{
   switch (s) {
   case VK_SHADER_STAGE_VERTEX_BIT: return "vertex";
   case VK_SHADER_STAGE_FRAGMENT_BIT: return "fragment";
   default: return "other";
   }
}

static void print_feedback(const char *what, const VkPipelineCreationFeedback *fb)
{
   VkPipelineCreationFeedbackFlags f = fb->flags;
   printf("feedback %-9s valid=%d cache_hit=%d base_accel=%d duration_us=%.1f  (flags 0x%x)\n", what,
          !!(f & VK_PIPELINE_CREATION_FEEDBACK_VALID_BIT),
          !!(f & VK_PIPELINE_CREATION_FEEDBACK_APPLICATION_PIPELINE_CACHE_HIT_BIT),
          !!(f & VK_PIPELINE_CREATION_FEEDBACK_BASE_PIPELINE_ACCELERATION_BIT),
          fb->duration / 1000.0, (unsigned)f);
}

int main(int argc, char **argv)
{
   int n_samp = 0, arr = 0, ubo = 0, n_ssbo = 0, push = 0, use_cache = 0, reps = 1, irep = 0, quiet = 0, opt;
   while ((opt = getopt(argc, argv, "s:a:ub:p:Pn:iq")) != -1) {
      switch (opt) {
      case 's': n_samp = atoi(optarg); break;
      case 'a': arr = atoi(optarg); break;
      case 'u': ubo = 1; break;
      case 'b': n_ssbo = atoi(optarg); break;
      case 'p': push = atoi(optarg); break;
      case 'P': use_cache = 1; break;
      case 'n': reps = atoi(optarg); break;
      case 'i': irep = 1; break;
      case 'q': quiet = 1; break;
      default:
         fprintf(stderr, "usage: %s [-s N] [-a M] [-u] [-b K] [-p bytes] [-P] [-n reps] [-i] [-q] vert.spv frag.spv\n",
                 argv[0]);
         return 3;
      }
   }
   if (argc - optind != 2) {
      fprintf(stderr, "usage: %s [options] vert.spv frag.spv (see the comment at the top of pipetest.c)\n", argv[0]);
      return 3;
   }

   VkApplicationInfo app = { .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .pApplicationName = "pipetest",
                             .apiVersion = VK_API_VERSION_1_3 };
   VkInstanceCreateInfo ici = { .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app };
   VkInstance inst;
   CHECK(vkCreateInstance(&ici, NULL, &inst), 1);

   uint32_t npd = 0;
   CHECK(vkEnumeratePhysicalDevices(inst, &npd, NULL), 1);
   if (npd == 0) { fprintf(stderr, "pipetest: no physical device\n"); return 1; }
   VkPhysicalDevice pds[8]; if (npd > 8) npd = 8;
   CHECK(vkEnumeratePhysicalDevices(inst, &npd, pds), 1);
   VkPhysicalDevice pd = pds[0];
   VkPhysicalDeviceProperties props;
   vkGetPhysicalDeviceProperties(pd, &props);
   printf("device: %s (vendor 0x%x device 0x%x, driver 0x%x)\n", props.deviceName, props.vendorID, props.deviceID,
          props.driverVersion);
   printf("pipelineCacheUUID: ");
   for (int i = 0; i < VK_UUID_SIZE; i++) printf("%02x", props.pipelineCacheUUID[i]);
   printf("\n");

   uint32_t nqf = 0; vkGetPhysicalDeviceQueueFamilyProperties(pd, &nqf, NULL);
   VkQueueFamilyProperties qfp[16]; if (nqf > 16) nqf = 16;
   vkGetPhysicalDeviceQueueFamilyProperties(pd, &nqf, qfp);
   uint32_t qf = 0;
   for (uint32_t i = 0; i < nqf; i++) if (qfp[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) { qf = i; break; }

   float prio = 1.0f;
   VkDeviceQueueCreateInfo qci = { .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = qf,
                                   .queueCount = 1, .pQueuePriorities = &prio };
   VkPhysicalDevicePipelineExecutablePropertiesFeaturesKHR pexf = {
      .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PIPELINE_EXECUTABLE_PROPERTIES_FEATURES_KHR,
      .pipelineExecutableInfo = VK_TRUE };
   VkPhysicalDeviceVulkan13Features f13 = { .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_3_FEATURES,
                                            .pNext = &pexf, .dynamicRendering = VK_TRUE };
   const char *exts[] = { VK_KHR_PIPELINE_EXECUTABLE_PROPERTIES_EXTENSION_NAME };
   VkDeviceCreateInfo dci = { .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .pNext = &f13,
                              .queueCreateInfoCount = 1, .pQueueCreateInfos = &qci,
                              .enabledExtensionCount = 1, .ppEnabledExtensionNames = exts };
   VkDevice dev;
   CHECK(vkCreateDevice(pd, &dci, NULL, &dev), 1);

   PFN_vkGetPipelineExecutablePropertiesKHR getExecProps =
      (PFN_vkGetPipelineExecutablePropertiesKHR)vkGetDeviceProcAddr(dev, "vkGetPipelineExecutablePropertiesKHR");
   PFN_vkGetPipelineExecutableStatisticsKHR getExecStats =
      (PFN_vkGetPipelineExecutableStatisticsKHR)vkGetDeviceProcAddr(dev, "vkGetPipelineExecutableStatisticsKHR");
   PFN_vkGetPipelineExecutableInternalRepresentationsKHR getExecIR =
      (PFN_vkGetPipelineExecutableInternalRepresentationsKHR)vkGetDeviceProcAddr(
         dev, "vkGetPipelineExecutableInternalRepresentationsKHR");

   /* descriptor set layout */
   VkDescriptorSetLayoutBinding binds[256];
   uint32_t nb = 0;
   for (int i = 0; i < n_samp && nb < 256; i++, nb++)
      binds[nb] = (VkDescriptorSetLayoutBinding){ nb, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 1,
                                                  VK_SHADER_STAGE_ALL_GRAPHICS, NULL };
   if (arr > 0 && nb < 256) {
      binds[nb] = (VkDescriptorSetLayoutBinding){ nb, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, (uint32_t)arr,
                                                  VK_SHADER_STAGE_ALL_GRAPHICS, NULL };
      nb++;
   }
   if (ubo && nb < 256) {
      binds[nb] = (VkDescriptorSetLayoutBinding){ nb, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, 1,
                                                  VK_SHADER_STAGE_ALL_GRAPHICS, NULL };
      nb++;
   }
   for (int i = 0; i < n_ssbo && nb < 256; i++, nb++)
      binds[nb] = (VkDescriptorSetLayoutBinding){ nb, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1,
                                                  VK_SHADER_STAGE_ALL_GRAPHICS, NULL };
   printf("layout: set 0 with %u bindings (samplers %d, sampler array %d, ubo %d, ssbo %d), push %d bytes\n",
          nb, n_samp, arr, ubo, n_ssbo, push);
   VkDescriptorSetLayoutCreateInfo dslci = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
                                             .bindingCount = nb, .pBindings = binds };
   VkDescriptorSetLayout dsl;
   CHECK(vkCreateDescriptorSetLayout(dev, &dslci, NULL, &dsl), 1);
   VkPushConstantRange pcr = { VK_SHADER_STAGE_ALL_GRAPHICS, 0, (uint32_t)push };
   VkPipelineLayoutCreateInfo plci = { .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,
                                       .setLayoutCount = 1, .pSetLayouts = &dsl,
                                       .pushConstantRangeCount = push > 0 ? 1 : 0, .pPushConstantRanges = &pcr };
   VkPipelineLayout pl;
   CHECK(vkCreatePipelineLayout(dev, &plci, NULL, &pl), 1);

   /* shaders */
   VkShaderModule mods[2];
   for (int i = 0; i < 2; i++) {
      size_t sz; void *code = read_file(argv[optind + i], &sz);
      VkShaderModuleCreateInfo smci = { .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, .codeSize = sz,
                                        .pCode = code };
      CHECK(vkCreateShaderModule(dev, &smci, NULL, &mods[i]), 2);
      free(code);
   }

   VkPipelineCache cache = VK_NULL_HANDLE;
   if (use_cache) {
      VkPipelineCacheCreateInfo pcci = { .sType = VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO };
      CHECK(vkCreatePipelineCache(dev, &pcci, NULL, &cache), 1);
   }

   for (int rep = 0; rep < reps; rep++) {
      VkPipelineCreationFeedback fb_pipe = {0}, fb_stage[2] = {{0}};
      VkPipelineCreationFeedbackCreateInfo fbci = { .sType = VK_STRUCTURE_TYPE_PIPELINE_CREATION_FEEDBACK_CREATE_INFO,
                                                    .pPipelineCreationFeedback = &fb_pipe,
                                                    .pipelineStageCreationFeedbackCount = 2,
                                                    .pPipelineStageCreationFeedbacks = fb_stage };
      VkFormat cfmt = VK_FORMAT_R8G8B8A8_UNORM;
      VkPipelineRenderingCreateInfo prci = { .sType = VK_STRUCTURE_TYPE_PIPELINE_RENDERING_CREATE_INFO,
                                             .pNext = &fbci, .colorAttachmentCount = 1,
                                             .pColorAttachmentFormats = &cfmt };
      VkPipelineShaderStageCreateInfo stages[2] = {
         { .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_VERTEX_BIT,
           .module = mods[0], .pName = "main" },
         { .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_FRAGMENT_BIT,
           .module = mods[1], .pName = "main" },
      };
      VkPipelineVertexInputStateCreateInfo vi = { .sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO };
      VkPipelineInputAssemblyStateCreateInfo ia = { .sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO,
                                                    .topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST };
      VkPipelineViewportStateCreateInfo vp = { .sType = VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO,
                                               .viewportCount = 1, .scissorCount = 1 };
      VkPipelineRasterizationStateCreateInfo rs = { .sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO,
                                                    .polygonMode = VK_POLYGON_MODE_FILL, .cullMode = VK_CULL_MODE_NONE,
                                                    .frontFace = VK_FRONT_FACE_COUNTER_CLOCKWISE, .lineWidth = 1.0f };
      VkPipelineMultisampleStateCreateInfo ms = { .sType = VK_STRUCTURE_TYPE_PIPELINE_MULTISAMPLE_STATE_CREATE_INFO,
                                                  .rasterizationSamples = VK_SAMPLE_COUNT_1_BIT };
      VkPipelineColorBlendAttachmentState cba = { .colorWriteMask = 0xf };
      VkPipelineColorBlendStateCreateInfo cb = { .sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO,
                                                 .attachmentCount = 1, .pAttachments = &cba };
      VkDynamicState dyn[] = { VK_DYNAMIC_STATE_VIEWPORT, VK_DYNAMIC_STATE_SCISSOR };
      VkPipelineDynamicStateCreateInfo ds = { .sType = VK_STRUCTURE_TYPE_PIPELINE_DYNAMIC_STATE_CREATE_INFO,
                                              .dynamicStateCount = 2, .pDynamicStates = dyn };
      VkGraphicsPipelineCreateInfo gpci = {
         .sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO, .pNext = &prci,
         .flags = VK_PIPELINE_CREATE_CAPTURE_STATISTICS_BIT_KHR |
                  (irep ? VK_PIPELINE_CREATE_CAPTURE_INTERNAL_REPRESENTATIONS_BIT_KHR : 0),
         .stageCount = 2, .pStages = stages, .pVertexInputState = &vi, .pInputAssemblyState = &ia,
         .pViewportState = &vp, .pRasterizationState = &rs, .pMultisampleState = &ms, .pColorBlendState = &cb,
         .pDynamicState = &ds, .layout = pl };
      VkPipeline pipe;
      fflush(stdout);
      VkResult r = vkCreateGraphicsPipelines(dev, cache, 1, &gpci, NULL, &pipe);
      if (r != VK_SUCCESS) {
         fprintf(stderr, "pipetest: vkCreateGraphicsPipelines failed: VkResult %d\n", (int)r);
         return 2;
      }
      printf("=== pipeline %d created\n", rep);
      print_feedback("pipeline", &fb_pipe);
      print_feedback("vertex", &fb_stage[0]);
      print_feedback("fragment", &fb_stage[1]);

      VkPipelineInfoKHR pinfo = { .sType = VK_STRUCTURE_TYPE_PIPELINE_INFO_KHR, .pipeline = pipe };
      uint32_t nexe = 0;
      CHECK(getExecProps(dev, &pinfo, &nexe, NULL), 2);
      VkPipelineExecutablePropertiesKHR eprops[8];
      if (nexe > 8) nexe = 8;
      for (uint32_t i = 0; i < nexe; i++)
         eprops[i] = (VkPipelineExecutablePropertiesKHR){ .sType = VK_STRUCTURE_TYPE_PIPELINE_EXECUTABLE_PROPERTIES_KHR };
      CHECK(getExecProps(dev, &pinfo, &nexe, eprops), 2);
      for (uint32_t e = 0; e < nexe; e++) {
         VkPipelineExecutableInfoKHR einfo = { .sType = VK_STRUCTURE_TYPE_PIPELINE_EXECUTABLE_INFO_KHR,
                                               .pipeline = pipe, .executableIndex = e };
         uint32_t ns = 0;
         CHECK(getExecStats(dev, &einfo, &ns, NULL), 2);
         VkPipelineExecutableStatisticKHR *st = calloc(ns ? ns : 1, sizeof(*st));
         for (uint32_t i = 0; i < ns; i++) st[i].sType = VK_STRUCTURE_TYPE_PIPELINE_EXECUTABLE_STATISTIC_KHR;
         CHECK(getExecStats(dev, &einfo, &ns, st), 2);
         if (!quiet)
            printf("executable %u: %s (%s), stages 0x%x (%s), subgroup %u\n", e, eprops[e].name,
                   eprops[e].description, eprops[e].stages, stage_name(eprops[e].stages), eprops[e].subgroupSize);
         else
            printf("stats %s:", stage_name(eprops[e].stages));
         for (uint32_t i = 0; i < ns; i++) {
            char val[64];
            switch (st[i].format) {
            case VK_PIPELINE_EXECUTABLE_STATISTIC_FORMAT_BOOL32_KHR:
               snprintf(val, sizeof(val), "%s", st[i].value.b32 ? "true" : "false"); break;
            case VK_PIPELINE_EXECUTABLE_STATISTIC_FORMAT_INT64_KHR:
               snprintf(val, sizeof(val), "%lld", (long long)st[i].value.i64); break;
            case VK_PIPELINE_EXECUTABLE_STATISTIC_FORMAT_UINT64_KHR:
               snprintf(val, sizeof(val), "%llu", (unsigned long long)st[i].value.u64); break;
            default:
               snprintf(val, sizeof(val), "%g", st[i].value.f64); break;
            }
            if (quiet)
               printf(" %s=%s", st[i].name, val);
            else
               printf("  stat %-32s = %-10s  (%s)\n", st[i].name, val, st[i].description);
         }
         if (quiet) printf("\n");
         free(st);

         if (irep && getExecIR) {
            uint32_t nir = 0;
            CHECK(getExecIR(dev, &einfo, &nir, NULL), 2);
            VkPipelineExecutableInternalRepresentationKHR *ir = calloc(nir ? nir : 1, sizeof(*ir));
            for (uint32_t i = 0; i < nir; i++) ir[i].sType = VK_STRUCTURE_TYPE_PIPELINE_EXECUTABLE_INTERNAL_REPRESENTATION_KHR;
            CHECK(getExecIR(dev, &einfo, &nir, ir), 2);
            for (uint32_t i = 0; i < nir; i++) { ir[i].pData = calloc(1, ir[i].dataSize + 1); }
            VkResult rr = getExecIR(dev, &einfo, &nir, ir);
            if (rr != VK_SUCCESS && rr != VK_INCOMPLETE) CHECK(rr, 2);
            for (uint32_t i = 0; i < nir; i++) {
               printf("  --- internal representation %s (%s), %zu bytes\n", ir[i].name, ir[i].description,
                      ir[i].dataSize);
               if (ir[i].isText) printf("%s\n", (char *)ir[i].pData);
               free(ir[i].pData);
            }
            free(ir);
         }
      }
      vkDestroyPipeline(dev, pipe, NULL);
   }

   if (cache) vkDestroyPipelineCache(dev, cache, NULL);
   for (int i = 0; i < 2; i++) vkDestroyShaderModule(dev, mods[i], NULL);
   vkDestroyPipelineLayout(dev, pl, NULL);
   vkDestroyDescriptorSetLayout(dev, dsl, NULL);
   vkDestroyDevice(dev, NULL);
   vkDestroyInstance(inst, NULL);
   return 0;
}
