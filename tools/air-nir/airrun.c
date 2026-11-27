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
 * AIRRUN_ONLY_TRANSLATE=1 stops after pipeline creation (no dispatch). AIRRUN_CORRUPT=1 is the harness's own test: it
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
   X(vkResetCommandBuffer) X(vkDeviceWaitIdle)

#define X(n) static PFN_##n n;
VK_FUNCS(X)
DEV_FUNCS(X)
#undef X

static VkInstance inst;
static VkPhysicalDevice pdev;
static VkDevice dev;
static VkQueue queue;
static uint32_t mem_type = ~0u;
static VkDescriptorSetLayout set_layout;
static VkPipelineLayout layout;
static VkDescriptorPool pool;
static VkDescriptorSet set;
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

struct test;
struct run {
   const struct test *t;
   struct buf slot[NVMTL_AIR_NIR_MAX_BUFFERS];
   unsigned threads;                       /* in the whole grid */
   char why[256];                          /* the first mismatch */
};

/* struct test flags: how the push constants describe the grid */
#define GRID_INDIRECT (1u << 0)   /* threads_per_grid = 0x80000000 | threadgroup size, groups 0x80000000: an indirect
                                   * dispatch, the kernel reads the group count from the hardware */
#define GRID_BASE     (1u << 1)   /* the second half of a 64x64 grid: thread_base.y 32, threadgroup_base.y 4 */

struct test {
   const char *name;
   const char *air;       /* AIRDIR/<air>.air */
   const char *entry;     /* the kernel's name */
   unsigned tg[3];        /* threads per threadgroup */
   unsigned groups[3];    /* threadgroups in the grid */
   unsigned tg_len[2];    /* bytes of [[threadgroup(0)]] and [[threadgroup(1)]] */
   unsigned flags;
   void (*setup)(struct run *);
   bool (*check)(struct run *);
};

static uint32_t rng_state;

static uint32_t
rnd(void)
{
   uint32_t x = rng_state;
   x ^= x << 13;
   x ^= x >> 17;
   x ^= x << 5;
   return rng_state = x;
}

static float
rndf(float lo, float hi)
{
   return lo + (hi - lo) * (float)(rnd() >> 8) * (1.0f / 16777216.0f);
}

