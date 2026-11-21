/* zink_present_ahead_fixture.c: runs the "one frame ahead" half of zink/nvk_present.c, as it is, against a scripted
 * GPU. tools/test_zink_present_queue.py cuts that half out of the source into ahead_lifted.inc, builds this file with
 * zink/nvk_present_queue.c and runs it (also under ThreadSanitizer). Everything the half reaches outside itself is a
 * stand-in defined here: Zink's screen and resources, the Vulkan calls it makes, NVK's private exports, IOSurface.
 * What a stand-in cannot show is what the GPU does with the commands; that is the card's to show.
 *   argv[1]: the arm set to run (keep, nokeep, oldnvk, oldkernel, off, cfg, fork). The switches are read once per
 *   process, so each set is a run; the settings file they are read from is NVK_ZINK_CFG_PATH, given with -D.
 * -DPA_NO_FORK leaves the fork set out (a sanitizer's runtime does not follow a fork of a threaded process). */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#ifndef __APPLE__
static int issetugid(void) { return 0; }   /* nvk_zink_cfg.h asks; only Apple's and the BSDs' C libraries answer */
#endif
#include "nvk_present_queue.h"
#include "nvk_zink_cfg.h"

static int fails;
#define ARM(name, cond) do { const int ok_ = !!(cond); printf("%s  %s\n", ok_ ? "ok  " : "FAIL", name); fflush(stdout); if (!ok_) fails++; } while (0)
static void sleep_ms(int ms) { struct timespec t = { ms / 1000, (ms % 1000) * 1000000L }; nanosleep(&t, NULL); }

/* ---- IOSurface and CoreFoundation ---- */
typedef struct fake_surface { atomic_int refs; uint32_t id, planes, format, width, height, element; size_t row, alloc; } *IOSurfaceRef;
static IOSurfaceRef fake_retain(IOSurfaceRef s) { atomic_fetch_add(&s->refs, 1); return s; }
#define CFRetain(s) fake_retain((IOSurfaceRef)(s))
#define CFRelease(s) ((void)atomic_fetch_sub(&((IOSurfaceRef)(s))->refs, 1))
#define IOSurfaceGetID(s) ((s)->id)
#define IOSurfaceGetPlaneCount(s) ((size_t)(s)->planes)
#define IOSurfaceGetPixelFormat(s) ((s)->format)
#define IOSurfaceGetWidth(s) ((size_t)(s)->width)
#define IOSurfaceGetHeight(s) ((size_t)(s)->height)
#define IOSurfaceGetBytesPerElement(s) ((size_t)(s)->element)
#define IOSurfaceGetBytesPerRow(s) ((s)->row)
#define IOSurfaceGetAllocSize(s) ((s)->alloc)

/* ---- the Vulkan the half names ---- */
typedef int VkResult;
enum { VK_SUCCESS = 0, VK_TIMEOUT = 2, VK_ERROR_DEVICE_LOST = -4, VK_ERROR_INVALID_EXTERNAL_HANDLE = -1000072003 };
typedef struct VkDevice_T *VkDevice; typedef struct VkQueue_T *VkQueue; typedef struct VkInstance_T *VkInstance;
typedef struct VkBuffer_T *VkBuffer; typedef struct VkDeviceMemory_T *VkDeviceMemory; typedef struct VkCommandPool_T *VkCommandPool;
typedef struct VkCommandBuffer_T *VkCommandBuffer; typedef struct VkFence_T *VkFence; typedef struct VkImage_T *VkImage;
typedef uint64_t VkDeviceSize; typedef uint32_t VkFlags; typedef uint32_t VkBool32; typedef int VkImageLayout;
#define VK_NULL_HANDLE NULL
#define VK_TRUE 1u
enum { VK_IMAGE_LAYOUT_UNDEFINED = 0, VK_IMAGE_LAYOUT_GENERAL = 1, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL = 6 };
enum { VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_BUFFER_CREATE_INFO = 1, VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR,
       VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
       VK_STRUCTURE_TYPE_FENCE_CREATE_INFO, VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
       VK_STRUCTURE_TYPE_SUBMIT_INFO };
enum { VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT = 1, VK_BUFFER_USAGE_TRANSFER_SRC_BIT = 1, VK_BUFFER_USAGE_TRANSFER_DST_BIT = 2,
       VK_SHARING_MODE_EXCLUSIVE = 0, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT = 1, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT = 2,
       VK_MEMORY_PROPERTY_HOST_COHERENT_BIT = 4, VK_COMMAND_BUFFER_LEVEL_PRIMARY = 0, VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT = 1,
       VK_ACCESS_TRANSFER_READ_BIT = 0x800, VK_ACCESS_MEMORY_READ_BIT = 0x8000, VK_ACCESS_MEMORY_WRITE_BIT = 0x10000,
       VK_IMAGE_ASPECT_COLOR_BIT = 1, VK_PIPELINE_STAGE_TRANSFER_BIT = 0x1000, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT = 0x10000,
       VK_DRIVER_ID_MESA_NVK = 24 };
#define VK_QUEUE_FAMILY_IGNORED (~0u)
typedef struct { int sType; const void *pNext; VkFlags handleTypes; } VkExternalMemoryBufferCreateInfo;
typedef struct { int sType; const void *pNext; VkDeviceSize size; VkFlags usage; int sharingMode; } VkBufferCreateInfo;
typedef struct { VkDeviceSize size, alignment; uint32_t memoryTypeBits; } VkMemoryRequirements;
typedef struct { int sType; const void *pNext; VkFlags handleType; int fd; } VkImportMemoryFdInfoKHR;
typedef struct { int sType; const void *pNext; VkDeviceSize allocationSize; uint32_t memoryTypeIndex; } VkMemoryAllocateInfo;
typedef struct { int sType; const void *pNext; VkFlags flags; uint32_t queueFamilyIndex; } VkCommandPoolCreateInfo;
typedef struct { int sType; const void *pNext; VkCommandPool commandPool; int level; uint32_t commandBufferCount; } VkCommandBufferAllocateInfo;
typedef struct { int sType; const void *pNext; VkFlags flags; } VkFenceCreateInfo;
typedef struct { int sType; const void *pNext; VkFlags flags; const void *pInheritanceInfo; } VkCommandBufferBeginInfo;
typedef struct { VkFlags aspectMask; uint32_t baseMipLevel, levelCount, baseArrayLayer, layerCount; } VkImageSubresourceRange;
typedef struct { int sType; const void *pNext; VkFlags srcAccessMask, dstAccessMask; VkImageLayout oldLayout, newLayout;
                 uint32_t srcQueueFamilyIndex, dstQueueFamilyIndex; VkImage image; VkImageSubresourceRange subresourceRange; } VkImageMemoryBarrier;
typedef struct { VkFlags aspectMask; uint32_t mipLevel, baseArrayLayer, layerCount; } VkImageSubresourceLayers;
typedef struct { int32_t x, y, z; } VkOffset3D;
typedef struct { uint32_t width, height, depth; } VkExtent3D;
typedef struct { VkDeviceSize bufferOffset; uint32_t bufferRowLength, bufferImageHeight; VkImageSubresourceLayers imageSubresource;
                 VkOffset3D imageOffset; VkExtent3D imageExtent; } VkBufferImageCopy;
