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
 * cannot carry yet). AIRRUN_ONLY_TRANSLATE=1 stops after pipeline creation (no dispatch). AIRRUN_CORRUPT=1 is the harness's own test: it
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
#define MUST_REFUSE   (1u << 2)   /* passes only if the frontend refuses the kernel (nothing is dispatched) */

/* function-constant values a test's request carries (NVMTL_AIR_NIR_FC_VALUES) */
struct fcval {
   uint32_t index, size;
   uint8_t bytes[8];
};
struct fcset {
   unsigned n;
   struct fcval v[8];
};

struct test {
   const char *name;
   const char *air;       /* AIRDIR/<air>.air */
   const char *entry;     /* the kernel's name */
   unsigned tg[3];        /* threads per threadgroup */
   unsigned groups[3];    /* threadgroups in the grid */
   unsigned tg_len[2];    /* bytes of [[threadgroup(0)]] and [[threadgroup(1)]] */
   unsigned flags;
   const struct fcset *fc;   /* NULL: the request has no function-constant values */
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

/* --- vec_ops -------------------------------------------------------------------------------------------------- */

static void
vec_ops_setup(struct run *r)
{
   unsigned n = r->threads;
   float *a = slot(r, 0, n * 16), *b = slot(r, 1, n * 16);
   uint32_t *u = slot(r, 2, n * 4);
   slot(r, 3, n * 4 * 16);
   slot(r, 4, n * 16);
   for (unsigned i = 0; i < n * 4; i++) {
      a[i] = rndf(-4, 4);
      b[i] = rndf(-4, 4);
   }
   for (unsigned i = 0; i < n; i++)
      u[i] = rnd();
}

static bool
vec_ops_check(struct run *r)
{
   unsigned n = r->threads;
   const float *a = S(r, 0), *b = S(r, 1);
   const uint32_t *u = S(r, 2);
   float *want = calloc(n * 16, 4);
   uint32_t *iwant = calloc(n * 4, 4);
   for (unsigned i = 0; i < n; i++) {
      const float *x = a + i * 4, *y = b + i * 4;
      float *o = want + i * 16;
      bool gt[4], any = false, all = true;
      for (unsigned k = 0; k < 4; k++) {
         o[k] = x[k] * y[k] + x[3 - k];
         gt[k] = x[k] > y[k];
         any |= gt[k];
         all &= gt[k];
      }
      o[4] = x[1] * y[2] - x[2] * y[1];
      o[5] = x[2] * y[0] - x[0] * y[2];
      o[6] = x[0] * y[1] - x[1] * y[0];
      o[7] = x[0] * y[0] + x[1] * y[1] + x[2] * y[2] + x[3] * y[3];
      float v[3] = { y[0] + 0.5f, y[1] + 0.5f, y[2] + 0.5f };
      float len = sqrtf(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
      for (unsigned k = 0; k < 3; k++)
         o[8 + k] = v[k] / len;
      o[11] = sqrtf(x[0] * x[0] + x[1] * x[1] + x[2] * x[2] + x[3] * x[3]);
      float extra[4] = { any ? 1.0f : 0.0f, all ? 1.0f : 0.0f, x[1], x[1] };
      for (unsigned k = 0; k < 4; k++)
         o[12 + k] = (gt[k] ? y[k] : x[k]) + extra[k];
      uint32_t w = u[i], bytes[4];
      for (unsigned k = 0; k < 4; k++)
         bytes[k] = (w >> (8 * k)) & 255u;
      static const int32_t mul[4] = { 3, -1, 7, 2 };
      uint32_t hw = w & 0x3bff3bffu;
      uint32_t add[4] = { (uint32_t)(h2f((uint16_t)hw) * 1024.0f), (uint32_t)(h2f((uint16_t)(hw >> 16)) * 1024.0f),
                          w >> 24, w & 0xffu };
      for (unsigned k = 0; k < 4; k++) {
         int32_t iv = (int32_t)bytes[k] - 128 + (int32_t)x[k];
         iwant[i * 4 + k] = (uint32_t)(iv * mul[k]) ^ (bytes[3 - k] + add[k]);
      }
   }
   static const float abs_tol[16] = { 1e-6f, 1e-6f, 1e-6f, 1e-6f, 1e-5f, 1e-5f, 1e-5f, 1e-5f,
                                      1e-6f, 1e-6f, 1e-6f, 1e-6f, 0, 0, 0, 0 };
   static const float rel_tol[16] = { 1e-6f, 1e-6f, 1e-6f, 1e-6f, 1e-6f, 1e-6f, 1e-6f, 1e-6f,
                                      1e-5f, 1e-5f, 1e-5f, 4e-6f, 0, 0, 0, 0 };
   bool ok = check_f32(r, "out", S(r, 3), want, n * 16, 16, abs_tol, rel_tol) &&
             check_u32(r, "iout", S(r, 4), iwant, n * 4, 4);
   free(want);
   free(iwant);
   return ok;
}

/* --- tg_transpose --------------------------------------------------------------------------------------------- */

static void
tg_transpose_setup(struct run *r)
{
   float *in = slot(r, 0, 64 * 64 * 4);
   slot(r, 1, 64 * 64 * 4);
   slot(r, 2, 16 * 64 * 16);
   uint32_t *size = slot(r, 3, 16);
   size[0] = 64;
   size[1] = 64;
   for (unsigned i = 0; i < 64 * 64; i++)
      in[i] = (float)i + 0.5f;
}

static bool
tg_transpose_check(struct run *r)
{
   const float *in = S(r, 0);
   float *want = calloc(64 * 64, 4), *vwant = calloc(16 * 64 * 4, 4);
   for (unsigned y = 0; y < 64; y++) {
      for (unsigned x = 0; x < 64; x++)
         want[y * 64 + x] = in[x * 64 + y];
   }
   for (unsigned ty = 0; ty < 4; ty++) {
      for (unsigned tx = 0; tx < 4; tx++) {
         for (unsigned l = 0; l < 64; l++) {
            float *o = vwant + ((ty * 4 + tx) * 64 + l) * 4;
            o[0] = (float)((l * 7u) & 63u) * 2.0f;
            o[1] = (float)tx * 2.0f;
            o[2] = (float)ty * 2.0f;
            o[3] = 2.0f;
         }
      }
   }
   static const float zero[4] = { 0 };
   bool ok = check_f32(r, "out", S(r, 1), want, 64 * 64, 1, zero, zero) &&
             check_f32(r, "vout", S(r, 2), vwant, 16 * 64 * 4, 4, zero, zero);
   free(want);
   free(vwant);
   return ok;
}

/* --- thread_array --------------------------------------------------------------------------------------------- */

static void
thread_array_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 12 * 4);
   slot(r, 1, n * 20 * 4);
   for (unsigned i = 0; i < n * 12; i++)
      in[i] = rnd() % 1000;
}