static void *
slot(struct run *r, unsigned i, size_t bytes)
{
   r->slot[i] = buf_create(bytes, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
   return r->slot[i].map;
}

static void *
S(struct run *r, unsigned i)
{
   return r->slot[i].map;
}

#define FAILF(r, ...) (snprintf((r)->why, sizeof (r)->why, __VA_ARGS__), false)

static bool
check_u32(struct run *r, const char *what, const uint32_t *got, const uint32_t *want, unsigned n, unsigned stride)
{
   for (unsigned i = 0; i < n; i++) {
      if (got[i] != want[i]) {
         if (stride > 1)
            return FAILF(r, "%s[%u] (thread %u, value %u) = 0x%08x (%u), expected 0x%08x (%u)", what, i, i / stride,
                         i % stride, got[i], got[i], want[i], want[i]);
         return FAILF(r, "%s[%u] = 0x%08x (%u), expected 0x%08x (%u)", what, i, got[i], got[i], want[i], want[i]);
      }
   }
   return true;
}

/* |got - want| <= abs_tol + rel_tol * |want| (NaNs and infinities must match) */
static bool
check_f32(struct run *r, const char *what, const float *got, const float *want, unsigned n, unsigned stride,
          const float *abs_tol, const float *rel_tol)
{
   for (unsigned i = 0; i < n; i++) {
      float g = got[i], w = want[i];
      unsigned k = i % stride;
      bool ok;
      if (isnan(w) || isinf(w))
         ok = (isnan(w) && isnan(g)) || g == w;
      else
         ok = !isnan(g) && fabsf(g - w) <= abs_tol[k] + rel_tol[k] * fabsf(w);
      if (!ok)
         return FAILF(r, "%s[%u] (thread %u, value %u) = %.9g, expected %.9g", what, i, i / stride, k, g, w);
   }
   return true;
}

/* half <-> float, round to nearest even */
static uint16_t
f2h(float f)
{
   uint32_t x;
   memcpy(&x, &f, 4);
   uint32_t sign = (x >> 16) & 0x8000u, e = (x >> 23) & 0xff, m = x & 0x7fffffu;
   if (e == 0xff)
      return (uint16_t)(sign | 0x7c00u | (m ? 0x200u : 0));
   int ee = (int)e - 127 + 15;
   if (ee >= 31)
      return (uint16_t)(sign | 0x7c00u);
   if (ee <= 0) {
      if (ee < -10)
         return (uint16_t)sign;
      m |= 0x800000u;
      unsigned shift = (unsigned)(14 - ee);
      uint32_t h = m >> shift, rem = m & ((1u << shift) - 1), half = 1u << (shift - 1);
      if (rem > half || (rem == half && (h & 1)))
         h++;
      return (uint16_t)(sign | h);
   }
   uint32_t h = ((uint32_t)ee << 10) | (m >> 13), rem = m & 0x1fffu;
   if (rem > 0x1000u || (rem == 0x1000u && (h & 1)))
      h++;
   return (uint16_t)(sign | h);
}

static float
h2f(uint16_t h)
{
   uint32_t sign = (uint32_t)(h & 0x8000u) << 16, e = (h >> 10) & 0x1f, m = h & 0x3ffu, x;
   if (e == 0x1f)
      x = sign | 0x7f800000u | (m << 13);
   else if (e)
      x = sign | ((e + 112) << 23) | (m << 13);
   else if (!m)
      x = sign;
   else {
      e = 113;
      while (!(m & 0x400u)) {
         m <<= 1;
         e--;
      }
      x = sign | (e << 23) | ((m & 0x3ffu) << 13);
   }
   float f;
   memcpy(&f, &x, 4);
   return f;
}

static uint32_t
fbits(float f)
{
   uint32_t u;
   memcpy(&u, &f, 4);
   return u;
}

/* --- fmath, fmath_precise ------------------------------------------------------------------------------------- */

static void
fmath_setup(struct run *r)
{
   unsigned n = r->threads;
   float *a = slot(r, 0, n * 4), *b = slot(r, 1, n * 4), *c = slot(r, 2, n * 4);
   slot(r, 3, n * 12 * 4);
   for (unsigned i = 0; i < n; i++) {
      a[i] = rndf(-4, 4);
      b[i] = rndf(0.5f, 4) * ((rnd() & 1) ? -1.0f : 1.0f);
      c[i] = rndf(-4, 4);
   }
}

static bool
fmath_check(struct run *r)
{
   unsigned n = r->threads;
   const float *a = S(r, 0), *b = S(r, 1), *c = S(r, 2);
   float *want = calloc(n * 12, 4);
   for (unsigned i = 0; i < n; i++) {
      float x = a[i], y = b[i], z = c[i], *o = want + i * 12;
      o[0] = x + y;
      o[1] = x * y;
      o[2] = fmaf(x, y, z);
      o[3] = sqrtf(fabsf(x));
      o[4] = expf(x);
      o[5] = sinf(x);
      o[6] = x / y;
      o[7] = 1.0f / sqrtf(fabsf(y) + 1.0f);
      o[8] = log2f(fabsf(z) + 1.0f);
      o[9] = floorf(x * 3.0f);
      o[10] = fmaxf(x, y) - fminf(y, z);
      o[11] = cosf(z) + tanhf(y * 0.25f);
   }
   /* the GPU's transcendental units are approximations: these bounds check the translation, not Metal's ulp
    * limits */
   static const float abs_tol[12] = { 0, 0, 0, 0, 0, 2e-5f, 0, 0, 2e-5f, 0, 0, 1e-4f };
   static const float rel_tol[12] = { 1e-6f, 1e-6f, 1e-6f, 2e-6f, 2e-5f, 0, 4e-6f, 4e-6f, 1e-5f, 0, 0, 0 };
   bool ok = check_f32(r, "out", S(r, 3), want, n * 12, 12, abs_tol, rel_tol);
   free(want);
   return ok;
}

/* --- imath ---------------------------------------------------------------------------------------------------- */

static void
imath_setup(struct run *r)
{
   unsigned n = r->threads;
   int32_t *a = slot(r, 0, n * 4), *b = slot(r, 1, n * 4);
   slot(r, 2, n * 16 * 4);
   for (unsigned i = 0; i < n; i++) {
      a[i] = (int32_t)rnd();
      int32_t y = (int32_t)rnd() >> (rnd() % 31);
      if (y == 0 || (y == -1 && a[i] == INT32_MIN))
         y = 7;
      b[i] = y;
   }
   a[0] = 0;
   b[0] = 1;
   a[1] = -7;
   b[1] = 2;
   a[2] = INT32_MIN;
   b[2] = 3;
}

static bool
imath_check(struct run *r)
{
   unsigned n = r->threads;
   const int32_t *a = S(r, 0), *b = S(r, 1);
   uint32_t *want = calloc(n * 16, 4);
   for (unsigned i = 0; i < n; i++) {
      int32_t x = a[i], y = b[i];
      uint32_t ux = (uint32_t)x, uy = (uint32_t)y, s = uy & 31u, *o = want + i * 16;
      o[0] = (uint32_t)(x / y);
      o[1] = (uint32_t)(x % y);
      o[2] = ux / uy;
      o[3] = ux % uy;
      o[4] = ux << s;
      o[5] = ux >> s;
      o[6] = (uint32_t)(x >> s);
      o[7] = (uint32_t)(x < y ? x : y);
      o[8] = (uint32_t)(x > y ? x : y);
      o[9] = ux < uy ? ux : uy;
      o[10] = ux > uy ? ux : uy;
      o[11] = (uint32_t)__builtin_popcount(ux);
      o[12] = ux ? (uint32_t)__builtin_clz(ux) : 32u;
      o[13] = uy ? (uint32_t)__builtin_ctz(uy) : 32u;
      o[14] = (uint32_t)(((uint64_t)ux * uy) >> 32);
      uint32_t rot = s ? (ux << s) | (ux >> (32 - s)) : ux;
      int32_t d = (int32_t)(ux - uy);
      uint32_t ad = d < 0 ? 0u - (uint32_t)d : (uint32_t)d;
      o[15] = rot ^ (ux * uy) ^ ad;
   }
   bool ok = check_u32(r, "out", S(r, 2), want, n * 16, 16);
   free(want);
   return ok;
}

/* --- conv ----------------------------------------------------------------------------------------------------- */

static void
conv_setup(struct run *r)
{
   unsigned n = r->threads;
   float *f = slot(r, 0, n * 4);
   int32_t *k = slot(r, 1, n * 4);
   uint16_t *h = slot(r, 2, n * 2);
   slot(r, 3, n * 10 * 4);
   slot(r, 4, n * 2 * 2);
   for (unsigned i = 0; i < n; i++) {
      f[i] = rndf(-1000, 1000);
      k[i] = (int32_t)rnd();
      h[i] = f2h(rndf(-100, 100));
   }
   f[0] = 0.5f;
   f[1] = -0.5f;
   f[2] = 2.5f;
   f[3] = 1.00048828125f;   /* a tie between two halves: rounds to even */
}

static bool
conv_check(struct run *r)
{
   unsigned n = r->threads;
   const float *f = S(r, 0);
   const int32_t *kk = S(r, 1);
   const uint16_t *hh = S(r, 2);
   uint32_t *want = calloc(n * 10, 4);
   uint16_t *hwant = calloc(n * 2, 2);
   for (unsigned i = 0; i < n; i++) {
      float x = f[i], y = h2f(hh[i]);
      int32_t k = kk[i];
      uint32_t *o = want + i * 10;
      o[0] = (uint32_t)(int32_t)x;
      o[1] = (uint32_t)fabsf(x);
      o[2] = fbits((float)k);
      o[3] = fbits((float)(uint32_t)k);
      o[4] = fbits(h2f(f2h(x)));
      o[5] = (uint32_t)(int32_t)y;
      o[6] = fbits(y * 2.0f);
      o[7] = (uint32_t)(int32_t)(int16_t)k;
      o[8] = (uint32_t)(uint8_t)k | ((uint32_t)(uint16_t)k << 8);
      o[9] = (uint32_t)(int32_t)(int8_t)(k >> 4);
      hwant[i * 2] = f2h(h2f(f2h(x)) + y);
      hwant[i * 2 + 1] = f2h(h2f(f2h((float)(k & 255))) * y);
   }
   bool ok = check_u32(r, "out", S(r, 3), want, n * 10, 10);
   if (ok) {
      const uint16_t *g = S(r, 4);
      for (unsigned i = 0; i < n * 2 && ok; i++) {
         if (g[i] != hwant[i])
            ok = FAILF(r, "hout[%u] (thread %u) = 0x%04x (%g), expected 0x%04x (%g)", i, i / 2, g[i], h2f(g[i]),
                       hwant[i], h2f(hwant[i]));
      }
   }
   free(want);
   free(hwant);
   return ok;
}

/* --- loop_phi ------------------------------------------------------------------------------------------------- */

static void
loop_phi_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, n * 3 * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = 1 + rnd() % 100000;
   in[0] = 1;
   in[1] = 27;
   in[2] = 15;
}