typedef struct { int sType; const void *pNext; uint32_t commandBufferCount; const VkCommandBuffer *pCommandBuffers; } VkSubmitInfo;
typedef struct { VkFlags propertyFlags; uint32_t heapIndex; } VkMemoryType;
typedef struct { uint32_t memoryTypeCount; VkMemoryType memoryTypes[32]; } VkPhysicalDeviceMemoryProperties;
typedef void (*PFN_vkVoidFunction)(void);
typedef PFN_vkVoidFunction (*PFN_vkGetInstanceProcAddr)(VkInstance, const char *);
typedef PFN_vkVoidFunction (*PFN_vkGetDeviceProcAddr)(VkDevice, const char *);

/* ---- Gallium and Zink ---- */
typedef pthread_mutex_t simple_mtx_t;
#define simple_mtx_lock(m) pthread_mutex_lock(m)
#define simple_mtx_unlock(m) pthread_mutex_unlock(m)
#define p_atomic_read(p) __atomic_load_n((p), __ATOMIC_SEQ_CST)
#define p_atomic_set(p, v) __atomic_store_n((p), (v), __ATOMIC_SEQ_CST)
enum { PIPE_FORMAT_B8G8R8A8_UNORM = 1, PIPE_FORMAT_R8G8B8A8_UNORM = 2 };
struct pipe_fence_handle { atomic_int refs; };
struct pipe_resource { atomic_int refs; uint32_t width0, height0; int format; };
struct pipe_screen {
   bool (*fence_finish)(struct pipe_screen *, void *, struct pipe_fence_handle *, uint64_t);
   void (*fence_reference)(struct pipe_screen *, struct pipe_fence_handle **, struct pipe_fence_handle *);
};
static void pipe_resource_reference(struct pipe_resource **to, struct pipe_resource *from)
{
   if (from) atomic_fetch_add(&from->refs, 1);
   if (*to) atomic_fetch_sub(&(*to)->refs, 1);
   *to = from;
}
struct zink_resource_object { VkImage image; bool is_buffer; };
struct zink_resource { struct pipe_resource base; VkImageLayout layout; struct zink_resource_object *obj; };
#define zink_resource(p) ((struct zink_resource *)(p))
struct zink_screen {
   struct pipe_screen base;
   VkDevice dev; VkQueue queue; VkInstance instance;
   simple_mtx_t *queue_lock;
   uint32_t gfx_queue;
   void *loader_lib;
   PFN_vkGetDeviceProcAddr vk_GetDeviceProcAddr;
   struct { struct { int driverID; } driver_props; struct { uint32_t vendorID; } props; bool have_KHR_external_memory_fd;
            VkPhysicalDeviceMemoryProperties mem_props; } info;
   struct {
      VkResult (*CreateBuffer)(VkDevice, const VkBufferCreateInfo *, const void *, VkBuffer *);
      void (*DestroyBuffer)(VkDevice, VkBuffer, const void *);
      void (*GetBufferMemoryRequirements)(VkDevice, VkBuffer, VkMemoryRequirements *);
      VkResult (*AllocateMemory)(VkDevice, const VkMemoryAllocateInfo *, const void *, VkDeviceMemory *);
      void (*FreeMemory)(VkDevice, VkDeviceMemory, const void *);
      VkResult (*BindBufferMemory)(VkDevice, VkBuffer, VkDeviceMemory, VkDeviceSize);
      VkResult (*CreateCommandPool)(VkDevice, const VkCommandPoolCreateInfo *, const void *, VkCommandPool *);
      void (*DestroyCommandPool)(VkDevice, VkCommandPool, const void *);
      VkResult (*ResetCommandPool)(VkDevice, VkCommandPool, VkFlags);
      VkResult (*AllocateCommandBuffers)(VkDevice, const VkCommandBufferAllocateInfo *, VkCommandBuffer *);
      VkResult (*CreateFence)(VkDevice, const VkFenceCreateInfo *, const void *, VkFence *);
      void (*DestroyFence)(VkDevice, VkFence, const void *);
      VkResult (*ResetFences)(VkDevice, uint32_t, const VkFence *);
      VkResult (*BeginCommandBuffer)(VkCommandBuffer, const VkCommandBufferBeginInfo *);
      VkResult (*EndCommandBuffer)(VkCommandBuffer);
      void (*CmdPipelineBarrier)(VkCommandBuffer, VkFlags, VkFlags, VkFlags, uint32_t, const void *, uint32_t, const void *, uint32_t,
                                 const VkImageMemoryBarrier *);
      void (*CmdCopyImageToBuffer)(VkCommandBuffer, VkImage, VkImageLayout, VkBuffer, uint32_t, const VkBufferImageCopy *);
      VkResult (*QueueSubmit)(VkQueue, uint32_t, const VkSubmitInfo *, VkFence);
      VkResult (*WaitForFences)(VkDevice, uint32_t, const VkFence *, VkBool32, uint64_t);
   } vk;
};
#define zink_screen(p) ((struct zink_screen *)(p))

/* ---- what the first half of nvk_present.c gives the second ---- */
struct surface_import { uint32_t version, surface, plane, pad; uint64_t bytes; };
typedef void (*stage_import_fn)(const struct surface_import *);
typedef uint32_t (*surface_dirty_fn)(VkDevice, uint32_t, uint32_t);
struct nvk_zink_present_event {
   void *get_device_proc_addr, *device, *queue;
   uint64_t buffer;
   uint32_t row, width, height, queue_family, host_coherent_types;
   uint32_t host_backing;
};
typedef int (*nvk_zink_present_observer)(const struct nvk_zink_present_event *);
typedef int (*nvk_zink_frame_done)(void *arg, int publish);
static _Atomic(nvk_zink_present_observer) present_observer;

