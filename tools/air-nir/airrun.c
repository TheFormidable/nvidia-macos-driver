/*
 * NullMoth NVIDIA driver for macOS
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 *
 * airrun NVK.dylib LLVM.dylib AIRDIR [TEST...]: runs Metal kernels through NVK's AIR-to-NIR export
 * (nvk/air/nvk_air_to_nir.c) on the card and checks what they compute (macOS only).
 *
 * For each test below it reads AIRDIR/<test>.air (xcrun -sdk macosx metal -c of tools/air-nir/metal/<test>.metal,
 * tools/air-nir/run-mac.sh), translates it with nvmtl_air_to_nir_v1, creates the compute pipeline with the plugin's
 * layout for translated kernels (set 0 binding 640 the buffer-address table as a dynamic uniform buffer, 48 bytes of
 * push constants, nvmtl_air_nir_abi.h), fills host-visible buffers, writes their GPU addresses into the table,
 * dispatches whole threadgroups, waits at most 2 s and compares the output with a reference computed here.
 *
 * NVK is the library named on the command line, opened directly (vk_icdGetInstanceProcAddr), never through the
 * Vulkan loader. Before and after every dispatch it reads NVRM's fault counters (debug.nvrm_xid_count,
 * debug.nvrm_chan_recovered, debug.nvrm_chan_gave_up, debug.nvrm_chan_dropped); if one rises, or a fence does not
 * signal in 2 s, it names the kernel, stops all GPU work and exits 3.
 *
 * Prints one line per test: PASS, FAIL (the first wrong value: index, got, expected), REFUSED (the frontend's
 * message) or PIPELINE FAILED; then a summary. Exit 1 if any test failed, 3 on a GPU fault, 2 if it could not start.
 * A test marked MUST_REFUSE passes only if the frontend refuses its kernel (function constants, whose values the request
 * cannot carry yet). AIRRUN_ALIGN4=1 runs the build the plugin's align guard makes for a misaligned buffer (no buffer
 * access promises more than 4 bytes). AIRRUN_ONLY_TRANSLATE=1 stops after pipeline creation (no dispatch). AIRRUN_CORRUPT=1 is the harness's own test: it
 * changes the first byte of every buffer after the dispatch, and a test passes only if its check then fails.
 */
#define VK_NO_PROTOTYPES
#include <vulkan/vulkan_core.h>

#include <dlfcn.h>
#include <math.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/sysctl.h>
#include <unistd.h>

#include "nvmtl_air_nir_abi.h"

#define STYPE_NIR ((VkStructureType)1000290001)   /* VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_NIR_CREATE_INFO_MESA */
typedef struct {
   VkStructureType sType;
   const void *pNext;
   void *nir;
} NirInfo;

/* ---------------------------------------------------------------------------------------------------------------- */
/* Vulkan, straight from NVK                                                                                         */

#define VK_FUNCS(X)                                                                                              \
   X(vkEnumeratePhysicalDevices) X(vkGetPhysicalDeviceProperties) X(vkGetPhysicalDeviceMemoryProperties)          \
   X(vkGetPhysicalDeviceQueueFamilyProperties) X(vkCreateDevice) X(vkGetDeviceProcAddr) X(vkDestroyInstance)
#define DEV_FUNCS(X)                                                                                             \
   X(vkDestroyDevice) X(vkGetDeviceQueue) X(vkCreateBuffer) X(vkDestroyBuffer) X(vkGetBufferMemoryRequirements)   \
   X(vkAllocateMemory) X(vkFreeMemory) X(vkBindBufferMemory) X(vkMapMemory) X(vkGetBufferDeviceAddress)           \
   X(vkCreateDescriptorSetLayout) X(vkDestroyDescriptorSetLayout) X(vkCreatePipelineLayout)                       \
   X(vkDestroyPipelineLayout) X(vkCreateDescriptorPool) X(vkDestroyDescriptorPool) X(vkAllocateDescriptorSets)     \
   X(vkUpdateDescriptorSets) X(vkCreateComputePipelines) X(vkDestroyPipeline) X(vkCreateCommandPool)              \
   X(vkDestroyCommandPool) X(vkAllocateCommandBuffers) X(vkBeginCommandBuffer) X(vkEndCommandBuffer)              \
   X(vkCmdBindPipeline) X(vkCmdBindDescriptorSets) X(vkCmdPushConstants) X(vkCmdDispatch) X(vkCmdPipelineBarrier)  \
   X(vkQueueSubmit) X(vkCreateFence) X(vkDestroyFence) X(vkWaitForFences) X(vkResetFences)                        \
   X(vkResetCommandBuffer) X(vkDeviceWaitIdle) X(vkCreateImage) X(vkDestroyImage) X(vkGetImageMemoryRequirements)      \
   X(vkBindImageMemory) X(vkCreateImageView) X(vkDestroyImageView) X(vkCreateBufferView) X(vkDestroyBufferView)       \
   X(vkCreateSampler) X(vkDestroySampler) X(vkCmdCopyBufferToImage) X(vkCmdCopyImageToBuffer) X(vkResetDescriptorPool)

#define X(n) static PFN_##n n;
VK_FUNCS(X)
DEV_FUNCS(X)
#undef X

static VkInstance inst;
static VkPhysicalDevice pdev;
static VkDevice dev;
static VkQueue queue;
static uint32_t mem_type = ~0u;
static VkPhysicalDeviceMemoryProperties mem_props;
static VkDescriptorPool pool;
static VkCommandPool cmd_pool;
static VkCommandBuffer cmd;
static VkFence fence;
static nvmtl_air_to_nir_v1_fn to_nir;
static nvmtl_air_nir_free_v1_fn free_nir;