static bool
thread_array_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0);
   uint32_t *want = calloc(n * 20, 4);
   for (unsigned i = 0; i < n; i++) {
      uint32_t v[12], *o = want + i * 20;
      memcpy(v, in + i * 12, sizeof v);
      for (unsigned k = 0; k < 12; k++)
         o[12 + (v[k] & 7u)]++;
      for (unsigned k = 1; k < 12; k++) {
         uint32_t x = v[k];
         int j = (int)k - 1;
         while (j >= 0 && v[j] > x) {
            v[j + 1] = v[j];
            j--;
         }
         v[j + 1] = x;
      }
      memcpy(o, v, sizeof v);
   }
   bool ok = check_u32(r, "out", S(r, 1), want, n * 20, 20);
   free(want);
   return ok;
}

/* --- ptr_ops -------------------------------------------------------------------------------------------------- */

struct rec {   /* struct Rec in ptr_ops.metal */
   uint32_t a;
   float b;
   uint32_t c[4];
};

static void
ptr_ops_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *x = slot(r, 0, n * 16), *y = slot(r, 1, n * 16);
   struct rec *recs = slot(r, 2, 7 * sizeof(struct rec));
   slot(r, 3, n * 16);
   for (unsigned i = 0; i < n * 4; i++) {
      x[i] = rnd();
      y[i] = rnd();
   }
   for (unsigned i = 0; i < 7; i++) {
      recs[i].a = rnd();
      recs[i].b = rndf(0, 1000);
      for (unsigned k = 0; k < 4; k++)
         recs[i].c[k] = rnd();
   }
}