/* ---- the script, and what was seen ---- */
enum { E_SUBMIT = 1, E_WAIT, E_OBSERVE, E_MARK, E_MARK_AT, E_TAG, E_DONE, E_OLD_PATH };
static struct {
   pthread_mutex_t lock;
   int events[4096], count;
   /* script */
   atomic_int gate_closed;         /* the fence does not signal while 1 */
   atomic_int hang;                /* the fence never signals: a wait with a time limit runs out */
   VkResult submit_result, alloc_result;
   uint32_t cookie_next;           /* what the kernel names the next block; 0 = it does not say */
   int no_keep_exports;            /* an NVK from before 3.0.7 */
   uint32_t mark_status;           /* what the next mark answers (then 0 again) */
   int dropped;                    /* the fence was signalled by a channel rebuild */
   int old_path_result;            /* what nvk_zink_copy_vram answers */
   int zink_fence_never;           /* Zink's own fence does not finish */
   int uncertain_at_mark;          /* another window's frame fails while this one's mark is with the kernel */
   /* seen */
   atomic_int buffers, memories, pools, fences, opened, imports, marks, marks_at, tags, old_path, staged, staged_cleared;
   atomic_int cookie_asked_in_window, cookie_asked_outside, in_window, fence_finish_calls, fence_finish_zero;
   uint32_t last_mark_cookie, last_mark_surface;
   uint32_t staged_surface, block_of[8];   /* the fake kernel: the block each surface (100..107) has, by its name */
   atomic_int marks_other_block;           /* marks that named a block the surface does not have */
   uint64_t last_wait_timeout;
   pthread_t observer_thread;
   int observer_saw_lock_held;
} G = { .lock = PTHREAD_MUTEX_INITIALIZER };
static void event(int e) { pthread_mutex_lock(&G.lock); if (G.count < 4096) G.events[G.count++] = e; pthread_mutex_unlock(&G.lock); }
static int events_are(const int *want, int n)
{
   pthread_mutex_lock(&G.lock);
   int same = G.count == n;
   for (int i = 0; same && i < n; i++) same = G.events[i] == want[i];
   G.count = 0;
   pthread_mutex_unlock(&G.lock);
   return same;
}
/* wait until n events were seen (the presentation thread got that far), at most 5 s */
static int wait_events(int n)
{
   for (int i = 0; i < 5000; i++) {
      pthread_mutex_lock(&G.lock);
      const int have = G.count;
      pthread_mutex_unlock(&G.lock);
      if (have >= n) return 1;
      sleep_ms(1);
   }
   return 0;
}
/* what a thread that comes for the queue finds, read under the queue's own lock: 1 taken (and left again), 0, -1 */
static int queue_claim_now(struct nvk_pq *q)
{
   void *frame; int result;
   const int claimed = nvk_pq_claim(q, 0, &frame, &result);
   if (claimed == NVK_PQ_TAKEN && !frame) nvk_pq_release(q);
   return claimed == NVK_PQ_TAKEN && frame ? 2 : claimed;
}
#define EVENTS(...) events_are((const int[]){__VA_ARGS__}, (int)(sizeof((const int[]){__VA_ARGS__}) / sizeof(int)))

static simple_mtx_t queue_lock = PTHREAD_MUTEX_INITIALIZER;
static VkResult f_CreateBuffer(VkDevice d, const VkBufferCreateInfo *i, const void *a, VkBuffer *out)
{ (void)d; (void)a; (void)i; atomic_fetch_add(&G.buffers, 1); *out = malloc(1); return VK_SUCCESS; }
static void f_DestroyBuffer(VkDevice d, VkBuffer b, const void *a) { (void)d; (void)a; atomic_fetch_sub(&G.buffers, 1); free(b); }
static void f_GetBufferMemoryRequirements(VkDevice d, VkBuffer b, VkMemoryRequirements *r)
{ (void)d; (void)b; r->size = 4096; r->alignment = 4096; r->memoryTypeBits = 1; }
static VkResult f_AllocateMemory(VkDevice d, const VkMemoryAllocateInfo *i, const void *a, VkDeviceMemory *out)
{
   (void)d; (void)a;
   if (G.alloc_result != VK_SUCCESS) return G.alloc_result;
   close(((const VkImportMemoryFdInfoKHR *)i->pNext)->fd);   /* a successful import takes the descriptor */
   atomic_fetch_add(&G.memories, 1); atomic_fetch_add(&G.imports, 1);
   atomic_store(&G.in_window, 1);
   *out = malloc(1);
   return VK_SUCCESS;
}
static void f_FreeMemory(VkDevice d, VkDeviceMemory m, const void *a) { (void)d; (void)a; atomic_fetch_sub(&G.memories, 1); free(m); }
static VkResult f_BindBufferMemory(VkDevice d, VkBuffer b, VkDeviceMemory m, VkDeviceSize o) { (void)d; (void)b; (void)m; (void)o; return VK_SUCCESS; }
static VkResult f_CreateCommandPool(VkDevice d, const VkCommandPoolCreateInfo *i, const void *a, VkCommandPool *out)
{ (void)d; (void)i; (void)a; atomic_fetch_add(&G.pools, 1); *out = malloc(1); return VK_SUCCESS; }
static void f_DestroyCommandPool(VkDevice d, VkCommandPool p, const void *a) { (void)d; (void)a; atomic_fetch_sub(&G.pools, 1); free(p); }
static VkResult f_ResetCommandPool(VkDevice d, VkCommandPool p, VkFlags f) { (void)d; (void)p; (void)f; return VK_SUCCESS; }
static VkResult f_AllocateCommandBuffers(VkDevice d, const VkCommandBufferAllocateInfo *i, VkCommandBuffer *out)
{ (void)d; (void)i; *out = (VkCommandBuffer)(uintptr_t)0x10; return VK_SUCCESS; }
/* A fence is unsignalled when made and after a reset, pending once submitted with, signalled when the "GPU" is done.
 * One that is submitted with again without a reset would still be signalled from the frame before: a wait for it
 * returns at once, and the frame would be marked and presented while its copy is still running. */
enum { FENCE_IDLE = 0, FENCE_PENDING, FENCE_SIGNALLED };
struct VkFence_T { atomic_int state; };
static VkResult f_CreateFence(VkDevice d, const VkFenceCreateInfo *i, const void *a, VkFence *out)
{ (void)d; (void)i; (void)a; atomic_fetch_add(&G.fences, 1); *out = calloc(1, sizeof(struct VkFence_T)); return VK_SUCCESS; }
static void f_DestroyFence(VkDevice d, VkFence f, const void *a) { (void)d; (void)a; atomic_fetch_sub(&G.fences, 1); free(f); }
static VkResult f_ResetFences(VkDevice d, uint32_t n, const VkFence *f)
{ (void)d; for (uint32_t i = 0; i < n; i++) atomic_store(&f[i]->state, FENCE_IDLE); return VK_SUCCESS; }
static VkResult f_BeginCommandBuffer(VkCommandBuffer c, const VkCommandBufferBeginInfo *i) { (void)c; (void)i; return VK_SUCCESS; }
static VkResult f_EndCommandBuffer(VkCommandBuffer c) { (void)c; return VK_SUCCESS; }
static void f_CmdPipelineBarrier(VkCommandBuffer c, VkFlags s, VkFlags d, VkFlags f, uint32_t a, const void *pa, uint32_t b, const void *pb,
                                 uint32_t n, const VkImageMemoryBarrier *i)
{ (void)c; (void)s; (void)d; (void)f; (void)a; (void)pa; (void)b; (void)pb; (void)n; (void)i; }
static void f_CmdCopyImageToBuffer(VkCommandBuffer c, VkImage i, VkImageLayout l, VkBuffer b, uint32_t n, const VkBufferImageCopy *r)
{ (void)c; (void)i; (void)l; (void)b; (void)n; (void)r; }
static VkResult f_QueueSubmit(VkQueue q, uint32_t n, const VkSubmitInfo *s, VkFence f)
{
   (void)q; (void)n; (void)s;
   /* the caller holds Zink's queue lock */
   if (pthread_mutex_trylock(&queue_lock) == 0) { pthread_mutex_unlock(&queue_lock); printf("FAIL  a submission without Zink's queue lock\n"); fails++; }
   if (G.submit_result != VK_SUCCESS) return G.submit_result;
   if (atomic_exchange(&f->state, FENCE_PENDING) != FENCE_IDLE) { printf("FAIL  a fence submitted with again without a reset\n"); fails++; }
   event(E_SUBMIT);
   return VK_SUCCESS;
}
static VkResult f_WaitForFences(VkDevice d, uint32_t n, const VkFence *f, VkBool32 all, uint64_t timeout)
{
   (void)d; (void)n; (void)all;
   G.last_wait_timeout = timeout;
   if (atomic_load(&f[0]->state) == FENCE_IDLE) { printf("FAIL  a wait for a fence nothing was submitted with\n"); fails++; }
   if (atomic_load(&G.hang)) { if (timeout) sleep_ms(30); return VK_TIMEOUT; }
   while (atomic_load(&G.gate_closed)) sleep_ms(1);
   atomic_store(&f[0]->state, FENCE_SIGNALLED);
   event(E_WAIT);
   return VK_SUCCESS;
}
static PFN_vkVoidFunction f_gdpa(VkDevice d, const char *name)
{ (void)d; return strcmp(name, "vkCmdCopyImageToBuffer") ? NULL : (PFN_vkVoidFunction)f_CmdCopyImageToBuffer; }
static PFN_vkVoidFunction f_gipa(VkInstance i, const char *name)
{ (void)i; return strcmp(name, "vkGetDeviceProcAddr") ? NULL : (PFN_vkVoidFunction)f_gdpa; }