#define CK(x)                                                                                                    \
   do {                                                                                                          \
      VkResult r_ = (x);                                                                                         \
      if (r_ != VK_SUCCESS) {                                                                                    \
         fprintf(stderr, "airrun: %s = %d\n", #x, r_);                                                           \
         exit(2);                                                                                                \
      }                                                                                                          \
   } while (0)

/* ---------------------------------------------------------------------------------------------------------------- */
/* NVRM's fault counters                                                                                             */

static const char *const counter_names[] = {
   "debug.nvrm_xid_count", "debug.nvrm_chan_recovered", "debug.nvrm_chan_gave_up", "debug.nvrm_chan_dropped",
};
#define NCOUNTERS 4

static void
read_counters(int64_t *v)
{
   for (unsigned i = 0; i < NCOUNTERS; i++) {
      uint64_t raw = 0;
      size_t len = sizeof raw;
      if (sysctlbyname(counter_names[i], &raw, &len, NULL, 0) != 0) {
         v[i] = -1;
         continue;
      }
      v[i] = len == 4 ? (int64_t)(int32_t)(uint32_t)raw : (int64_t)raw;
   }
}

static void
print_counters(const char *when, const int64_t *v)
{
   printf("counters %s:", when);
   for (unsigned i = 0; i < NCOUNTERS; i++)
      printf(" %s=%lld", counter_names[i] + 6, (long long)v[i]);
   printf("\n");
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Buffers                                                                                                           */

struct buf {
   VkBuffer buffer;
   VkDeviceMemory memory;
   void *map;
   VkDeviceAddress address;
   size_t size;
};

#define MAX_BYTES (16u << 20)

static struct buf
buf_create(size_t size, VkBufferUsageFlags usage)
{
   struct buf b = { .size = size };
   if (size > MAX_BYTES) {
      fprintf(stderr, "airrun: a buffer of %zu bytes is over the 16 MB limit\n", size);
      exit(2);
   }
   VkBufferCreateInfo bci = { VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = size,
                              .usage = usage | VK_BUFFER_USAGE_SHADER_DEVICE_ADDRESS_BIT,
                              .sharingMode = VK_SHARING_MODE_EXCLUSIVE };
   CK(vkCreateBuffer(dev, &bci, NULL, &b.buffer));
   VkMemoryRequirements req;
   vkGetBufferMemoryRequirements(dev, b.buffer, &req);
   if (!(req.memoryTypeBits & (1u << mem_type))) {
      fprintf(stderr, "airrun: memory type %u cannot hold a buffer\n", mem_type);
      exit(2);
   }
   VkMemoryAllocateFlagsInfo flags = { VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_FLAGS_INFO,
                                       .flags = VK_MEMORY_ALLOCATE_DEVICE_ADDRESS_BIT };
   VkMemoryAllocateInfo mai = { VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, &flags, req.size, mem_type };
   CK(vkAllocateMemory(dev, &mai, NULL, &b.memory));
   CK(vkBindBufferMemory(dev, b.buffer, b.memory, 0));
   CK(vkMapMemory(dev, b.memory, 0, VK_WHOLE_SIZE, 0, &b.map));
   memset(b.map, 0, size);
   VkBufferDeviceAddressInfo ai = { VK_STRUCTURE_TYPE_BUFFER_DEVICE_ADDRESS_INFO, .buffer = b.buffer };
   b.address = vkGetBufferDeviceAddress(dev, &ai);
   return b;
}

static void
buf_destroy(struct buf *b)
{
   if (!b->buffer)
      return;
   vkDestroyBuffer(dev, b->buffer, NULL);
   vkFreeMemory(dev, b->memory, NULL);
   memset(b, 0, sizeof *b);
}

/* ---------------------------------------------------------------------------------------------------------------- */
/* Tests                                                                                                             */

/* a [[texture(n)]] resource: an image (uploaded from stage before the dispatch and, when it is written, copied back
 * into stage after it) or a texel buffer (stage is the buffer itself) */
struct timg {
   VkImage image;
   VkDeviceMemory mem;
   VkImageView view;
   VkBufferView bview;
   struct buf stage;
   VkFormat fmt;
   VkImageViewType vtype;
   uint32_t w, h, d, layers, mips, texel;
   bool storage, depth, is_buffer;
};

struct test;
struct run {
   const struct test *t;
   struct buf slot[NVMTL_AIR_NIR_MAX_BUFFERS];
   struct timg tex[16];
   VkSampler samp[32];
   VkSampler statics[32];
   unsigned threads;                       /* in the whole grid */
   char why[256];                          /* the first mismatch */
};

/* ---------------------------------------------------------------------------------------------------------------- */
/* Textures and samplers, made as the plugin makes them (plugin/nvmtl_vk.c nvmtl_vk_image_create_typed): optimal      */
/* tiling, transfer, sampled and (when written) storage usage, colour-attachment usage for colour formats, a cube     */
/* image cube-compatible; sampled images bound in SHADER_READ_ONLY_OPTIMAL, storage images in GENERAL.               */

static uint32_t
local_type(uint32_t bits)
{
   for (uint32_t i = 0; i < mem_props.memoryTypeCount; i++) {
      if ((bits & (1u << i)) && (mem_props.memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT))
         return i;
   }
   for (uint32_t i = 0; i < mem_props.memoryTypeCount; i++) {
      if (bits & (1u << i))
         return i;
   }
   return ~0u;
}

static uint32_t
mip_size(uint32_t v, uint32_t m)
{
   return v >> m ? v >> m : 1;
}

/* bytes of every mip level and layer before level m in the staging copy (levels one after the other, each holding all
 * its layers, as vkCmdCopyBufferToImage reads them below) */
static size_t
tex_level_offset(const struct timg *x, uint32_t m)
{
   size_t off = 0;
   for (uint32_t k = 0; k < m; k++)
      off += (size_t)mip_size(x->w, k) * mip_size(x->h, k) * mip_size(x->d, k) * x->layers * x->texel;
   return off;
}

/* the texel (x, y, z or layer) of level m in the staging copy */
static void *
tex_at(struct run *r, unsigned i, uint32_t m, uint32_t x, uint32_t y, uint32_t zl)
{
   struct timg *t = &r->tex[i];
   uint32_t w = mip_size(t->w, m), h = mip_size(t->h, m);
   return (uint8_t *)t->stage.map + tex_level_offset(t, m) + (((size_t)zl * h + y) * w + x) * t->texel;
}

/* An image for [[texture(i)]]: vt its view type, layers 6 for a cube, d the depth of a 3D image. Returns the staging
 * copy, filled by the test, uploaded before the dispatch. */
static void
tex_image(struct run *r, unsigned i, VkImageViewType vt, VkFormat fmt, uint32_t texel, uint32_t w, uint32_t h, uint32_t d,
          uint32_t layers, uint32_t mips, bool storage)
{
   struct timg *t = &r->tex[i];
   memset(t, 0, sizeof *t);
   t->fmt = fmt;
   t->vtype = vt;
   t->w = w;
   t->h = h;
   t->d = d;
   t->layers = layers;
   t->mips = mips;
   t->texel = texel;
   t->storage = storage;
   t->depth = fmt == VK_FORMAT_D32_SFLOAT;
   VkImageCreateInfo ici = {
      VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, NULL,
      (vt == VK_IMAGE_VIEW_TYPE_CUBE || vt == VK_IMAGE_VIEW_TYPE_CUBE_ARRAY) ? VK_IMAGE_CREATE_CUBE_COMPATIBLE_BIT : 0,
      vt == VK_IMAGE_VIEW_TYPE_3D ? VK_IMAGE_TYPE_3D : (vt == VK_IMAGE_VIEW_TYPE_1D || vt == VK_IMAGE_VIEW_TYPE_1D_ARRAY) ?
                                                         VK_IMAGE_TYPE_1D : VK_IMAGE_TYPE_2D,
      fmt, { w, h, d }, mips, layers, VK_SAMPLE_COUNT_1_BIT, VK_IMAGE_TILING_OPTIMAL,
      (t->depth ? 0 : VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT) | VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT |
         VK_IMAGE_USAGE_SAMPLED_BIT | (storage ? VK_IMAGE_USAGE_STORAGE_BIT : 0),
      VK_SHARING_MODE_EXCLUSIVE, 0, NULL, VK_IMAGE_LAYOUT_UNDEFINED };
   CK(vkCreateImage(dev, &ici, NULL, &t->image));
   VkMemoryRequirements req;
   vkGetImageMemoryRequirements(dev, t->image, &req);
   VkMemoryAllocateInfo mai = { VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, NULL, req.size, local_type(req.memoryTypeBits) };
   CK(vkAllocateMemory(dev, &mai, NULL, &t->mem));
   CK(vkBindImageMemory(dev, t->image, t->mem, 0));
   VkImageViewCreateInfo vci = { VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, NULL, 0, t->image, vt, fmt,
                                 { 0 }, { t->depth ? VK_IMAGE_ASPECT_DEPTH_BIT : VK_IMAGE_ASPECT_COLOR_BIT, 0, mips, 0, layers } };
   CK(vkCreateImageView(dev, &vci, NULL, &t->view));
   t->stage = buf_create(tex_level_offset(t, mips), VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT);
}

/* A texture_buffer for [[texture(i)]]: n texels in a host-visible buffer, the buffer itself the test's view of it */
static void *
tex_buffer(struct run *r, unsigned i, VkFormat fmt, uint32_t texel, uint32_t n, bool storage)
{
   struct timg *t = &r->tex[i];
   memset(t, 0, sizeof *t);
   t->fmt = fmt;
   t->texel = texel;
   t->w = n;
   t->h = t->d = t->layers = t->mips = 1;
   t->storage = storage;
   t->is_buffer = true;
   t->stage = buf_create((size_t)n * texel, VK_BUFFER_USAGE_UNIFORM_TEXEL_BUFFER_BIT | VK_BUFFER_USAGE_STORAGE_TEXEL_BUFFER_BIT);
   VkBufferViewCreateInfo bvi = { VK_STRUCTURE_TYPE_BUFFER_VIEW_CREATE_INFO, NULL, 0, t->stage.buffer, fmt, 0, VK_WHOLE_SIZE };
   CK(vkCreateBufferView(dev, &bvi, NULL, &t->bview));
   return t->stage.map;
}

static void
tex_destroy(struct timg *t)
{
   if (t->view)
      vkDestroyImageView(dev, t->view, NULL);
   if (t->image)
      vkDestroyImage(dev, t->image, NULL);
   if (t->mem)
      vkFreeMemory(dev, t->mem, NULL);
   if (t->bview)
      vkDestroyBufferView(dev, t->bview, NULL);
   buf_destroy(&t->stage);
   memset(t, 0, sizeof *t);
}

static void
samp_create(struct run *r, unsigned slot, VkFilter filter, VkSamplerAddressMode address, VkSamplerMipmapMode mip, float max_lod)
{
   VkSamplerCreateInfo sci = { VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO, NULL, 0, filter, filter, mip, address, address, address,
                               0, VK_FALSE, 1, VK_FALSE, VK_COMPARE_OP_NEVER, 0, max_lod,
                               VK_BORDER_COLOR_FLOAT_TRANSPARENT_BLACK, VK_FALSE };
   CK(vkCreateSampler(dev, &sci, NULL, &r->samp[slot]));
}

/* airrun cannot hand a sampler's state to the frontend: the tests that bind a pixel-coordinate sampler are the plugin's
 * (PLUGIN_ONLY), and are not run here */
static void
samp_create_pixel(struct run *r, unsigned slot, VkFilter filter, VkSamplerAddressMode address)
{
   (void)r; (void)slot; (void)filter; (void)address;
}

/* argument buffers need the plugin's resource IDs and argument encoders: their tests are PLUGIN_ONLY */
static uint64_t ab_addr(struct run *r, unsigned i, uint64_t off) { (void)r; (void)i; (void)off; return 0; }
static uint64_t ab_tex(struct run *r, unsigned i) { (void)r; (void)i; return 0; }
static uint64_t ab_samp(struct run *r, unsigned i) { (void)r; (void)i; return 0; }
static unsigned ab_enc_begin(struct run *r, unsigned index, int nested, unsigned dst, size_t off)
{
   (void)r; (void)index; (void)nested; (void)dst; (void)off;
   return 0;
}
static void ab_enc_tex(struct run *r, unsigned id, unsigned i) { (void)r; (void)id; (void)i; }
static void ab_enc_samp(struct run *r, unsigned id, unsigned i) { (void)r; (void)id; (void)i; }
static void ab_enc_buf(struct run *r, unsigned id, unsigned i, size_t off) { (void)r; (void)id; (void)i; (void)off; }
static void *ab_enc_const(struct run *r, unsigned id) { (void)r; (void)id; return NULL; }

static float h2f(uint16_t h);
static float
half_bits_to_float(uint16_t h)
{
   return h2f(h);
}

/* A constexpr sampler from its two AIR words, as the plugin makes one from the translator's StaticSampler state
 * (plugin/NVMTLEmbedded.m nvmtl_static_sampler_from: an MTLSamplerDescriptor, then the plugin's VkSampler). */
static VkSampler
static_sampler(uint64_t w)
{
   static const VkFilter filters[3] = { VK_FILTER_NEAREST, VK_FILTER_LINEAR, VK_FILTER_LINEAR };
   static const VkSamplerAddressMode modes[4] = { VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER, VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
                                                  VK_SAMPLER_ADDRESS_MODE_REPEAT, VK_SAMPLER_ADDRESS_MODE_MIRRORED_REPEAT };
   static const VkCompareOp ops[9] = { VK_COMPARE_OP_NEVER, VK_COMPARE_OP_LESS, VK_COMPARE_OP_LESS_OR_EQUAL,
                                       VK_COMPARE_OP_GREATER, VK_COMPARE_OP_GREATER_OR_EQUAL, VK_COMPARE_OP_EQUAL,
                                       VK_COMPARE_OP_NOT_EQUAL, VK_COMPARE_OP_ALWAYS, VK_COMPARE_OP_NEVER };
   unsigned mip = (w >> 13) & 3, cmp = (w >> 16) & 0xf;
   float lo = half_bits_to_float((uint16_t)(w >> 24)), hi = half_bits_to_float((uint16_t)(w >> 40));
   VkSamplerCreateInfo sci = { VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO, NULL, 0, filters[(w >> 9) & 3], filters[(w >> 11) & 3],
                               mip == 2 ? VK_SAMPLER_MIPMAP_MODE_LINEAR : VK_SAMPLER_MIPMAP_MODE_NEAREST,
                               modes[w & 3], modes[(w >> 3) & 3], modes[(w >> 6) & 3], 0, VK_FALSE, 1,
                               cmp ? VK_TRUE : VK_FALSE, ops[cmp < 9 ? cmp : 0], mip ? lo : 0, mip ? hi : 0.25f,
                               VK_BORDER_COLOR_FLOAT_TRANSPARENT_BLACK, VK_FALSE };
   VkSampler s;
   CK(vkCreateSampler(dev, &sci, NULL, &s));
   return s;
}

static void
image_barrier(VkImage image, bool depth, VkImageLayout from, VkImageLayout to, VkAccessFlags src, VkAccessFlags dst,
              VkPipelineStageFlags ss, VkPipelineStageFlags ds)
{
   VkImageMemoryBarrier b = { VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER, NULL, src, dst, from, to, VK_QUEUE_FAMILY_IGNORED,
                              VK_QUEUE_FAMILY_IGNORED, image,
                              { depth ? VK_IMAGE_ASPECT_DEPTH_BIT : VK_IMAGE_ASPECT_COLOR_BIT, 0, VK_REMAINING_MIP_LEVELS, 0,
                                VK_REMAINING_ARRAY_LAYERS } };
   vkCmdPipelineBarrier(cmd, ss, ds, 0, 0, NULL, 0, NULL, 1, &b);
}

static void
tex_copy(struct timg *t, bool to_image)
{
   VkBufferImageCopy c[16];
   for (uint32_t m = 0; m < t->mips && m < 16; m++) {
      c[m] = (VkBufferImageCopy){ tex_level_offset(t, m), 0, 0,
                                  { t->depth ? VK_IMAGE_ASPECT_DEPTH_BIT : VK_IMAGE_ASPECT_COLOR_BIT, m, 0, t->layers },
                                  { 0, 0, 0 }, { mip_size(t->w, m), mip_size(t->h, m), mip_size(t->d, m) } };
   }
   if (to_image)
      vkCmdCopyBufferToImage(cmd, t->stage.buffer, t->image, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, t->mips, c);
   else
      vkCmdCopyImageToBuffer(cmd, t->image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, t->stage.buffer, t->mips, c);
}


static void *
slot(struct run *r, unsigned i, size_t bytes)
{
   r->slot[i] = buf_create(bytes, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
   return r->slot[i].map;
}

/* tex_ms is PLUGIN_ONLY: its texture is cleared by a Metal render pass (plugin_run.m) */
static void
tex_ms_clear(struct run *r, unsigned i, uint32_t w, uint32_t h, unsigned samples, const float rgba[4])
{
   (void)r; (void)i; (void)w; (void)h; (void)samples; (void)rgba;
   fprintf(stderr, "airrun: tex_ms_clear is plugin_run's\n");
   exit(2);
}

#include "airrun_tests.h"

/* ---------------------------------------------------------------------------------------------------------------- */
/* Running one                                                                                                       */

static void *
slurp(const char *p, size_t *n)
{
   FILE *f = fopen(p, "rb");
   if (!f)
      return NULL;
   fseek(f, 0, SEEK_END);
   long len = ftell(f);
   fseek(f, 0, SEEK_SET);
   void *b = len > 0 ? malloc((size_t)len) : NULL;
   if (b && fread(b, 1, (size_t)len, f) != (size_t)len) {
      free(b);
      b = NULL;
   }
   fclose(f);
   *n = b ? (size_t)len : 0;
   return b;
}

enum outcome { PASS, FAIL, REFUSED, PIPELINE_FAILED, MISSING };

static void
run_cleanup(struct run *r, struct buf *table, VkPipeline pipe, VkPipelineLayout layout, VkDescriptorSetLayout set_layout)
{
   buf_destroy(table);
   for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++)
      buf_destroy(&r->slot[i]);
   for (unsigned i = 0; i < 16; i++)
      tex_destroy(&r->tex[i]);
   for (unsigned i = 0; i < 32; i++) {
      if (r->samp[i])
         vkDestroySampler(dev, r->samp[i], NULL);
      if (r->statics[i])
         vkDestroySampler(dev, r->statics[i], NULL);
   }
   vkDestroyPipeline(dev, pipe, NULL);
   vkDestroyPipelineLayout(dev, layout, NULL);
   vkDestroyDescriptorSetLayout(dev, set_layout, NULL);
}

static bool only_translate, corrupt;
static const char *llvm_path;
static const char *air_dir;

static enum outcome
run_test(const struct test *t)
{
   char path[2048];
   snprintf(path, sizeof path, "%s/%s.air", air_dir, t->air ? t->air : t->name);
   size_t size;
   void *bc = slurp(path, &size);
   if (!bc) {
      printf("%-16s MISSING  %s\n", t->name, path);
      return MISSING;
   }
   struct nvmtl_air_nir_request req = { sizeof req, 0, llvm_path, bc, size, t->entry,
                                        { t->tg[0], t->tg[1], t->tg[2] } };
   /* AIRRUN_ALIGN4=1: the build the plugin's align guard runs for a misaligned buffer (no buffer access promises more
    * than 4 bytes); every test must pass with it as well */
   if (getenv("AIRRUN_ALIGN4") && getenv("AIRRUN_ALIGN4")[0] == '1')
      req.flags |= NVMTL_AIR_NIR_ALIGN4;
   req.threadgroup_lengths[0] = t->tg_len[0];
   req.threadgroup_lengths[1] = t->tg_len[1];
   uint32_t fc_idx[8], fc_sz[8];
   const uint8_t *fc_pay[8];
   if (t->fc) {
      req.flags |= NVMTL_AIR_NIR_FC_VALUES;
      for (unsigned i = 0; i < t->fc->n; i++) {
         fc_idx[i] = t->fc->v[i].index;
         fc_sz[i] = t->fc->v[i].size;
         fc_pay[i] = t->fc->v[i].bytes;
      }
      req.fc_count = t->fc->n;
      req.fc_indices = fc_idx;
      req.fc_sizes = fc_sz;
      req.fc_payloads = fc_pay;
   }
   struct nvmtl_air_nir_result res = { sizeof res };
   int st = to_nir(dev, &req, &res);
   free(bc);
   if (t->flags & MUST_REFUSE) {
      if (st == NVMTL_AIR_NIR_OK)
         free_nir(res.nir);
      printf("%-16s %s %s\n", t->name, st != NVMTL_AIR_NIR_OK ? "PASS    " : "FAIL    ",
             st != NVMTL_AIR_NIR_OK ? res.message : "translated, but the frontend should refuse it");
      return st != NVMTL_AIR_NIR_OK ? PASS : FAIL;
   }
   if (st != NVMTL_AIR_NIR_OK) {
      printf("%-16s REFUSED(%d) %s\n", t->name, st, res.message);
      return REFUSED;
   }
   if ((req.flags & NVMTL_AIR_NIR_ALIGN4) && res.global_align > 4) {
      printf("%-16s FAIL     an access promises %u-byte alignment in the build without promises\n", t->name, res.global_align);
      free_nir(res.nir);
      return FAIL;
   }

   /* the set layout holds what the result says the kernel reads, with the types the plugin's layout gives those
    * bands: a binding the kernel reads and the result leaves out fails pipeline creation */
   VkDescriptorSetLayoutBinding bind[300];
   unsigned nbind = 0;
   bind[nbind++] = (VkDescriptorSetLayoutBinding){ NVMTL_AIR_NIR_TABLE_BINDING, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC, 1,
                                                   VK_SHADER_STAGE_COMPUTE_BIT, NULL };
#define BIT(m, i) (((m)[(i) / 32] >> ((i) % 32)) & 1)
   for (unsigned i = 0; i < 128; i++) {
      if (BIT(res.texture_mask, i))
         bind[nbind++] = (VkDescriptorSetLayoutBinding){ 32 + i, BIT(res.texbuf_mask, i) ? VK_DESCRIPTOR_TYPE_UNIFORM_TEXEL_BUFFER :
                                                         VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
      if (BIT(res.storage_mask, i))
         bind[nbind++] = (VkDescriptorSetLayoutBinding){ 480 + i, BIT(res.stexbuf_mask, i) ? VK_DESCRIPTOR_TYPE_STORAGE_TEXEL_BUFFER :
                                                         VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
   }
   for (unsigned i = 0; i < 32; i++) {
      if ((res.sampler_mask >> i) & 1)
         bind[nbind++] = (VkDescriptorSetLayoutBinding){ 160 + i, VK_DESCRIPTOR_TYPE_SAMPLER, 1, VK_SHADER_STAGE_COMPUTE_BIT, NULL };
   }
   VkDescriptorSetLayoutCreateInfo dl = { VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO, .bindingCount = nbind,
                                          .pBindings = bind };
   VkDescriptorSetLayout set_layout;
   CK(vkCreateDescriptorSetLayout(dev, &dl, NULL, &set_layout));
   VkPushConstantRange pr = { VK_SHADER_STAGE_COMPUTE_BIT, 0, NVMTL_AIR_NIR_PUSH_BYTES };
   VkPipelineLayoutCreateInfo pl = { VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, .setLayoutCount = 1,
                                     .pSetLayouts = &set_layout, .pushConstantRangeCount = 1, .pPushConstantRanges = &pr };
   VkPipelineLayout layout;
   CK(vkCreatePipelineLayout(dev, &pl, NULL, &layout));

   NirInfo ni = { STYPE_NIR, NULL, res.nir };
   VkComputePipelineCreateInfo cpi = {
      VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
      .stage = { VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, &ni, 0, VK_SHADER_STAGE_COMPUTE_BIT,
                 VK_NULL_HANDLE, "main", NULL },
      .layout = layout,
   };
   VkPipeline pipe;
   VkResult vr = vkCreateComputePipelines(dev, VK_NULL_HANDLE, 1, &cpi, NULL, &pipe);
   free_nir(res.nir);
   if (vr != VK_SUCCESS) {
      printf("%-16s PIPELINE FAILED %d (%s)\n", t->name, vr, res.message);
      vkDestroyPipelineLayout(dev, layout, NULL);
      vkDestroyDescriptorSetLayout(dev, set_layout, NULL);
      return PIPELINE_FAILED;
   }
   if (only_translate) {
      printf("%-16s OK       %s\n", t->name, res.message);
      vkDestroyPipeline(dev, pipe, NULL);
      vkDestroyPipelineLayout(dev, layout, NULL);
      vkDestroyDescriptorSetLayout(dev, set_layout, NULL);
      return PASS;
   }

   struct run r;
   memset(&r, 0, sizeof r);
   r.t = t;
   r.threads = t->tg[0] * t->tg[1] * t->tg[2] * t->groups[0] * t->groups[1] * t->groups[2];
   if (r.threads > 65536) {
      fprintf(stderr, "airrun: %s dispatches %u threads, over the 64K limit\n", t->name, r.threads);
      exit(2);
   }
   rng_state = 0x9e3779b9u ^ (uint32_t)strlen(t->name) * 2654435761u;
   t->setup(&r);
   const char *missing = NULL;
   unsigned missing_index = 0;
   for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++) {
      if ((res.buffer_mask & (1u << i)) && !r.slot[i].buffer)
         missing = "buffer", missing_index = i;
   }
   for (unsigned i = 0; i < 128; i++) {
      bool want = BIT(res.texture_mask, i) || BIT(res.storage_mask, i);
      if (want && (i >= 16 || (!r.tex[i].image && !r.tex[i].bview) ||
                   r.tex[i].is_buffer != (BIT(res.texbuf_mask, i) || BIT(res.stexbuf_mask, i)) ||
                   (BIT(res.storage_mask, i) && !r.tex[i].storage)))
         missing = "texture", missing_index = i;
   }
   /* the constexpr samplers at the slots the result gives them, made from their AIR state as the plugin makes them */
   for (unsigned k = 0; k < res.static_sampler_count && k < 32; k++) {
      unsigned slot = res.static_samplers[k].slot;
      if (slot < 32 && !r.statics[slot])
         r.statics[slot] = static_sampler(res.static_samplers[k].words[0]);
   }
   for (unsigned i = 0; i < 32; i++) {
      if (((res.sampler_mask >> i) & 1) && !r.samp[i] && !r.statics[i])
         missing = "sampler", missing_index = i;
   }
   struct buf table = buf_create(256, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT);
   if (missing) {
      printf("%-16s FAIL     the kernel reads %s %u, which the test does not bind as that\n", t->name, missing, missing_index);
      run_cleanup(&r, &table, pipe, layout, set_layout);
      return FAIL;
   }
   if (t->expect && !t->expect(&r, &res)) {
      printf("%-16s FAIL     %s\n", t->name, r.why);
      run_cleanup(&r, &table, pipe, layout, set_layout);
      return FAIL;
   }

   /* the address table: one 64-bit address per Metal buffer slot */
   uint64_t *addr = table.map;
   for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++)
      addr[i] = r.slot[i].address;
   CK(vkResetDescriptorPool(dev, pool, 0));
   VkDescriptorSet set;
   VkDescriptorSetAllocateInfo dsa = { VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO, .descriptorPool = pool,
                                       .descriptorSetCount = 1, .pSetLayouts = &set_layout };
   CK(vkAllocateDescriptorSets(dev, &dsa, &set));
   VkDescriptorBufferInfo dbi = { table.buffer, 0, 256 };
   VkWriteDescriptorSet w[300];
   VkDescriptorImageInfo ii[300];
   unsigned nw = 0;
   w[nw++] = (VkWriteDescriptorSet){ VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = set, .dstBinding = NVMTL_AIR_NIR_TABLE_BINDING,
                                     .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC,
                                     .pBufferInfo = &dbi };
   for (unsigned i = 0; i < 16; i++) {
      struct timg *x = &r.tex[i];
      for (unsigned storage = 0; storage < 2; storage++) {
         if (!BIT(storage ? res.storage_mask : res.texture_mask, i))
            continue;
         VkWriteDescriptorSet *d = &w[nw];
         *d = (VkWriteDescriptorSet){ VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = set,
                                      .dstBinding = (storage ? 480 : 32) + i, .descriptorCount = 1 };
         if (x->is_buffer) {
            d->descriptorType = storage ? VK_DESCRIPTOR_TYPE_STORAGE_TEXEL_BUFFER : VK_DESCRIPTOR_TYPE_UNIFORM_TEXEL_BUFFER;
            d->pTexelBufferView = &x->bview;
         } else {
            ii[nw] = (VkDescriptorImageInfo){ VK_NULL_HANDLE, x->view, x->storage ? VK_IMAGE_LAYOUT_GENERAL :
                                                                       VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL };
            d->descriptorType = storage ? VK_DESCRIPTOR_TYPE_STORAGE_IMAGE : VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE;
            d->pImageInfo = &ii[nw];
         }
         nw++;
      }
   }
   for (unsigned i = 0; i < 32; i++) {
      if (!((res.sampler_mask >> i) & 1))
         continue;
      ii[nw] = (VkDescriptorImageInfo){ r.statics[i] ? r.statics[i] : r.samp[i], VK_NULL_HANDLE, VK_IMAGE_LAYOUT_UNDEFINED };
      w[nw] = (VkWriteDescriptorSet){ VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = set, .dstBinding = 160 + i,
                                      .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_SAMPLER, .pImageInfo = &ii[nw] };
      nw++;
   }
   vkUpdateDescriptorSets(dev, nw, w, 0, NULL);

   uint32_t push[12] = { 0 };
   for (unsigned i = 0; i < 3; i++) {
      push[i] = t->tg[i] * t->groups[i];   /* threads_per_grid */
      push[9 + i] = t->groups[i];          /* threadgroups_per_grid */
      if (t->flags & GRID_INDIRECT) {
         push[i] = 0x80000000u | t->tg[i];
         push[9 + i] = 0x80000000u;
      }
   }
   if (t->flags & GRID_BASE) {
      push[1] = 2 * t->tg[1] * t->groups[1];   /* the whole grid is twice as high */
      push[10] = 2 * t->groups[1];
      push[4] = t->tg[1] * t->groups[1];       /* thread_base.y */
      push[7] = t->groups[1];                  /* threadgroup_base.y */
   }

   CK(vkResetCommandBuffer(cmd, 0));
   VkCommandBufferBeginInfo cbi = { VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
                                    .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT };
   CK(vkBeginCommandBuffer(cmd, &cbi));
   /* images: uploaded, then in the layout their descriptor names */
   for (unsigned i = 0; i < 16; i++) {
      struct timg *x = &r.tex[i];
      if (!x->image)
         continue;
      image_barrier(x->image, x->depth, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0,
                    VK_ACCESS_TRANSFER_WRITE_BIT, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT);
      tex_copy(x, true);
      image_barrier(x->image, x->depth, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                    x->storage ? VK_IMAGE_LAYOUT_GENERAL : VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                    VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT,
                    VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
   }
   vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, pipe);
   uint32_t dyn = 0;
   vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, layout, 0, 1, &set, 1, &dyn);
   vkCmdPushConstants(cmd, layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof push, push);
   vkCmdDispatch(cmd, t->groups[0], t->groups[1], t->groups[2]);
   /* written images: copied back into their staging copy */
   for (unsigned i = 0; i < 16; i++) {
      struct timg *x = &r.tex[i];
      if (!x->image || !x->storage)
         continue;
      image_barrier(x->image, x->depth, VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                    VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                    VK_PIPELINE_STAGE_TRANSFER_BIT);
      tex_copy(x, false);
   }
   VkMemoryBarrier mb = { VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT | VK_ACCESS_TRANSFER_WRITE_BIT,
                          .dstAccessMask = VK_ACCESS_HOST_READ_BIT };
   vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT,
                        0, 1, &mb, 0, NULL, 0, NULL);
   CK(vkEndCommandBuffer(cmd));

   int64_t before[NCOUNTERS], after[NCOUNTERS];
   read_counters(before);
   CK(vkResetFences(dev, 1, &fence));
   VkSubmitInfo si = { VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &cmd };
   CK(vkQueueSubmit(queue, 1, &si, fence));
   vr = vkWaitForFences(dev, 1, &fence, VK_TRUE, 2000000000ull);
   read_counters(after);
   bool rose = false;
   for (unsigned i = 0; i < NCOUNTERS; i++)
      rose |= after[i] != before[i];
   if (vr != VK_SUCCESS || rose) {
      printf("%-16s STOP     %s; no more GPU work\n", t->name,
             vr == VK_TIMEOUT ? "the fence did not signal in 2 s" : vr != VK_SUCCESS ? "the fence wait failed" :
                                                                   "a fault counter rose");
      print_counters("before", before);
      print_counters("after", after);
      fflush(stdout);
      _exit(3);
   }

   if (corrupt) {
      /* the self-test: a result with the top byte of its first word changed in every buffer must not pass */
      for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++) {
         if (r.slot[i].map)
            ((uint8_t *)r.slot[i].map)[3] ^= 0x40;
      }
      for (unsigned i = 0; i < 16; i++) {
         if (r.tex[i].stage.map)
            ((uint8_t *)r.tex[i].stage.map)[3] ^= 0x40;
      }
      bool caught = !t->check(&r);
      printf("%-16s %s %s\n", t->name, caught ? "PASS    " : "FAIL    ", caught ? r.why : "a corrupted result passed the check");
      run_cleanup(&r, &table, pipe, layout, set_layout);
      return caught ? PASS : FAIL;
   }
   bool ok = t->check(&r);
   if (ok)
      printf("%-16s PASS     %u threads (%ux%ux%u x %ux%ux%u)  %s\n", t->name, r.threads, t->groups[0], t->groups[1],
             t->groups[2], t->tg[0], t->tg[1], t->tg[2], res.message);
   else
      printf("%-16s FAIL     %s\n", t->name, r.why);
   fflush(stdout);
   run_cleanup(&r, &table, pipe, layout, set_layout);
   return ok ? PASS : FAIL;
}