static bool
ptr_ops_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *x = S(r, 0), *y = S(r, 1);
   const struct rec *recs = S(r, 2);
   uint32_t *want = calloc(n * 4, 4);
   for (unsigned i = 0; i < n; i++) {
      const uint32_t *p = (i & 1u) ? x : y;
      uint32_t s = 0;
      for (unsigned k = 0; k < 4; k++)
         s += p[i * 4 + k];
      const struct rec *rr = recs + i % 7u;
      uint64_t big = (uint64_t)x[i * 4] * 0x100000001ull + i;
      want[i * 4] = s;
      want[i * 4 + 1] = rr->c[i & 3u] + rr->a;
      want[i * 4 + 2] = (uint32_t)(big >> 13) ^ (uint32_t)(big % 1000003ull);
      want[i * 4 + 3] = (uint32_t)(rr->b * 4.0f);
   }
   bool ok = check_u32(r, "out", S(r, 3), want, n * 4, 4);
   free(want);
   return ok;
}

/* --- simd_funcs ----------------------------------------------------------------------------------------------- */

static void
simd_funcs_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   float *fin = slot(r, 1, n * 4);
   slot(r, 2, n * 8 * 4);
   slot(r, 3, n * 2 * 4);
   for (unsigned i = 0; i < n; i++) {
      in[i] = (i / 32) % 2 ? rnd() % 8 : 1 + rnd() % 999;   /* every other SIMD-group has no zero */
      fin[i] = (float)((int)(rnd() % 128) - 64) * 0.25f;
   }
}

static bool
simd_funcs_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0);
   const float *fin = S(r, 1);
   uint32_t *want = calloc(n * 8, 4);
   float *fwant = calloc(n * 2, 4);
   for (unsigned g = 0; g < n / 32; g++) {
      const uint32_t *v = in + g * 32;
      const float *f = fin + g * 32;
      uint32_t sum = 0, mx = 0, mn = ~0u, ballot = 0;
      bool all = true, any = false;
      float fsum = 0;
      for (unsigned l = 0; l < 32; l++) {
         sum += v[l];
         mx = v[l] > mx ? v[l] : mx;
         mn = v[l] < mn ? v[l] : mn;
         ballot |= ((v[l] & 3u) == 0u) << l;
         all &= v[l] != 0u;
         any |= v[l] == 5u;
         fsum += f[l];
      }
      uint32_t pre = 0;
      float fpre = 0;
      for (unsigned l = 0; l < 32; l++) {
         uint32_t *o = want + (g * 32 + l) * 8;
         o[0] = sum;
         o[1] = mx;
         o[2] = pre;
         pre += v[l];
         o[3] = v[(l * 5u + 3u) & 31u];
         o[4] = v[7];
         o[5] = l + 3 < 32 ? v[l + 3] : v[l];
         o[6] = ballot;
         o[7] = (uint32_t)all | ((uint32_t)any << 1) | (mn & ~3u);
         fpre += f[l];
         fwant[(g * 32 + l) * 2] = fsum;
         fwant[(g * 32 + l) * 2 + 1] = fpre;
      }
   }
   static const float zero[2] = { 0, 0 };
   bool ok = check_u32(r, "out", S(r, 2), want, n * 8, 8) && check_f32(r, "fout", S(r, 3), fwant, n * 2, 2, zero, zero);
   free(want);
   free(fwant);
   return ok;
}

/* --- math_lib ------------------------------------------------------------------------------------------------- */