static bool
loop_phi_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0);
   uint32_t *want = calloc(n * 3, 4);
   for (unsigned i = 0; i < n; i++) {
      uint32_t x = in[i], steps = 0, peak = x;
      while (x != 1u) {
         x = (x & 1u) ? 3u * x + 1u : x >> 1;
         peak = peak > x ? peak : x;
         if (++steps >= 500u)
            break;
      }
      uint32_t a = 1, b = 0;
      for (uint32_t j = 0; j < (in[i] & 15u); j++) {
         uint32_t t = a + b;
         b = a;
         a = t;
         if (a > 1000u)
            break;
      }
      want[i * 3] = steps;
      want[i * 3 + 1] = peak;
      want[i * 3 + 2] = a * 65536u + b;
   }
   bool ok = check_u32(r, "out", S(r, 1), want, n * 3, 3);
   free(want);
   return ok;
}

/* --- switch_case ---------------------------------------------------------------------------------------------- */

static void
switch_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, n * 2 * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = i < 64 ? i : rnd();
}

static bool
switch_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0);
   uint32_t *want = calloc(n * 2, 4);
   for (unsigned i = 0; i < n; i++) {
      uint32_t x = in[i], v = 0, t;
      switch (x % 9u) {
      case 0: v = x * 3u; break;
      case 1: v = x + 7u; break;
      case 2: x ^= 0x55u; /* fall through */
      case 3: v = x << 2; break;
      case 5: v = x / 3u; break;
      case 7: v = ~x; break;
      default: v = x - 1u; break;
      }
      static const uint32_t table[8] = { 11, 23, 5, 99, 1000, 7, 64, 3 };
      t = table[x & 7u];
      want[i * 2] = v;
      want[i * 2 + 1] = t;
   }
   bool ok = check_u32(r, "out", S(r, 1), want, n * 2, 2);
   free(want);
   return ok;
}