/* NVK's private exports */
static void n_stage(const struct surface_import *in)
{
   if (in) { atomic_fetch_add(&G.staged, 1); G.staged_surface = in->surface; }
   else { atomic_fetch_add(&G.staged_cleared, 1); atomic_store(&G.in_window, 0); }
}
/* The grant names the block the surface has; the fake kernel remembers it, as the real one holds the cookie. */
static uint32_t n_cookie(void)
{
   atomic_fetch_add(atomic_load(&G.in_window) ? &G.cookie_asked_in_window : &G.cookie_asked_outside, 1);
   if (!G.cookie_next) return 0;
   return G.block_of[(G.staged_surface - 100) % 8] = G.cookie_next++;
}
static uint32_t take_mark_status(void) { const uint32_t s = G.mark_status; G.mark_status = 0; return s; }
static int uncertain;   /* the share group's flag (defined with the context below) */
static uint32_t n_dirty(VkDevice d, uint32_t surface, uint32_t plane)
{ (void)d; (void)plane; G.last_mark_surface = surface; G.last_mark_cookie = 0; atomic_fetch_add(&G.marks, 1); event(E_MARK); return take_mark_status(); }
static uint32_t n_dirty_at(VkDevice d, uint32_t surface, uint32_t plane, uint32_t cookie)
{
   (void)d; (void)plane;
   G.last_mark_surface = surface; G.last_mark_cookie = cookie; atomic_fetch_add(&G.marks_at, 1); event(E_MARK_AT);
   const uint32_t scripted = take_mark_status();
   if (G.uncertain_at_mark) p_atomic_set(&uncertain, 1);
   /* the kernel's rule: a mark that names another block than the surface has is refused */
   if (G.block_of[(surface - 100) % 8] != cookie) { atomic_fetch_add(&G.marks_other_block, 1); return 0x8000000Au; }
   /* a script that says "moved" means it: the surface has another block from now on, which nobody was told the name of */
   if (scripted == 0x8000000Au) G.block_of[(surface - 100) % 8] = 0;
   return scripted;
}
static int n_dropped(VkDevice d, VkFence f) { (void)d; (void)f; return G.dropped; }

/* the loader and dlfcn: one library, the scripted NVK */
static void *util_dl_get_proc_address(void *lib, const char *name) { (void)lib; (void)name; return (void *)1; }
static atomic_int dl_open;
static int fake_dladdr(const void *address, Dl_info *info) { (void)address; info->dli_fname = "libnvk_renderer.dylib"; return 1; }
static void *fake_dlopen(const char *path, int mode) { (void)path; (void)mode; atomic_fetch_add(&dl_open, 1); return (void *)&dl_open; }
static int fake_dlclose(void *handle) { (void)handle; atomic_fetch_sub(&dl_open, 1); return 0; }
static void *fake_dlsym(void *handle, const char *name)
{
   (void)handle;
   if (!strcmp(name, "vk_icdGetInstanceProcAddr")) return (void *)f_gipa;
   if (!strcmp(name, "nvmtl_nvk_stage_import")) return (void *)n_stage;
   if (!strcmp(name, "nvmtl_nvk_surface_dirty")) return (void *)n_dirty;
   if (!strcmp(name, "nvmtl_nvk_fence_dropped_v1")) return (void *)n_dropped;
   if (G.no_keep_exports) return NULL;
   if (!strcmp(name, "nvmtl_nvk_surface_dirty_at")) return (void *)n_dirty_at;
   if (!strcmp(name, "nvmtl_nvk_import_cookie")) return (void *)n_cookie;
   return NULL;
}
#define dladdr fake_dladdr
#define dlopen fake_dlopen
#define dlclose fake_dlclose
#define dlsym fake_dlsym
#ifndef RTLD_FIRST
#define RTLD_FIRST 0
#endif

static int publish_tag(IOSurfaceRef surface) { (void)surface; atomic_fetch_add(&G.tags, 1); event(E_TAG); return 1; }
/* 3.0.6's whole presentation, which the half falls back on */
static struct pipe_resource *old_path_image; static IOSurfaceRef old_path_surface;
static int nvk_zink_copy_vram(struct pipe_screen *s, struct pipe_resource *image, IOSurfaceRef surface)
{ (void)s; old_path_image = image; old_path_surface = surface; atomic_fetch_add(&G.old_path, 1); event(E_OLD_PATH); return G.old_path_result; }

static bool z_fence_finish(struct pipe_screen *s, void *ctx, struct pipe_fence_handle *f, uint64_t timeout)
{
   (void)s; (void)f;
   atomic_fetch_add(&G.fence_finish_calls, 1);
   if (!timeout && !ctx) atomic_fetch_add(&G.fence_finish_zero, 1);
   return !G.zink_fence_never;
}
static void z_fence_reference(struct pipe_screen *s, struct pipe_fence_handle **to, struct pipe_fence_handle *from)
{
   (void)s;
   if (from) atomic_fetch_add(&from->refs, 1);
   if (*to) atomic_fetch_sub(&(*to)->refs, 1);
   *to = from;
}

#include "ahead_lifted.inc"

/* ---- the context that presents ---- */
static struct zink_screen screen = {
   .base = { z_fence_finish, z_fence_reference },
   .dev = (VkDevice)(uintptr_t)0x100, .queue = (VkQueue)(uintptr_t)0x200, .instance = (VkInstance)(uintptr_t)0x300,
   .queue_lock = &queue_lock, .vk_GetDeviceProcAddr = f_gdpa,
   .info = { .driver_props = { VK_DRIVER_ID_MESA_NVK }, .props = { 0x10de }, .have_KHR_external_memory_fd = true,
             .mem_props = { 2, { { VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT, 0 }, { VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT, 1 } } } },
   .vk = { f_CreateBuffer, f_DestroyBuffer, f_GetBufferMemoryRequirements, f_AllocateMemory, f_FreeMemory, f_BindBufferMemory,
           f_CreateCommandPool, f_DestroyCommandPool, f_ResetCommandPool, f_AllocateCommandBuffers, f_CreateFence, f_DestroyFence,
           f_ResetFences, f_BeginCommandBuffer, f_EndCommandBuffer, f_CmdPipelineBarrier, f_CmdCopyImageToBuffer, f_QueueSubmit,
           f_WaitForFences },
};
static struct zink_resource_object image_object = { (VkImage)(uintptr_t)0x400, false };
static struct zink_resource image = { { 1, 64, 16, PIPE_FORMAT_B8G8R8A8_UNORM }, VK_IMAGE_LAYOUT_GENERAL, &image_object };
static struct fake_surface surfaces[6];
static struct nvk_pq queue;
static struct nvk_present_ahead *ahead;
static int uncertain;