static void
math_lib_setup(struct run *r)
{
   unsigned n = r->threads;
   float *a = slot(r, 0, n * 4), *b = slot(r, 1, n * 4);
   int32_t *k = slot(r, 2, n * 4);
   slot(r, 3, n * 16 * 4);
   slot(r, 4, n * 8 * 4);
   for (unsigned i = 0; i < n; i++) {
      a[i] = rndf(-6, 6);
      b[i] = rndf(0.5f, 3) * ((rnd() & 1) ? -1.0f : 1.0f);
      k[i] = (int32_t)rnd();
   }
   a[0] = 2.5f;
   a[1] = -2.5f;
   a[2] = 0.0f;
   k[0] = INT32_MAX;
   k[1] = 5;
   k[2] = INT32_MIN;
   k[3] = 7;
}

static float
smoothstep_ref(float e0, float e1, float x)
{
   float t = (x - e0) / (e1 - e0);
   t = t < 0 ? 0 : t > 1 ? 1 : t;
   return t * t * (3.0f - 2.0f * t);
}

static bool
math_lib_check(struct run *r)
{
   unsigned n = r->threads;
   const float *a = S(r, 0), *b = S(r, 1);
   const int32_t *kk = S(r, 2);
   float *want = calloc(n * 16, 4);
   uint32_t *iwant = calloc(n * 8, 4);
   for (unsigned i = 0; i < n; i++) {
      float x = a[i], y = b[i], *o = want + i * 16;
      o[0] = roundf(x);
      o[1] = truncf(x);
      o[2] = ceilf(x);
      o[3] = x - floorf(x);
      o[4] = fmodf(x, y);
      o[5] = copysignf(y, x);
      o[6] = x < -1.0f ? -1.0f : x > 2.0f ? 2.0f : x;
      o[7] = x + (y - x) * 0.25f;
      o[8] = (x < y ? 0.0f : 1.0f) + smoothstep_ref(-1.0f, 1.0f, x);
      o[9] = (x < 0 ? 0 : x > 1 ? 1 : x) + (y > 0 ? 1.0f : y < 0 ? -1.0f : 0.0f);
      o[10] = powf(fabsf(x) + 0.5f, 1.5f);
      o[11] = atan2f(x, y);
      float cx = x * 0.1f, cy = y * 0.1f;
      o[12] = asinf(cx < -1 ? -1 : cx > 1 ? 1 : cx) + acosf(cy < -1 ? -1 : cy > 1 ? 1 : cy);
      o[13] = sinhf(x * 0.5f);
      o[14] = h2f(f2h(h2f(f2h(x)) * 0.5f + h2f(f2h(y))));
      o[15] = exp2f(x) - logf(fabsf(y) + 1.0f);
      int32_t k = kk[i], m = kk[i ^ 1u];
      uint32_t uk = (uint32_t)k, um = (uint32_t)m, *u = iwant + i * 8;
      int64_t sum = (int64_t)k + m, dif = (int64_t)k - m;
      u[0] = (uint32_t)(int32_t)(sum > INT32_MAX ? INT32_MAX : sum < INT32_MIN ? INT32_MIN : sum);
      u[1] = (uint32_t)(int32_t)(dif > INT32_MAX ? INT32_MAX : dif < INT32_MIN ? INT32_MIN : dif);
      u[2] = (uint32_t)(dif < 0 ? -dif : dif);
      u[3] = (uk >> 3) & 0x1ffu;
      u[4] = (uint32_t)((int32_t)(uk << (32 - 12)) >> (32 - 7));
      uint32_t rev = 0;
      for (unsigned bit = 0; bit < 32; bit++)
         rev |= ((uk >> bit) & 1u) << (31 - bit);
      u[5] = rev;
      uint32_t uh = (uint32_t)(((uint64_t)uk + um) >> 1);
      int32_t ir = (int32_t)((sum + 1) >> 1);
      u[6] = uh ^ (uint32_t)ir;
      u[7] = ((uk & ~(0xfffu << 4)) | ((um & 0xfffu) << 4)) + ((uk & 0xffffu) * (um & 0xffffu) + 5u);
   }
   static const float abs_tol[16] = { 0, 0, 0, 0, 1e-4f, 0, 0, 1e-6f, 1e-6f, 0, 0, 2e-5f, 1e-4f, 2e-6f, 4e-3f, 1e-4f };
   static const float rel_tol[16] = { 0, 0, 0, 1e-6f, 0, 0, 0, 1e-6f, 1e-6f, 0, 1e-5f, 1e-6f, 0, 1e-4f, 0, 1e-5f };
   bool ok = check_f32(r, "out", S(r, 3), want, n * 16, 16, abs_tol, rel_tol) &&
             check_u32(r, "iout", S(r, 4), iwant, n * 8, 8);
   free(want);
   free(iwant);
   return ok;
}