/* --- tg_reduce ------------------------------------------------------------------------------------------------ */

static void
tg_reduce_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, (n / 256) * 4);
   slot(r, 2, (n / 256) * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = rnd() & 0xfffffu;
}

static bool
tg_reduce_check(struct run *r)
{
   unsigned n = r->threads, groups = n / 256;
   const uint32_t *in = S(r, 0);
   uint32_t *want = calloc(groups, 4);
   float *fwant = calloc(groups, 4);
   for (unsigned g = 0; g < groups; g++) {
      for (unsigned j = 0; j < 256; j++) {
         want[g] += in[g * 256 + j];
         fwant[g] += (float)(in[g * 256 + j] & 1023u) * 0.5f;
      }
   }
   static const float zero[1] = { 0 };
   bool ok = check_u32(r, "sums", S(r, 1), want, groups, 1) &&
             check_f32(r, "fsums", S(r, 2), fwant, groups, 1, zero, zero);
   free(want);
   free(fwant);
   return ok;
}

/* --- tg_arg --------------------------------------------------------------------------------------------------- */

static void
tg_arg_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, n * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = rnd();
}

static bool
tg_arg_check(struct run *r)
{
   unsigned n = r->threads, tg = 128;
   const uint32_t *in = S(r, 0);
   uint32_t *want = calloc(n, 4);
   for (unsigned i = 0; i < n; i++) {
      unsigned base = i / tg * tg, lid = i % tg;
      want[i] = in[base + tg - 1 - lid] + (uint32_t)(uint16_t)(((lid + 1) % tg) * 3);
   }
   bool ok = check_u32(r, "out", S(r, 1), want, n, 1);
   free(want);
   return ok;
}

/* --- dev_atomics ---------------------------------------------------------------------------------------------- */

static void
dev_atomics_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4), *counters = slot(r, 1, 8 * 4);
   slot(r, 2, n * 4);
   slot(r, 3, 16 * 4);
   slot(r, 4, 2 * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = rnd();
   counters[2] = 0xffffffffu;
   counters[4] = 0xffffffffu;
   counters[5] = 1000000000u;
   memset(S(r, 2), 0xff, n * 4);
}