struct drawable { atomic_int done, published; pthread_t on; };
static int frame_done(void *arg, int publish)
{
   struct drawable *d = arg;
   d->on = pthread_self();
   atomic_store(&d->published, publish);
   atomic_fetch_add(&d->done, 1);
   event(E_DONE);
   return publish;
}
static struct pipe_fence_handle zfences[4096]; static int zfence_next;
static struct drawable drawables[4096]; static int drawable_next;
/* one swap: enter, send. Returns what submit answered (or what begin answered when it was not 1), and the drawable */
static int swap(IOSurfaceRef surface, int synchronous, struct drawable **out)
{
   struct drawable *d = &drawables[drawable_next++];
   if (out) *out = d;
   const int began = nvk_present_ahead_begin(&queue, &ahead, &screen.base, &uncertain);
   if (began != 1) return began == 0 ? -100 : began;
   struct pipe_fence_handle *fence = &zfences[zfence_next++];
   atomic_store(&fence->refs, 1);
   return nvk_present_ahead_submit(&queue, ahead, &image.base, surface, fence, synchronous, frame_done, d);
}
static int drain(void) { return nvk_present_ahead_drain(&queue, &uncertain); }
static int surface_refs_idle(void) { for (int i = 0; i < 6; i++) if (atomic_load(&surfaces[i].refs) != 1) return 0; return 1; }
static int observed_answer;
static int observer(const struct nvk_zink_present_event *e)
{
   G.observer_thread = pthread_self();
   G.observer_saw_lock_held = pthread_mutex_trylock(&queue_lock) != 0;
   if (!G.observer_saw_lock_held) pthread_mutex_unlock(&queue_lock);
   event(E_OBSERVE);
   return e->row == 256 && e->width == 64 && e->height == 16 && e->buffer && !e->host_backing ? observed_answer : -1;
}