/* --- mat_const ------------------------------------------------------------------------------------------------ */

struct params {   /* struct Params in mat_const.metal */
   float m[4][4];  /* column-major: m[column][row] */
   float scale[2];
   uint32_t count;
   int32_t offset;
};
_Static_assert(sizeof(struct params) == 80, "Params is 80 bytes in Metal");

static void
mat_const_setup(struct run *r)
{
   unsigned n = r->threads;
   struct params *p = slot(r, 0, sizeof *p);
   float *in = slot(r, 1, n * 16);
   slot(r, 2, n * 16);
   for (unsigned c = 0; c < 4; c++) {
      for (unsigned k = 0; k < 4; k++)
         p->m[c][k] = rndf(-2, 2);
   }
   p->scale[0] = 1.5f;
   p->scale[1] = -0.75f;
   p->count = 40;
   p->offset = -13;
   for (unsigned i = 0; i < n * 4; i++)
      in[i] = rndf(-10, 10);
}

static bool
mat_const_check(struct run *r)
{
   unsigned n = r->threads;
   const struct params *p = S(r, 0);
   const float *in = S(r, 1);
   float *want = calloc(n * 4, 4);
   for (unsigned i = 0; i < n; i++) {
      float v[4], rr[4];
      memcpy(v, in + i * 4, sizeof v);
      for (unsigned row = 0; row < 4; row++) {
         rr[row] = 0;
         for (unsigned c = 0; c < 4; c++)
            rr[row] += p->m[c][row] * v[c];
      }
      rr[0] *= p->scale[0];
      rr[1] *= p->scale[1];
      rr[3] += (float)((int32_t)p->count + p->offset);
      for (unsigned row = 0; row < 4; row++) {
         float s = 0;
         for (unsigned c = 0; c < 4; c++)
            s += p->m[row][c] * rr[c];
         want[i * 4 + row] = s * 0.0625f;
      }
   }
   static const float abs_tol[4] = { 1e-4f, 1e-4f, 1e-4f, 1e-4f }, rel_tol[4] = { 1e-5f, 1e-5f, 1e-5f, 1e-5f };
   bool ok = check_f32(r, "out", S(r, 2), want, n * 4, 4, abs_tol, rel_tol);
   free(want);
   return ok;
}

/* --- ctrl_flow ------------------------------------------------------------------------------------------------ */

static void
ctrl_flow_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, n * 3 * 4);
   for (unsigned i = 0; i < n; i++)
      in[i] = rnd();
   in[0] = 0;
   in[1] = 0xffffffffu;
}

static bool
ctrl_flow_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0);
   uint32_t *want = calloc(n * 3, 4);
   for (unsigned i = 0; i < n; i++) {
      uint32_t x = in[i], acc = 0;
      for (uint32_t a = 0; a < 8u; a++) {
         if (((x >> a) & 1u) == 0u)
            continue;
         for (uint32_t b = 0; b < 8u; b++) {
            if (b > a)
               break;
            acc += a * 8u + b;
            if (acc > 300u)
               break;
         }
      }
      uint32_t y = x, steps = 0;
      do {
         y = y * 1103515245u + 12345u;
         steps++;
      } while ((y >> 28) != 0u && steps < 40u);
      want[i * 3] = acc;
      want[i * 3 + 1] = steps;
      want[i * 3 + 2] = 99u;
      for (uint32_t k = 0; k < 32u; k++) {
         if ((((x * 2654435761u) >> k) & 1u) && k > (x & 15u)) {
            want[i * 3 + 2] = k;
            break;
         }
      }
   }
   bool ok = check_u32(r, "out", S(r, 1), want, n * 3, 3);
   free(want);
   return ok;
}