static bool
dev_atomics_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0), *counters = S(r, 1), *slots = S(r, 2), *hist = S(r, 3), *cas = S(r, 4);
   uint32_t want[8] = { n, 0, 0xffffffffu, 0, 0xffffffffu, 1000000000u, 0, 0 }, hwant[16] = { 0 }, cwant[2] = { 0, 7 };
   for (unsigned i = 0; i < n; i++) {
      uint32_t v = in[i];
      hwant[v & 15u]++;
      want[1] = want[1] > v ? want[1] : v;
      want[2] = want[2] < v ? want[2] : v;
      want[3] |= 1u << (v & 31u);
      want[4] &= ~(1u << ((v >> 5) & 31u));
      want[5] -= v & 7u;
      if (v & 1u)
         cwant[0] += v & 255u;
   }
   if (!check_u32(r, "counters", counters, want, 8, 1) || !check_u32(r, "hist", hist, hwant, 16, 1) ||
       !check_u32(r, "cas", cas, cwant, 2, 1))
      return false;
   /* every thread took one slot: the slots hold each thread id once */
   unsigned char *seen = calloc(n, 1);
   bool ok = true;
   for (unsigned i = 0; i < n && ok; i++) {
      if (slots[i] >= n || seen[slots[i]])
         ok = FAILF(r, "slots[%u] = %u: not a thread id, or one seen before", i, slots[i]);
      else
         seen[slots[i]] = 1;
   }
   free(seen);
   return ok;
}

/* --- tg_atomics ----------------------------------------------------------------------------------------------- */

static void
tg_atomics_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, (n / 256) * 18 * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = rnd();
}

static bool
tg_atomics_check(struct run *r)
{
   unsigned n = r->threads, groups = n / 256;
   const uint32_t *in = S(r, 0);
   uint32_t *want = calloc(groups * 18, 4);
   for (unsigned g = 0; g < groups; g++) {
      uint32_t *o = want + g * 18;
      for (unsigned j = 0; j < 256; j++) {
         uint32_t v = in[g * 256 + j];
         o[v & 15u]++;
         o[16] = o[16] > v ? o[16] : v;
         o[17] ^= v;
      }
   }
   bool ok = check_u32(r, "out", S(r, 1), want, groups * 18, 18);
   free(want);
   return ok;
}

/* --- const_table ---------------------------------------------------------------------------------------------- */

static void
const_table_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, n * 3 * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = rnd();
}

static bool
const_table_check(struct run *r)
{
   static const float kF[16] = { 0.5f, 1.25f, -3.0f, 7.75f, 100.0f, -0.125f, 2.0f, 9.5f,
                                 -11.0f, 13.25f, 0.0f, 1e-3f, 42.0f, -42.0f, 3.5f, 6.0f };
   static const uint32_t kU[13] = { 2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41 };
   static const struct { uint32_t key; float scale; int16_t bias; } kE[5] = {
      { 1, 2.0f, -3 }, { 10, 0.5f, 4 }, { 100, -1.0f, 5 }, { 1000, 4.0f, -6 }, { 7, 8.0f, 7 } };
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0);
   float *want = calloc(n * 3, 4);
   for (unsigned i = 0; i < n; i++) {
      uint32_t x = in[i];
      want[i * 3] = kF[x & 15u] * 2.0f;
      want[i * 3 + 1] = (float)kU[(x >> 4) % 13u];
      want[i * 3 + 2] = (float)kE[x % 5u].key * kE[x % 5u].scale + (float)kE[x % 5u].bias;
   }
   static const float zero[3] = { 0, 0, 0 };
   bool ok = check_f32(r, "out", S(r, 1), want, n * 3, 3, zero, zero);
   free(want);
   return ok;
}

/* --- struct_buf ----------------------------------------------------------------------------------------------- */

struct particle {         /* Metal's layout of struct Particle in struct_buf.metal */
   float pos[4];          /* float3: 16 bytes */
   float vel[3];          /* packed_float3 */
   float mass;
   uint32_t id;
   uint32_t pad[3];
};
_Static_assert(sizeof(struct particle) == 48, "Particle is 48 bytes in Metal");

static void
struct_buf_setup(struct run *r)
{
   unsigned n = r->threads;
   struct particle *in = slot(r, 0, n * sizeof *in);
   slot(r, 1, n * sizeof *in);
   float *dt = slot(r, 2, 16);
   *dt = 0.125f;
   for (unsigned i = 0; i < n; i++) {
      for (unsigned k = 0; k < 3; k++) {
         in[i].pos[k] = rndf(-10, 10);
         in[i].vel[k] = rndf(-2, 2);
      }
      in[i].pos[3] = 0;
      in[i].mass = rndf(0, 5);
      in[i].id = rnd() >> 2;
   }
}