static void arms_keep(void)
{
   struct drawable *d;
   ARM("this process presents one frame ahead and keeps imports (the switches are on unset)", nvk_present_ahead_enabled() == 1);
   /* --- the first frame: the swap does not wait for the GPU --- */
   atomic_store(&G.gate_closed, 1);
   int r = swap(&surfaces[0], 0, &d);
   ARM("a swap hands its frame over and returns while the GPU is still copying", r == 2 && atomic_load(&d->done) == 0 && wait_events(1));
   ARM("the surface was imported once, with its block's name asked for right after the import and before the stage was cleared",
       atomic_load(&G.imports) == 1 && atomic_load(&G.cookie_asked_in_window) == 1 && atomic_load(&G.cookie_asked_outside) == 0 &&
       atomic_load(&G.staged) == 1 && atomic_load(&G.staged_cleared) == 1);
   sleep_ms(30);
   ARM("nothing is marked, tagged or presented before the copy's fence", atomic_load(&G.marks_at) + atomic_load(&G.marks) == 0 &&
       atomic_load(&G.tags) == 0 && atomic_load(&d->done) == 0);
   atomic_store(&G.gate_closed, 0);
   ARM("the next swap's wait finds the frame published", drain() == 1 && atomic_load(&d->done) == 1 && atomic_load(&d->published) == 1);
   ARM("in this order: submit, the fence, the mark for the block that was imported, the tag, the drawable",
       EVENTS(E_SUBMIT, E_WAIT, E_MARK_AT, E_TAG, E_DONE) && G.last_mark_cookie == 1000 && G.last_mark_surface == surfaces[0].id);
   ARM("the drawable was presented on the presentation thread, and the GPU wait had its 5 s limit",
       !pthread_equal(d->on, pthread_self()) && G.last_wait_timeout == 5000000000ull);
   ARM("Zink was told with its own fence, without waiting, and the fence was let go",
       atomic_load(&G.fence_finish_zero) == 1 && atomic_load(&zfences[0].refs) == 0);

   /* --- three drawables in rotation: three imports for sixty frames --- */
   int ok = 1;
   for (int i = 1; i <= 60 && ok; i++) ok = swap(&surfaces[i % 3], 0, &d) == 2;
   ok = ok && drain() == 1;
   ARM("sixty more frames over three surfaces: three imports in all, sixty-one marks, each for its own block",
       ok && atomic_load(&G.imports) == 3 && atomic_load(&G.marks_at) == 61 && atomic_load(&G.marks) == 0 &&
       atomic_load(&G.marks_other_block) == 0 && atomic_load(&G.old_path) == 0 &&
       atomic_load(&G.buffers) == 3 && atomic_load(&G.memories) == 3 && atomic_load(&G.tags) == 61);
   ARM("a swap waits for the frame before it: never two in flight (each frame's five events in a row)", ({
       int in_order = G.count == 300;
       for (int i = 0; in_order && i < 300; i += 5)
          in_order = G.events[i] == E_SUBMIT && G.events[i + 1] == E_WAIT && G.events[i + 2] == E_MARK_AT && G.events[i + 3] == E_TAG && G.events[i + 4] == E_DONE;
       G.count = 0; in_order; }));

   /* --- a fourth and a fifth surface: the one used longest ago makes room --- */
   const int imports0 = atomic_load(&G.imports);
   swap(&surfaces[3], 0, NULL); swap(&surfaces[4], 0, NULL); drain();
   ARM("a fourth surface takes the free place, a fifth takes the place of the one used longest ago: four kept at most",
       atomic_load(&G.imports) == imports0 + 2 && atomic_load(&G.buffers) == 4 && atomic_load(&G.memories) == 4);
   /* 59, 60 were surfaces 2, 0; surface 1 (frame 58) was the oldest and is gone, 2 and 0 are still kept */
   swap(&surfaces[2], 0, NULL); swap(&surfaces[0], 0, NULL); drain();
   const int kept_hits = atomic_load(&G.imports) == imports0 + 2;
   swap(&surfaces[1], 0, NULL); drain();
   ARM("the ones still kept are found again, the one that made room is imported anew", kept_hits && atomic_load(&G.imports) == imports0 + 3 &&
       atomic_load(&G.buffers) == 4);
   G.count = 0;

   /* --- an import nobody uses for 64 presents is let go --- */
   /* surface 1 was the last one presented into; the other kept ones are older */
   for (int i = 0; i < 63; i++) swap(&surfaces[0], 0, NULL);
   drain();
   const int at63 = atomic_load(&G.buffers);
   swap(&surfaces[0], 0, NULL); drain();
   ARM("an import is kept through 63 presents that did not use it and let go at the 64th", at63 == 2 && atomic_load(&G.buffers) == 1 &&
       atomic_load(&G.memories) == 1);
   G.count = 0;

   /* --- the surface has another block than the one kept: the frame is sent again, not lost --- */
   const int old0 = atomic_load(&G.old_path), imports1 = atomic_load(&G.imports);
   G.mark_status = 0x8000000Au; G.old_path_result = 1;
   atomic_store(&G.gate_closed, 1);
   r = swap(&surfaces[0], 0, &d);
   atomic_store(&G.gate_closed, 0);
   /* nobody comes back for this frame: an application that draws only when something changes may not swap again */
   const int went_out = wait_events(5);
   ARM("a mark refused for a kept block: the presentation thread sends the frame again the way 3.0.6 does, with the same image "
       "and surface, and presents it, without the application coming back",
       r == 2 && went_out && atomic_load(&G.old_path) == old0 + 1 && old_path_image == &image.base && old_path_surface == &surfaces[0] &&
       atomic_load(&d->done) == 1 && atomic_load(&d->published) == 1 && !pthread_equal(d->on, pthread_self()) &&
       EVENTS(E_SUBMIT, E_WAIT, E_MARK_AT, E_OLD_PATH, E_DONE));
   ARM("the next wait finds it published, and the import of the block that is gone was dropped",
       drain() == 1 && atomic_load(&G.buffers) == 0 && atomic_load(&G.memories) == 0);
   swap(&surfaces[0], 0, NULL); drain();
   ARM("and the surface is imported anew for the next frame", atomic_load(&G.imports) == imports1 + 1 && atomic_load(&G.buffers) == 1);
   G.count = 0;
   /* the old path's own refusal */
   G.mark_status = 0x8000000Au; G.old_path_result = 0;
   swap(&surfaces[0], 0, &d);
   ARM("a frame the old path cannot publish either is released unpresented, and the wait says so",
       drain() == 2 && atomic_load(&d->done) == 1 && atomic_load(&d->published) == 0);
   G.old_path_result = 1; G.count = 0;
   /* any refusal of a kept import's mark, not only "moved": the kernel could not say the surface has this block */
   swap(&surfaces[0], 0, NULL); drain();
   const int old1 = atomic_load(&G.old_path);
   G.mark_status = 0x80000004u;
   swap(&surfaces[0], 0, &d);
   ARM("a kept import's mark refused for any other reason is sent again the same way",
       drain() == 1 && atomic_load(&G.old_path) == old1 + 1 && atomic_load(&d->published) == 1 && atomic_load(&G.buffers) == 0);
   G.count = 0;

   /* --- the channel was rebuilt under the copy --- */
   G.dropped = 1;
   const int marks0 = atomic_load(&G.marks_at);
   swap(&surfaces[1], 0, &d);
   ARM("a fence signalled by a channel rebuild: no mark, no tag, the drawable released unpresented",
       drain() == 2 && atomic_load(&G.marks_at) == marks0 && atomic_load(&d->done) == 1 && atomic_load(&d->published) == 0 &&
       EVENTS(E_SUBMIT, E_WAIT, E_DONE));
   G.dropped = 0;

   /* --- a diagnostic observer, and a front-buffer flush, hear the frame's own result --- */
   atomic_store(&present_observer, observer);
   observed_answer = 1;
   r = swap(&surfaces[1], 0, &d);
   ARM("with an observer set the swap waits for its frame: the observer ran during it, on the presentation thread, under Zink's queue lock",
       r == 2 && atomic_load(&d->done) == 1 && atomic_load(&d->published) == 1 && G.observer_saw_lock_held &&
       !pthread_equal(G.observer_thread, pthread_self()) && EVENTS(E_SUBMIT, E_WAIT, E_OBSERVE, E_MARK_AT, E_TAG, E_DONE) && queue.state == NVK_PQ_FREE);
   observed_answer = 0;
   r = swap(&surfaces[1], 0, &d);
   ARM("an observer's safe failure fails that swap: nothing marked, the drawable released unpresented",
       r == 3 && atomic_load(&d->done) == 1 && atomic_load(&d->published) == 0 && EVENTS(E_SUBMIT, E_WAIT, E_OBSERVE, E_DONE));
   atomic_store(&present_observer, NULL);
   r = swap(&surfaces[1], 1, &d);
   ARM("a front-buffer flush waits for its frame too", r == 2 && atomic_load(&d->done) == 1 && queue.state == NVK_PQ_FREE);
   G.count = 0;

   /* --- what cannot be imported goes back to the caller, with nothing left behind --- */
   const int buffers0 = atomic_load(&G.buffers), opened0 = atomic_load(&dl_open);
   G.alloc_result = VK_ERROR_INVALID_EXTERNAL_HANDLE;
   r = swap(&surfaces[5], 0, &d);
   ARM("NVK refuses the import: the caller is told to finish the frame itself; nothing was submitted, kept or presented",
       r == -2 && queue.state == NVK_PQ_FREE && atomic_load(&d->done) == 0 && atomic_load(&G.buffers) == buffers0 && !uncertain &&
       atomic_load(&zfences[zfence_next - 1].refs) == 1 && atomic_load(&surfaces[5].refs) == 1 && G.count == 0);
   G.alloc_result = VK_SUCCESS;
   struct fake_surface planar = surfaces[5]; planar.planes = 2; atomic_store(&planar.refs, 1);
   ARM("a surface the old path would refuse is refused the same way", swap(&planar, 0, NULL) == -2 && queue.state == NVK_PQ_FREE);
   image.layout = VK_IMAGE_LAYOUT_UNDEFINED;
   ARM("so is an image Zink has not drawn into", swap(&surfaces[5], 0, NULL) == -2 && queue.state == NVK_PQ_FREE);
   image.layout = VK_IMAGE_LAYOUT_GENERAL;

   /* --- no drawable: the kept imports go --- */
   swap(&surfaces[2], 0, NULL);
   nvk_present_ahead_trim(&queue, &ahead, &uncertain);
   ARM("a context without a drawable lets every kept import go, after its last frame", atomic_load(&G.buffers) == 0 &&
       atomic_load(&G.memories) == 0 && queue.state == NVK_PQ_FREE && surface_refs_idle());
   G.count = 0;

   /* --- the block keeps moving: the context stops keeping --- */
   swap(&surfaces[1], 0, NULL); swap(&surfaces[2], 0, NULL); drain();
   const int kept_before = atomic_load(&G.buffers);
   int refused = 0;
   while (ahead->keep && refused < 16) { G.mark_status = 0x8000000Au; swap(&surfaces[0], 0, NULL); drain(); refused++; }
   ARM("after four marks refused for kept blocks in its life the context stops keeping, and lets the imports it still kept go at once",
       kept_before == 2 && refused >= 1 && ahead->moved == 4 && !ahead->keep && atomic_load(&G.buffers) == 0 &&
       atomic_load(&G.memories) == 0);
   const int imports2 = atomic_load(&G.imports), marks1 = atomic_load(&G.marks), named = atomic_load(&G.marks_at);
   swap(&surfaces[1], 0, NULL); swap(&surfaces[2], 0, NULL); swap(&surfaces[1], 0, NULL); drain();
   ARM("from then on every frame gets its own import and the plain mark, for the surfaces that had been kept too",
       atomic_load(&G.imports) == imports2 + 3 && atomic_load(&G.marks) == marks1 + 3 && atomic_load(&G.marks_at) == named &&
       atomic_load(&G.buffers) == 0);
   G.count = 0;

   /* --- teardown --- */
   atomic_store(&G.gate_closed, 1);
   swap(&surfaces[1], 0, &d);
   atomic_store(&G.gate_closed, 0);
   ARM("teardown waits for the frame in flight, then frees everything",
       nvk_present_ahead_destroy(&queue, &ahead, &uncertain) == 1 && ahead == NULL && atomic_load(&d->done) == 1 &&
       atomic_load(&G.buffers) == 0 && atomic_load(&G.memories) == 0 && atomic_load(&G.pools) == 0 && atomic_load(&G.fences) == 0 &&
       atomic_load(&dl_open) == opened0 - 1 && queue.state == NVK_PQ_FREE);
   ARM("every surface, the scratch image and every Zink fence is back to the references it had",
       surface_refs_idle() && atomic_load(&image.base.refs) == 1 && ({ int z = 1; for (int i = 0; i < zfence_next; i++) if (atomic_load(&zfences[i].refs) > 1) z = 0; z; }));
   ARM("teardown of a context that never presented is nothing", nvk_present_ahead_destroy(&queue, &ahead, &uncertain) == 1);

   /* --- uncertain: nothing is freed, nothing is presented, nothing more is sent --- */
   swap(&surfaces[0], 0, NULL); drain();
   const int b1 = atomic_load(&G.buffers), m1 = atomic_load(&G.memories), p1 = atomic_load(&G.pools), fn1 = atomic_load(&G.fences);
   atomic_store(&G.hang, 1);
   r = swap(&surfaces[0], 0, &d);
   ARM("a copy the GPU does not finish: the wait for it answers uncertain and the share group is quarantined",
       r == 2 && drain() == 0 && uncertain == 1 && atomic_load(&d->done) == 0 && queue_claim_now(&queue) == NVK_PQ_LOST);
   ARM("after that nothing is freed and no frame is taken: not by a swap, a trim or a teardown",
       swap(&surfaces[1], 0, NULL) == -1 && (nvk_present_ahead_trim(&queue, &ahead, &uncertain), 1) &&
       nvk_present_ahead_destroy(&queue, &ahead, &uncertain) == 0 && ahead != NULL && atomic_load(&G.buffers) == b1 &&
       atomic_load(&G.memories) == m1 && atomic_load(&G.pools) == p1 && atomic_load(&G.fences) == fn1 && atomic_load(&surfaces[0].refs) > 1);
}