/* --- atomic_float --------------------------------------------------------------------------------------------- */

static void
atomic_float_setup(struct run *r)
{
   unsigned n = r->threads;
   uint32_t *in = slot(r, 0, n * 4);
   slot(r, 1, 16);
   slot(r, 2, 16);
   for (unsigned i = 0; i < n; i++)
      in[i] = rnd();
}

static bool
atomic_float_check(struct run *r)
{
   unsigned n = r->threads;
   const uint32_t *in = S(r, 0);
   float want[4] = { 0 };
   uint32_t cwant[4] = { 0 };
   for (unsigned i = 0; i < n; i++) {
      want[in[i] & 3u] += (float)(in[i] & 255u) * 0.25f;
      cwant[in[i] & 3u] += in[i] >> 20;
   }
   static const float zero[1] = { 0 };
   return check_f32(r, "sums", S(r, 1), want, 4, 1, zero, zero) && check_u32(r, "counts", S(r, 2), cwant, 4, 1);
}

/* --- grid3d --------------------------------------------------------------------------------------------------- */

static void
grid3d_setup(struct run *r)
{
   slot(r, 0, r->threads * 2 * 4);
}

static bool
grid3d_check(struct run *r)
{
   const unsigned W = 16, H = 8, D = 8;
   uint32_t *want = calloc(W * H * D * 2, 4);
   for (unsigned z = 0; z < D; z++) {
      for (unsigned y = 0; y < H; y++) {
         for (unsigned x = 0; x < W; x++) {
            uint32_t *o = want + ((z * H + y) * W + x) * 2;
            unsigned lx = x % 4, ly = y % 4, lz = z % 4;
            o[0] = x | (y << 8) | (z << 16) | ((lx + ly * 4 + lz * 16) << 24);
            o[1] = lx | (ly << 4) | (lz << 8) | ((x / 4) << 12) | ((y / 4) << 16) | ((z / 4) << 20);
         }
      }
   }
   bool ok = check_u32(r, "out", S(r, 0), want, W * H * D * 2, 2);
   free(want);
   return ok;
}

/* --- agg_ops -------------------------------------------------------------------------------------------------- */

static void
agg_ops_setup(struct run *r)
{
   unsigned n = r->threads;
   float *in = slot(r, 0, n * 16);
   slot(r, 1, n * 16);
   slot(r, 2, n * 8);
   for (unsigned i = 0; i < n * 4; i++)
      in[i] = rndf(-100, 100);
}

struct pair {
   float v[4];
   uint32_t tag;
   uint16_t small;
};

static struct pair
make_pair_ref(const float *in, uint32_t i, uint32_t mode)
{
   struct pair p;
   for (unsigned k = 0; k < 4; k++)
      p.v[k] = (mode & 1u) ? in[i * 4 + k] * 2.0f : in[i * 4 + 3 - k] + 1.0f;
   p.tag = (mode & 1u) ? i : ~i;
   p.small = (uint16_t)((mode & 1u) ? i * 3u : i + 7u);
   return p;
}

static bool
agg_ops_check(struct run *r)
{
   unsigned n = r->threads;
   const float *in = S(r, 0);
   float *want = calloc(n * 4, 4);
   uint32_t *twant = calloc(n * 2, 4);
   for (uint32_t i = 0; i < n; i++) {
      struct pair p = make_pair_ref(in, i, i), q = make_pair_ref(in, i ^ 1u, i >> 1);
      for (unsigned k = 0; k < 4; k++)
         want[i * 4 + k] = p.v[k] - q.v[(k + 1) & 3];
      twant[i * 2] = p.tag ^ q.small;
      twant[i * 2 + 1] = q.tag + p.small;
   }
   static const float zero[4] = { 0 };
   bool ok = check_f32(r, "out", S(r, 1), want, n * 4, 4, zero, zero) && check_u32(r, "tags", S(r, 2), twant, n * 2, 2);
   free(want);
   free(twant);
   return ok;
}