static bool
struct_buf_check(struct run *r)
{
   unsigned n = r->threads;
   const struct particle *in = S(r, 0), *got = S(r, 1);
   const float dt = 0.125f;
   for (unsigned i = 0; i < n; i++) {
      float pos[3], vel[3];
      for (unsigned k = 0; k < 3; k++)
         pos[k] = in[i].pos[k] + in[i].vel[k] * dt;
      for (unsigned k = 0; k < 3; k++)
         vel[k] = in[i].vel[k] * 0.5f + pos[(k + 1) % 3];
      float mass = sqrtf(pos[0] * pos[0] + pos[1] * pos[1] + pos[2] * pos[2]) + in[i].mass;
      uint32_t id = in[i].id * 2u + 1u;
      for (unsigned k = 0; k < 3; k++) {
         if (fabsf(got[i].pos[k] - pos[k]) > 1e-5f + 1e-6f * fabsf(pos[k]))
            return FAILF(r, "out[%u].pos[%u] = %.9g, expected %.9g", i, k, got[i].pos[k], pos[k]);
         if (fabsf(got[i].vel[k] - vel[k]) > 1e-5f + 1e-6f * fabsf(vel[k]))
            return FAILF(r, "out[%u].vel[%u] = %.9g, expected %.9g", i, k, got[i].vel[k], vel[k]);
      }
      if (fabsf(got[i].mass - mass) > 1e-5f + 4e-6f * fabsf(mass))
         return FAILF(r, "out[%u].mass = %.9g, expected %.9g", i, got[i].mass, mass);
      if (got[i].id != id)
         return FAILF(r, "out[%u].id = %u, expected %u", i, got[i].id, id);
   }
   return true;
}

/* --- grid2d --------------------------------------------------------------------------------------------------- */

static void
grid2d_setup(struct run *r)
{
   slot(r, 0, 64 * 64 * 4 * 4);
}

static bool
grid2d_check(struct run *r)
{
   /* a 64x32 grid of 16x8 threadgroups; with GRID_BASE, rows 32-63 of a 64x64 one (rows 0-31 stay 0) */
   const bool based = r->t->flags & GRID_BASE;
   const unsigned W = 64, H = based ? 64 : 32, Y0 = based ? 32 : 0, TX = 16, TY = 8;
   uint32_t *want = calloc(64 * 64 * 4, 4);
   for (unsigned y = Y0; y < H; y++) {
      for (unsigned x = 0; x < W; x++) {
         uint32_t *o = want + (y * W + x) * 4;
         o[0] = x | (y << 16);
         o[1] = (x % TX) | ((y % TY) << 8) | ((x / TX) << 16) | ((y / TY) << 24);
         o[2] = W | (H << 16);
         o[3] = (W / TX) | ((H / TY) << 8) | (TX << 16) | (TY << 24);
      }
   }
   bool ok = check_u32(r, "out", S(r, 0), want, 64 * 64 * 4, 4);
   free(want);
   return ok;
}

/* --- simd_builtins -------------------------------------------------------------------------------------------- */

static void
simd_setup(struct run *r)
{
   slot(r, 0, r->threads * 4 * 4);
}

static bool
simd_check(struct run *r)
{
   unsigned n = r->threads, tg = 128;
   uint32_t *want = calloc(n * 4, 4);
   for (unsigned i = 0; i < n; i++) {
      unsigned lid = i % tg;
      want[i * 4] = lid % 32;
      want[i * 4 + 1] = lid / 32;
      want[i * 4 + 2] = 32 | ((tg / 32) << 16);
      want[i * 4 + 3] = lid;
   }
   bool ok = check_u32(r, "out", S(r, 0), want, n * 4, 4);
   free(want);
   return ok;
}

/* --- narrow --------------------------------------------------------------------------------------------------- */

static void
narrow_setup(struct run *r)
{
   unsigned n = r->threads;
   int8_t *a = slot(r, 0, n);
   int16_t *b = slot(r, 1, n * 2);
   uint8_t *c = slot(r, 2, n);
   uint16_t *d = slot(r, 3, n * 2);
   slot(r, 4, n * 2);
   slot(r, 5, n);
   slot(r, 6, n * 3 * 4);
   for (unsigned i = 0; i < n; i++) {
      a[i] = (int8_t)rnd();
      b[i] = (int16_t)rnd();
      c[i] = (uint8_t)rnd();
      d[i] = (uint16_t)rnd();
   }
}