/* A quarantined context keeps everything it had, for good: the test keeps it too, and goes on with a new one. */
static struct nvk_present_ahead *quarantined[8]; static int quarantined_count;
static void fresh_context(int uncertain_now)
{
   quarantined[quarantined_count++] = ahead;
   memset(&queue, 0, sizeof queue); ahead = NULL; uncertain = uncertain_now; G.count = 0;
}

/* more ways into quarantine, each on a context of its own */
static void arms_uncertain_more(void)
{
   struct drawable *d;
   /* the submission itself fails */
   fresh_context(0); atomic_store(&G.hang, 0);
   G.submit_result = VK_ERROR_DEVICE_LOST;
   int r = swap(&surfaces[0], 0, &d);
   ARM("a submission that fails is uncertain: no frame queued, no drawable presented, the import not freed",
       r == -1 && uncertain == 1 && atomic_load(&d->done) == 0 && queue.state == NVK_PQ_FREE && atomic_load(&surfaces[0].refs) > 1);
   G.submit_result = VK_SUCCESS;
   /* the old path's redo is uncertain */
   fresh_context(0);
   swap(&surfaces[1], 0, NULL); drain();
   const int b = atomic_load(&G.buffers);
   G.mark_status = 0x8000000Au; G.old_path_result = -1;
   swap(&surfaces[1], 0, &d);
   /* the flag is raised before the answer, so the wait can return first: the claim is what waits for the answer, and
    * the test may not take the queue away under the presentation thread before it */
   ARM("a frame sent again whose completion is uncertain: quarantined, the drawable neither presented nor released",
       drain() == 0 && queue_claim_now(&queue) == NVK_PQ_LOST && uncertain == 1 && atomic_load(&d->done) == 0 &&
       atomic_load(&G.buffers) == b - 1 && queue.state == NVK_PQ_UNCERTAIN);
   G.old_path_result = 1;
   /* the share group became uncertain while the frame's mark was with the kernel: the refused frame is not sent again */
   fresh_context(0);
   swap(&surfaces[1], 0, NULL); drain();
   const int b2 = atomic_load(&G.buffers), old2 = atomic_load(&G.old_path);
   G.mark_status = 0x8000000Au; G.uncertain_at_mark = 1;
   swap(&surfaces[1], 0, &d);
   ARM("a refused mark in a share group that is uncertain by then: nothing is sent again, nothing is freed, no drawable presented",
       queue_claim_now(&queue) == NVK_PQ_LOST && uncertain == 1 && atomic_load(&G.old_path) == old2 &&
       atomic_load(&G.buffers) == b2 && atomic_load(&d->done) == 0 && queue.state == NVK_PQ_UNCERTAIN);
   G.uncertain_at_mark = 0;
   /* Zink's own fence does not finish */
   fresh_context(0);
   G.zink_fence_never = 1;
   swap(&surfaces[2], 0, &d);
   ARM("Zink's own fence not finishing after the copy's did is uncertain too", drain() == 0 && uncertain == 1);
   G.zink_fence_never = 0;
   /* other windows behind a hung one are not each held for the full time */
   fresh_context(1);
   ARM("a quarantined share group takes no new frame", swap(&surfaces[3], 0, NULL) == -1);
}

static void arms_per_frame(const char *why)
{
   char name[256];
   struct drawable *d;
   int ok = 1;
   for (int i = 0; i < 30 && ok; i++) ok = swap(&surfaces[i % 3], 0, &d) == 2;
   ok = ok && drain() == 1;
   snprintf(name, sizeof name, "%s: every frame gets its own import, freed when the frame is done, and the plain mark", why);
   ARM(name, ok && atomic_load(&G.imports) == 30 && atomic_load(&G.marks) == 30 && atomic_load(&G.marks_at) == 0 &&
       atomic_load(&G.buffers) == 0 && atomic_load(&G.memories) == 0 && atomic_load(&G.tags) == 30 && surface_refs_idle());
   G.mark_status = 0x80000004u;
   swap(&surfaces[0], 0, &d);
   snprintf(name, sizeof name, "%s: a refused mark is a frame not published, as in 3.0.6, and is not sent again", why);
   ARM(name, drain() == 2 && atomic_load(&d->done) == 1 && atomic_load(&d->published) == 0 && atomic_load(&G.old_path) == 0);
   G.mark_status = 0x80000004u;
   snprintf(name, sizeof name, "%s: a front-buffer flush hears of it at once", why);
   ARM(name, swap(&surfaces[0], 1, &d) == 3 && atomic_load(&d->done) == 1 && atomic_load(&d->published) == 0);
   ARM("teardown frees everything", nvk_present_ahead_destroy(&queue, &ahead, &uncertain) == 1 && atomic_load(&G.pools) == 0 &&
       atomic_load(&G.fences) == 0 && atomic_load(&image.base.refs) == 1);
}