/* --- fc_kernel ------------------------------------------------------------------------------------------------ */

#define FC_F32(f) { (uint8_t)(f), (uint8_t)((f) >> 8), (uint8_t)((f) >> 16), (uint8_t)((f) >> 24) }
/* set A: every constant given; set B leaves out scale (read only under use_scale, which is false) and bias (it has a
 * default); set C leaves out scale while use_scale is true: the frontend must refuse it */
static const struct fcset fc_a = { 5, { { 0, 1, { 1 } }, { 1, 4, FC_F32(0x40200000u) /* 2.5 */ }, { 2, 4, { 1 } },
                                        { 3, 4, { 7 } }, { 4, 1, { 1 } } } };
static const struct fcset fc_b = { 3, { { 0, 1, { 0 } }, { 2, 4, { 2 } }, { 4, 1, { 0 } } } };
static const struct fcset fc_c = { 3, { { 0, 1, { 1 } }, { 2, 4, { 0 } }, { 4, 1, { 0 } } } };

static const uint8_t *
fc_get(const struct fcset *fc, uint32_t index)
{
   for (unsigned i = 0; fc && i < fc->n; i++) {
      if (fc->v[i].index == index)
         return fc->v[i].bytes;
   }
   return NULL;
}

static void
fc_kernel_setup(struct run *r)
{
   unsigned n = r->threads;
   float *in = slot(r, 0, n * 4);
   slot(r, 1, n * 4);
   uint32_t *extra = slot(r, 2, n * 4);
   slot(r, 3, n * 4);
   for (unsigned i = 0; i < n; i++) {
      in[i] = rndf(-8, 8);
      extra[i] = rnd() & 0xffffu;
   }
}

static bool
fc_kernel_check(struct run *r)
{
   const struct fcset *fc = r->t->fc;
   unsigned n = r->threads;
   const float *in = S(r, 0);
   const uint32_t *extra = S(r, 2);
   const uint8_t *p;
   bool use_scale = (p = fc_get(fc, 0)) && p[0], has_extra = (p = fc_get(fc, 4)) && p[0];
   float scale = 0;
   int32_t mode = 0;
   uint32_t bias = 1000;
   if ((p = fc_get(fc, 1)))
      memcpy(&scale, p, 4);
   if ((p = fc_get(fc, 2)))
      memcpy(&mode, p, 4);
   if ((p = fc_get(fc, 3)))
      memcpy(&bias, p, 4);
   float *want = calloc(n, 4);
   uint32_t *iwant = calloc(n, 4);
   for (unsigned i = 0; i < n; i++) {
      float x = in[i];
      if (use_scale)
         x *= scale;
      x = mode == 0 ? x + 1.0f : mode == 1 ? -x : x * x;
      want[i] = x;
      iwant[i] = i + bias + (has_extra ? extra[i] : 0);
   }
   static const float zero[1] = { 0 };
   bool ok = check_f32(r, "out", S(r, 1), want, n, 1, zero, zero) && check_u32(r, "iout", S(r, 3), iwant, n, 1);
   free(want);
   free(iwant);
   return ok;
}