static bool
narrow_check(struct run *r)
{
   unsigned n = r->threads;
   const int8_t *a = S(r, 0), *oc = S(r, 5);
   const int16_t *b = S(r, 1), *os = S(r, 4);
   const uint8_t *c = S(r, 2);
   const uint16_t *d = S(r, 3);
   const int32_t *oi = S(r, 6);
   for (unsigned i = 0; i < n; i++) {
      int16_t ws = (int16_t)(a[i] * b[i]);
      int8_t wc = (int8_t)(a[i] + (int8_t)c[i]);
      int32_t w0 = a[i] * 1000 + b[i], w1 = (int32_t)c[i] - (int32_t)d[i], w2 = (int16_t)d[i] + (uint8_t)a[i];
      if (os[i] != ws)
         return FAILF(r, "os[%u] = %d, expected %d", i, os[i], ws);
      if (oc[i] != wc)
         return FAILF(r, "oc[%u] = %d, expected %d", i, oc[i], wc);
      if (oi[i * 3] != w0 || oi[i * 3 + 1] != w1 || oi[i * 3 + 2] != w2)
         return FAILF(r, "oi[%u..] = %d %d %d, expected %d %d %d", i * 3, oi[i * 3], oi[i * 3 + 1], oi[i * 3 + 2], w0,
                      w1, w2);
   }
   return true;
}