/* ---------------------------------------------------------------------------------------------------------------- */

int
main(int argc, char **argv)
{
   if (argc < 4) {
      fprintf(stderr, "usage: airrun NVK.dylib LLVM.dylib AIRDIR [TEST...]\n");
      return 2;
   }
   llvm_path = argv[2];
   air_dir = argv[3];
   only_translate = getenv("AIRRUN_ONLY_TRANSLATE") && atoi(getenv("AIRRUN_ONLY_TRANSLATE"));
   corrupt = getenv("AIRRUN_CORRUPT") && atoi(getenv("AIRRUN_CORRUPT"));
   void *lib = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
   if (!lib) {
      fprintf(stderr, "airrun: %s\n", dlerror());
      return 2;
   }
   PFN_vkGetInstanceProcAddr gipa = (PFN_vkGetInstanceProcAddr)dlsym(lib, "vk_icdGetInstanceProcAddr");
   to_nir = (nvmtl_air_to_nir_v1_fn)dlsym(lib, "nvmtl_air_to_nir_v1");
   free_nir = (nvmtl_air_nir_free_v1_fn)dlsym(lib, "nvmtl_air_nir_free_v1");
   if (!gipa || !to_nir || !free_nir) {
      fprintf(stderr, "airrun: %s lacks vk_icdGetInstanceProcAddr or the AIR-to-NIR exports\n", argv[1]);
      return 2;
   }

   PFN_vkCreateInstance create_instance = (PFN_vkCreateInstance)gipa(NULL, "vkCreateInstance");
   VkApplicationInfo app = { VK_STRUCTURE_TYPE_APPLICATION_INFO, .pApplicationName = "airrun",
                             .apiVersion = VK_API_VERSION_1_3 };
   VkInstanceCreateInfo ici = { VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app };
   CK(create_instance(&ici, NULL, &inst));
#define X(n)                                                                                                     \
   n = (PFN_##n)gipa(inst, #n);                                                                                  \
   if (!n) {                                                                                                     \
      fprintf(stderr, "airrun: no %s\n", #n);                                                                    \
      return 2;                                                                                                  \
   }
   VK_FUNCS(X)
#undef X
   uint32_t count = 1;
   vkEnumeratePhysicalDevices(inst, &count, &pdev);
   if (!count) {
      fprintf(stderr, "airrun: NVK sees no card\n");
      return 2;
   }
   VkPhysicalDeviceProperties props;
   vkGetPhysicalDeviceProperties(pdev, &props);
   VkPhysicalDeviceMemoryProperties mp;
   vkGetPhysicalDeviceMemoryProperties(pdev, &mp);
   mem_props = mp;
   /* host-visible coherent memory, system RAM first */
   const VkMemoryPropertyFlags want = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
   for (uint32_t i = 0; i < mp.memoryTypeCount && mem_type == ~0u; i++) {
      if ((mp.memoryTypes[i].propertyFlags & want) == want &&
          !(mp.memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT))
         mem_type = i;
   }
   for (uint32_t i = 0; i < mp.memoryTypeCount && mem_type == ~0u; i++) {
      if ((mp.memoryTypes[i].propertyFlags & want) == want)
         mem_type = i;
   }
   if (mem_type == ~0u) {
      fprintf(stderr, "airrun: no host-visible coherent memory\n");
      return 2;
   }
   uint32_t nq = 16;
   VkQueueFamilyProperties qp[16];
   vkGetPhysicalDeviceQueueFamilyProperties(pdev, &nq, qp);
   uint32_t qf = 0;
   while (qf < nq && !(qp[qf].queueFlags & VK_QUEUE_COMPUTE_BIT))
      qf++;
   if (qf == nq) {
      fprintf(stderr, "airrun: no compute queue\n");
      return 2;
   }
   printf("device %s, memory type %u (flags 0x%x), queue family %u\n", props.deviceName, mem_type,
          mp.memoryTypes[mem_type].propertyFlags, qf);

   float prio = 1;
   VkDeviceQueueCreateInfo qci = { VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = qf,
                                   .queueCount = 1, .pQueuePriorities = &prio };
   VkPhysicalDeviceVulkan12Features f12 = { VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES,
                                            .bufferDeviceAddress = VK_TRUE };
   /* the plugin's device reads and writes storage images without a format (it tells the translator so) */
   VkPhysicalDeviceFeatures feat = { .shaderStorageImageReadWithoutFormat = VK_TRUE,
                                     .shaderStorageImageWriteWithoutFormat = VK_TRUE };
   VkDeviceCreateInfo dci = { VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, &f12, .queueCreateInfoCount = 1,
                              .pQueueCreateInfos = &qci, .pEnabledFeatures = &feat };
   CK(vkCreateDevice(pdev, &dci, NULL, &dev));
#define X(n)                                                                                                     \
   n = (PFN_##n)vkGetDeviceProcAddr(dev, #n);                                                                    \
   if (!n) {                                                                                                     \
      fprintf(stderr, "airrun: no %s\n", #n);                                                                    \
      return 2;                                                                                                  \
   }
   DEV_FUNCS(X)
#undef X
   vkGetDeviceQueue(dev, qf, 0, &queue);

   VkDescriptorPoolSize ps[6] = { { VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC, 1 }, { VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE, 128 },
                                  { VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 128 }, { VK_DESCRIPTOR_TYPE_SAMPLER, 32 },
                                  { VK_DESCRIPTOR_TYPE_UNIFORM_TEXEL_BUFFER, 128 }, { VK_DESCRIPTOR_TYPE_STORAGE_TEXEL_BUFFER, 128 } };
   VkDescriptorPoolCreateInfo dpi = { VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, .maxSets = 1, .poolSizeCount = 6,
                                      .pPoolSizes = ps };
   CK(vkCreateDescriptorPool(dev, &dpi, NULL, &pool));
   VkCommandPoolCreateInfo cpci = { VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
                                    .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT, .queueFamilyIndex = qf };
   CK(vkCreateCommandPool(dev, &cpci, NULL, &cmd_pool));
   VkCommandBufferAllocateInfo cba = { VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, .commandPool = cmd_pool,
                                       .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1 };
   CK(vkAllocateCommandBuffers(dev, &cba, &cmd));
   VkFenceCreateInfo fci = { VK_STRUCTURE_TYPE_FENCE_CREATE_INFO };
   CK(vkCreateFence(dev, &fci, NULL, &fence));

   int64_t start[NCOUNTERS], end[NCOUNTERS];
   read_counters(start);
   print_counters("at start", start);
   unsigned n[5] = { 0 };
   for (unsigned i = 0; i < sizeof tests / sizeof tests[0]; i++) {
      bool chosen = argc == 4;
      for (int a = 4; a < argc && !chosen; a++)
         chosen = !strcmp(argv[a], tests[i].name);
      if (chosen && (tests[i].flags & PLUGIN_ONLY))
         printf("%-16s SKIPPED  the plugin's (it binds a pixel-coordinate sampler)\n", tests[i].name);
      else if (chosen)
         n[run_test(&tests[i])]++;
   }
   read_counters(end);
   print_counters("at end", end);
   printf("pass %u, fail %u, refused %u, pipeline failed %u, missing %u\n", n[PASS], n[FAIL], n[REFUSED],
          n[PIPELINE_FAILED], n[MISSING]);

   vkDestroyFence(dev, fence, NULL);
   vkDestroyCommandPool(dev, cmd_pool, NULL);
   vkDestroyDescriptorPool(dev, pool, NULL);
   vkDestroyDevice(dev, NULL);
   vkDestroyInstance(inst, NULL);
   return n[FAIL] || n[PIPELINE_FAILED] ? 1 : 0;
}