/* the reader of the two switches, as a function of the file's text */
static void arms_cfg(void)
{
   const char *n = "NVK_ZINK_PRESENT_AHEAD";
   ARM("a switch is on when nothing names it", nvk_zink_cfg_on_in("", n, 0) == 1 && nvk_zink_cfg_on_in("OTHER=0\n", n, 0) == 1);
   ARM("NAME=0 at the start of a line turns it off; NAME=1 and an empty value leave it on",
       nvk_zink_cfg_on_in("NVK_ZINK_PRESENT_AHEAD=0\n", n, 0) == 0 && nvk_zink_cfg_on_in("A=1\nNVK_ZINK_PRESENT_AHEAD=0", n, 0) == 0 &&
       nvk_zink_cfg_on_in("NVK_ZINK_PRESENT_AHEAD=1\n", n, 0) == 1 && nvk_zink_cfg_on_in("NVK_ZINK_PRESENT_AHEAD=\n", n, 0) == 1);
   ARM("the last such line wins", nvk_zink_cfg_on_in("NVK_ZINK_PRESENT_AHEAD=0\nNVK_ZINK_PRESENT_AHEAD=1\n", n, 0) == 1 &&
       nvk_zink_cfg_on_in("NVK_ZINK_PRESENT_AHEAD=1\nNVK_ZINK_PRESENT_AHEAD=0\n", n, 0) == 0);
   ARM("a line that only contains the name does not count: a comment, a longer name, a name inside another",
       nvk_zink_cfg_on_in("# NVK_ZINK_PRESENT_AHEAD=0\n", n, 0) == 1 && nvk_zink_cfg_on_in("NVK_ZINK_PRESENT_AHEAD_X=0\n", n, 0) == 1 &&
       nvk_zink_cfg_on_in("XNVK_ZINK_PRESENT_AHEAD=0\n", n, 0) == 1 && nvk_zink_cfg_on_in("X NVK_ZINK_PRESENT_AHEAD=0\n", n, 0) == 1 &&
       nvk_zink_cfg_on_in("#\tNVK_ZINK_PRESENT_AHEAD = 0\n", n, 0) == 1);
   ARM("blanks before the name, around the '=' and before the value are skipped: every spelling that turns a switch of the Metal plugin off does so here",
       nvk_zink_cfg_on_in(" NVK_ZINK_PRESENT_AHEAD=0\n", n, 0) == 0 && nvk_zink_cfg_on_in("A=1\n\tNVK_ZINK_PRESENT_AHEAD = 0\n", n, 0) == 0 &&
       nvk_zink_cfg_on_in("NVK_ZINK_PRESENT_AHEAD =\t0\r\n", n, 0) == 0 && nvk_zink_cfg_on_in("NVK_ZINK_PRESENT_AHEAD = 1\n", n, 0) == 1 &&
       nvk_zink_cfg_on_in("NVK_ZINK_PRESENT_AHEAD = \n", n, 0) == 1);
   ARM("one switch's line does not turn the other off", nvk_zink_cfg_on_in("NVK_ZINK_SURFACE_KEEP=0\n", n, 0) == 1 &&
       nvk_zink_cfg_on_in("NVK_ZINK_SURFACE_KEEP=0\n", "NVK_ZINK_SURFACE_KEEP", 0) == 0);
   setenv("NVK_ZINK_PRESENT_AHEAD", "0", 1);
   ARM("a variable of the environment is not read unless the process keeps its own variables",
       nvk_zink_cfg_on_in("", n, 0) == 1 && nvk_zink_cfg_on_in("", n, 1) == 0);
   setenv("NVK_ZINK_PRESENT_AHEAD", "1", 1);
   ARM("and where it does, the variable wins over the file", nvk_zink_cfg_on_in("NVK_ZINK_PRESENT_AHEAD=0\n", n, 1) == 1);
   unsetenv("NVK_ZINK_PRESENT_AHEAD");
   ARM("without the variable the file decides there too", nvk_zink_cfg_on_in("NVK_ZINK_PRESENT_AHEAD=0\n", n, 1) == 0);
}

#ifndef PA_NO_FORK
/* The child of a fork has the context and no presentation thread: the thread that swaps does the waiting itself. */
static void arms_fork(void)
{
   struct drawable *d;
   int ok = swap(&surfaces[0], 0, &d) == 2 && drain() == 1;
   ARM("before the fork: a frame through the presentation thread", ok && !pthread_equal(d->on, pthread_self()));
   fflush(stdout);
   const pid_t child = fork();
   if (child == 0) {
      fails = 0;
      const int r = swap(&surfaces[1], 0, &d);
      ARM("in the child the swap itself waits, marks, tags and presents, and says the frame is out",
          r == 2 && atomic_load(&d->done) == 1 && atomic_load(&d->published) == 1 && pthread_equal(d->on, pthread_self()) &&
          queue.state == NVK_PQ_FREE);
      G.dropped = 1;
      ARM("and a frame that was not published is answered as one", swap(&surfaces[1], 0, &d) == 3 && atomic_load(&d->done) == 1 &&
          atomic_load(&d->published) == 0);
      G.dropped = 0;
      G.mark_status = 0x8000000Au;
      ARM("a kept block that moved is sent again there too", swap(&surfaces[1], 0, &d) == 2 && atomic_load(&G.old_path) == 1 &&
          atomic_load(&d->published) == 1);
      ARM("teardown in the child frees what the child holds", nvk_present_ahead_destroy(&queue, &ahead, &uncertain) == 1);
      fflush(stdout);
      _exit(fails ? 1 : 0);
   }
   int status = -1;
   ARM("the child's arms passed", child > 0 && waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
   ARM("the parent goes on presenting through its thread", swap(&surfaces[2], 0, &d) == 2 && drain() == 1 && !pthread_equal(d->on, pthread_self()));
   ARM("teardown frees everything", nvk_present_ahead_destroy(&queue, &ahead, &uncertain) == 1 && atomic_load(&G.pools) == 0);
}
#endif

int main(int argc, char **argv)
{
   const char *set = argc > 1 ? argv[1] : "keep";
   for (int i = 0; i < 6; i++)
      surfaces[i] = (struct fake_surface){ 1, 100 + i, 1, 0x42475241, 64, 16, 4, 256, 4096 };
   G.cookie_next = 1000;
   G.old_path_result = 1;
   if (!strcmp(set, "keep")) { arms_keep(); arms_uncertain_more(); }
   else if (!strcmp(set, "nokeep")) {
      ARM("NVK_ZINK_SURFACE_KEEP=0 in the settings file leaves presentation one frame ahead", nvk_present_ahead_enabled() == 1);
      arms_per_frame("NVK_ZINK_SURFACE_KEEP=0");
   } else if (!strcmp(set, "oldnvk")) {
      G.no_keep_exports = 1;
      arms_per_frame("an NVK without the two new exports");
   } else if (!strcmp(set, "oldkernel")) {
      G.cookie_next = 0;
      arms_per_frame("a kernel that does not name the block");
   } else if (!strcmp(set, "off")) {
      ARM("NVK_ZINK_PRESENT_AHEAD=0 in the settings file: the frontend is told to present as in 3.0.6", nvk_present_ahead_enabled() == 0);
   } else if (!strcmp(set, "cfg")) {
      arms_cfg();
#ifndef PA_NO_FORK
   } else if (!strcmp(set, "fork")) {
      arms_fork();
#endif
   } else { printf("unknown arm set %s\n", set); return 2; }
   printf("%s\n", fails ? "FAILED" : "all arms passed");
   return fails ? 1 : 0;
}