static const struct test tests[] = {
   { "fmath", NULL, "fmath", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, fmath_setup, fmath_check },
   { "fmath_precise", NULL, "fmath_precise", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, fmath_setup, fmath_check },
   { "imath", NULL, "imath", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, imath_setup, imath_check },
   { "conv", NULL, "conv", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, conv_setup, conv_check },
   { "loop_phi", NULL, "loop_phi", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, loop_phi_setup, loop_phi_check },
   { "switch", NULL, "switch_case", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, switch_setup, switch_check },
   { "tg_reduce", NULL, "tg_reduce", { 256, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, tg_reduce_setup, tg_reduce_check },
   { "tg_arg", NULL, "tg_arg", { 128, 1, 1 }, { 32, 1, 1 }, { 128 * 4, 128 * 2 }, 0, NULL, tg_arg_setup, tg_arg_check },
   { "dev_atomics", NULL, "dev_atomics", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, dev_atomics_setup, dev_atomics_check },
   { "tg_atomics", NULL, "tg_atomics", { 256, 1, 1 }, { 32, 1, 1 }, { 0 }, 0, NULL, tg_atomics_setup, tg_atomics_check },
   { "const_table", NULL, "const_table", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, const_table_setup, const_table_check },
   { "struct_buf", NULL, "struct_buf", { 64, 1, 1 }, { 16, 1, 1 }, { 0 }, 0, NULL, struct_buf_setup, struct_buf_check },
   { "grid2d", NULL, "grid2d", { 16, 8, 1 }, { 4, 4, 1 }, { 0 }, 0, NULL, grid2d_setup, grid2d_check },
   { "grid2d_indirect", "grid2d", "grid2d", { 16, 8, 1 }, { 4, 4, 1 }, { 0 }, GRID_INDIRECT, NULL, grid2d_setup, grid2d_check },
   { "grid2d_base", "grid2d", "grid2d", { 16, 8, 1 }, { 4, 4, 1 }, { 0 }, GRID_BASE, NULL, grid2d_setup, grid2d_check },
   { "simd_builtins", NULL, "simd_builtins", { 128, 1, 1 }, { 8, 1, 1 }, { 0 }, 0, NULL, simd_setup, simd_check },
   { "narrow", NULL, "narrow", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, narrow_setup, narrow_check },
   { "vec_ops", NULL, "vec_ops", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, vec_ops_setup, vec_ops_check },
   { "tg_transpose", NULL, "tg_transpose", { 16, 16, 1 }, { 4, 4, 1 }, { 0 }, 0, NULL, tg_transpose_setup, tg_transpose_check },
   { "thread_array", NULL, "thread_array", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, thread_array_setup, thread_array_check },
   { "ptr_ops", NULL, "ptr_ops", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, ptr_ops_setup, ptr_ops_check },
   { "simd_funcs", NULL, "simd_funcs", { 128, 1, 1 }, { 32, 1, 1 }, { 0 }, 0, NULL, simd_funcs_setup, simd_funcs_check },
   { "math_lib", NULL, "math_lib", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, math_lib_setup, math_lib_check },
   { "mat_const", NULL, "mat_const", { 64, 1, 1 }, { 16, 1, 1 }, { 0 }, 0, NULL, mat_const_setup, mat_const_check },
   { "ctrl_flow", NULL, "ctrl_flow", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, ctrl_flow_setup, ctrl_flow_check },
   { "atomic_float", NULL, "atomic_float", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, atomic_float_setup, atomic_float_check },
   { "grid3d", NULL, "grid3d", { 4, 4, 4 }, { 4, 2, 2 }, { 0 }, 0, NULL, grid3d_setup, grid3d_check },
   { "agg_ops", NULL, "agg_ops", { 64, 1, 1 }, { 64, 1, 1 }, { 0 }, 0, NULL, agg_ops_setup, agg_ops_check },
   { "fc_refuse", NULL, "fc_refuse", { 64, 1, 1 }, { 1, 1, 1 }, { 0 }, MUST_REFUSE, NULL, NULL, NULL },
   { "fc_kernel_a", "fc_kernel", "fc_kernel", { 64, 1, 1 }, { 16, 1, 1 }, { 0 }, 0, &fc_a, fc_kernel_setup, fc_kernel_check },
   { "fc_kernel_b", "fc_kernel", "fc_kernel", { 64, 1, 1 }, { 16, 1, 1 }, { 0 }, 0, &fc_b, fc_kernel_setup, fc_kernel_check },
   { "fc_kernel_c", "fc_kernel", "fc_kernel", { 64, 1, 1 }, { 1, 1, 1 }, { 0 }, MUST_REFUSE, &fc_c, NULL, NULL },
   { "fc_kernel_none", "fc_kernel", "fc_kernel", { 64, 1, 1 }, { 1, 1, 1 }, { 0 }, MUST_REFUSE, NULL, NULL, NULL },
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