static const struct test tests[] = {
   { "fmath", NULL, "fmath", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, fmath_setup, fmath_check },
   { "fmath_precise", NULL, "fmath_precise", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, fmath_setup, fmath_check },
   { "imath", NULL, "imath", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, imath_setup, imath_check },
   { "conv", NULL, "conv", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, conv_setup, conv_check },
   { "loop_phi", NULL, "loop_phi", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, loop_phi_setup, loop_phi_check },
   { "switch", NULL, "switch_case", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, switch_setup, switch_check },
   { "tg_reduce", NULL, "tg_reduce", { 256, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, tg_reduce_setup, tg_reduce_check },
   { "tg_arg", NULL, "tg_arg", { 128, 1, 1 }, { 32, 1, 1 }, { 128 * 4, 128 * 2 }, 0, tg_arg_setup, tg_arg_check },
   { "dev_atomics", NULL, "dev_atomics", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, dev_atomics_setup, dev_atomics_check },
   { "tg_atomics", NULL, "tg_atomics", { 256, 1, 1 }, { 32, 1, 1 }, { 0 }, 0, tg_atomics_setup, tg_atomics_check },
   { "const_table", NULL, "const_table", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, const_table_setup, const_table_check },
   { "struct_buf", NULL, "struct_buf", { 64, 1, 1 }, { 16, 1, 1 }, { 0 }, 0, struct_buf_setup, struct_buf_check },
   { "grid2d", NULL, "grid2d", { 16, 8, 1 }, { 4, 4, 1 }, { 0 }, 0, grid2d_setup, grid2d_check },
   { "grid2d_indirect", "grid2d", "grid2d", { 16, 8, 1 }, { 4, 4, 1 }, { 0 }, GRID_INDIRECT, grid2d_setup, grid2d_check },
   { "grid2d_base", "grid2d", "grid2d", { 16, 8, 1 }, { 4, 4, 1 }, { 0 }, GRID_BASE, grid2d_setup, grid2d_check },
   { "simd_builtins", NULL, "simd_builtins", { 128, 1, 1 }, { 8, 1, 1 }, { 0 }, 0, simd_setup, simd_check },
   { "narrow", NULL, "narrow", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, narrow_setup, narrow_check },
};

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
   req.threadgroup_lengths[0] = t->tg_len[0];
   req.threadgroup_lengths[1] = t->tg_len[1];
   struct nvmtl_air_nir_result res = { sizeof res };
   int st = to_nir(dev, &req, &res);
   free(bc);
   if (st != NVMTL_AIR_NIR_OK) {
      printf("%-16s REFUSED(%d) %s\n", t->name, st, res.message);
      return REFUSED;
   }

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
      return PIPELINE_FAILED;
   }
   if (only_translate) {
      printf("%-16s OK       %s\n", t->name, res.message);
      vkDestroyPipeline(dev, pipe, NULL);
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
   for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++) {
      if ((res.buffer_mask & (1u << i)) && !r.slot[i].buffer) {
         printf("%-16s FAIL     the kernel reads buffer %u, which the test does not bind\n", t->name, i);
         vkDestroyPipeline(dev, pipe, NULL);
         return FAIL;
      }
   }

   /* the address table: one 64-bit address per Metal buffer slot */
   struct buf table = buf_create(256, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT);
   uint64_t *addr = table.map;
   for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++)
      addr[i] = r.slot[i].address;
   VkDescriptorBufferInfo dbi = { table.buffer, 0, 256 };
   VkWriteDescriptorSet w = { VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = set, .dstBinding = NVMTL_AIR_NIR_TABLE_BINDING,
                              .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC,
                              .pBufferInfo = &dbi };
   vkUpdateDescriptorSets(dev, 1, &w, 0, NULL);

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
   vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, pipe);
   uint32_t dyn = 0;
   vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, layout, 0, 1, &set, 1, &dyn);
   vkCmdPushConstants(cmd, layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof push, push);
   vkCmdDispatch(cmd, t->groups[0], t->groups[1], t->groups[2]);
   VkMemoryBarrier mb = { VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT,
                          .dstAccessMask = VK_ACCESS_HOST_READ_BIT };
   vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &mb, 0, NULL, 0,
                        NULL);
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
      bool caught = !t->check(&r);
      printf("%-16s %s %s\n", t->name, caught ? "PASS    " : "FAIL    ", caught ? r.why : "a corrupted result passed the check");
      buf_destroy(&table);
      for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++)
         buf_destroy(&r.slot[i]);
      vkDestroyPipeline(dev, pipe, NULL);
      return caught ? PASS : FAIL;
   }
   bool ok = t->check(&r);
   if (ok)
      printf("%-16s PASS     %u threads (%ux%ux%u x %ux%ux%u)  %s\n", t->name, r.threads, t->groups[0], t->groups[1],
             t->groups[2], t->tg[0], t->tg[1], t->tg[2], res.message);
   else
      printf("%-16s FAIL     %s\n", t->name, r.why);
   fflush(stdout);
   buf_destroy(&table);
   for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++)
      buf_destroy(&r.slot[i]);
   vkDestroyPipeline(dev, pipe, NULL);
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
   VkDeviceCreateInfo dci = { VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, &f12, .queueCreateInfoCount = 1,
                              .pQueueCreateInfos = &qci };
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

   VkDescriptorSetLayoutBinding bind = { NVMTL_AIR_NIR_TABLE_BINDING, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC, 1,
                                         VK_SHADER_STAGE_COMPUTE_BIT, NULL };
   VkDescriptorSetLayoutCreateInfo dl = { VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO, .bindingCount = 1,
                                          .pBindings = &bind };
   CK(vkCreateDescriptorSetLayout(dev, &dl, NULL, &set_layout));
   VkPushConstantRange pr = { VK_SHADER_STAGE_COMPUTE_BIT, 0, NVMTL_AIR_NIR_PUSH_BYTES };
   VkPipelineLayoutCreateInfo pl = { VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, .setLayoutCount = 1,
                                     .pSetLayouts = &set_layout, .pushConstantRangeCount = 1,
                                     .pPushConstantRanges = &pr };
   CK(vkCreatePipelineLayout(dev, &pl, NULL, &layout));
   VkDescriptorPoolSize ps = { VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC, 1 };
   VkDescriptorPoolCreateInfo dpi = { VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, .maxSets = 1, .poolSizeCount = 1,
                                      .pPoolSizes = &ps };
   CK(vkCreateDescriptorPool(dev, &dpi, NULL, &pool));
   VkDescriptorSetAllocateInfo dsa = { VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO, .descriptorPool = pool,
                                       .descriptorSetCount = 1, .pSetLayouts = &set_layout };
   CK(vkAllocateDescriptorSets(dev, &dsa, &set));
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
      if (chosen)
         n[run_test(&tests[i])]++;
   }
   read_counters(end);
   print_counters("at end", end);
   printf("pass %u, fail %u, refused %u, pipeline failed %u, missing %u\n", n[PASS], n[FAIL], n[REFUSED],
          n[PIPELINE_FAILED], n[MISSING]);

   vkDestroyFence(dev, fence, NULL);
   vkDestroyCommandPool(dev, cmd_pool, NULL);
   vkDestroyDescriptorPool(dev, pool, NULL);
   vkDestroyPipelineLayout(dev, layout, NULL);
   vkDestroyDescriptorSetLayout(dev, set_layout, NULL);
   vkDestroyDevice(dev, NULL);
   vkDestroyInstance(inst, NULL);
   return n[FAIL] || n[PIPELINE_FAILED] ? 1 : 0;
}
