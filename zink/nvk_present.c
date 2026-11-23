/* SPDX-License-Identifier: MIT */
#include "nvk_present.h"
#include "nvk_present_queue.h"
#include "nvk_zink.h"
#include "nvk_zink_cfg.h"
#include "zink/zink_screen.h"
#include "zink/zink_resource.h"
#include "util/u_inlines.h"
#include "util/u_dl.h"
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <stdatomic.h>

/* The version-3 IOSurface import and dirty/tag publication are the same
 * protocol used by nvk/native-macos/bridge.mm. Never import another device's
 * VkImage: the Gallium scratch image belongs to this Zink screen. */
struct surface_import { uint32_t version, surface, plane, pad; uint64_t bytes; };
_Static_assert(sizeof(struct surface_import) == 24, "NVK import ABI");
typedef void (*stage_import_fn)(const struct surface_import *);
typedef uint32_t (*surface_dirty_fn)(VkDevice, uint32_t, uint32_t);

struct copy_frame {
   struct copy_frame *next;
   void *renderer;
   IOSurfaceRef surface;
   struct pipe_resource *image;
   VkBuffer buffer;
   VkDeviceMemory memory;
   VkCommandPool pool;
   VkFence fence;
   bool host_locked;
};
static pthread_mutex_t quarantine_lock = PTHREAD_MUTEX_INITIALIZER;
static struct copy_frame *quarantine;
static _Atomic(nvk_zink_present_observer) present_observer;

__attribute__((visibility("default"))) void
nvk_zink_set_present_observer(nvk_zink_present_observer observer)
{
   atomic_store(&present_observer, observer);
}

static void release_frame(struct zink_screen *screen, struct copy_frame *frame)
{
   if (frame->pool) screen->vk.DestroyCommandPool(screen->dev, frame->pool, NULL);
   if (frame->fence) screen->vk.DestroyFence(screen->dev, frame->fence, NULL);
   if (frame->buffer) screen->vk.DestroyBuffer(screen->dev, frame->buffer, NULL);
   if (frame->memory) screen->vk.FreeMemory(screen->dev, frame->memory, NULL);
   if (frame->host_locked) IOSurfaceUnlock(frame->surface, 0, NULL);
   pipe_resource_reference(&frame->image, NULL);
   if (frame->surface) CFRelease(frame->surface);
   if (frame->renderer) dlclose(frame->renderer);
   free(frame);
}

static int publish_tag(IOSurfaceRef surface)
{
   struct { uint32_t magic, seed, generation; } tag = {0x564d5634u, IOSurfaceGetSeed(surface), 0};
   CFTypeRef old = IOSurfaceCopyValue(surface, CFSTR("NVMTLVramAt:p0"));
   if (old && CFGetTypeID(old) == CFDataGetTypeID() && CFDataGetLength(old) == sizeof(tag)) {
      uint32_t words[3]; memcpy(words, CFDataGetBytePtr(old), sizeof(words));
      if (words[0] == tag.magic) tag.generation = words[2] + 1;
   }
   if (old) CFRelease(old);
   CFDataRef value = CFDataCreate(NULL, (const UInt8 *)&tag, sizeof(tag));
   if (!value) return 0;
   IOSurfaceSetValue(surface, CFSTR("NVMTLVramAt:p0"), value);
   CFRelease(value);
   return 1;
}

int nvk_zink_copy_vram(struct pipe_screen *pscreen, struct pipe_resource *image,
                       IOSurfaceRef surface)
{
   struct zink_screen *screen = zink_screen(pscreen);
   if (!image || !surface || screen->info.driver_props.driverID != VK_DRIVER_ID_MESA_NVK ||
       screen->info.props.vendorID != 0x10de || !screen->info.have_KHR_external_memory_fd ||
       IOSurfaceGetPlaneCount(surface) > 1 || IOSurfaceGetPixelFormat(surface) != 0x42475241 ||
       IOSurfaceGetWidth(surface) != image->width0 || IOSurfaceGetHeight(surface) != image->height0 ||
       IOSurfaceGetBytesPerElement(surface) != 4 || image->format != PIPE_FORMAT_B8G8R8A8_UNORM)
      return 0;
   size_t row = IOSurfaceGetBytesPerRow(surface), size = IOSurfaceGetAllocSize(surface);
   if (!row || row % 4 || row < (uint64_t)image->width0 * 4 || row / 4 > UINT32_MAX ||
       image->height0 > SIZE_MAX / row || row * image->height0 > size)
      return 0;

   /* Resolve private hooks only from the ICD implementing this exact device's
    * command entrypoint. A loader/layer wrapper or unrelated NVK image is
    * rejected rather than passing foreign handles to a private ABI. */
   if (!util_dl_get_proc_address(screen->loader_lib, "nvkbridgeSetPresentObserver")) return 0;
   PFN_vkVoidFunction copy = screen->vk_GetDeviceProcAddr(screen->dev, "vkCmdCopyImageToBuffer");
   Dl_info origin = {0};
   if (!copy || !dladdr((void *)copy, &origin) || !origin.dli_fname) return 0;
   struct copy_frame *frame = calloc(1, sizeof(*frame));
   if (!frame) return 0;
   frame->renderer = dlopen(origin.dli_fname, RTLD_NOW | RTLD_LOCAL | RTLD_FIRST | RTLD_NOLOAD);
   if (!frame->renderer) { free(frame); return 0; }
   PFN_vkGetInstanceProcAddr gipa = (void *)dlsym(frame->renderer, "vk_icdGetInstanceProcAddr");
   PFN_vkGetDeviceProcAddr gdpa = gipa ? (void *)gipa(screen->instance, "vkGetDeviceProcAddr") : NULL;
   stage_import_fn stage = (void *)dlsym(frame->renderer, "nvmtl_nvk_stage_import");
   surface_dirty_fn dirty = (void *)dlsym(frame->renderer, "nvmtl_nvk_surface_dirty");
   if (!gdpa || gdpa(screen->dev, "vkCmdCopyImageToBuffer") != copy || !stage || !dirty)
      goto failed;
   frame->surface = (IOSurfaceRef)CFRetain(surface);
   pipe_resource_reference(&frame->image, image);
   VkExternalMemoryBufferCreateInfo external = { .sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_BUFFER_CREATE_INFO,
      .handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT };
   VkBufferCreateInfo buffer = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .pNext = &external,
      .size = row * image->height0, .usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
      .sharingMode = VK_SHARING_MODE_EXCLUSIVE };
   if (screen->vk.CreateBuffer(screen->dev, &buffer, NULL, &frame->buffer) != VK_SUCCESS) goto failed;
   VkMemoryRequirements requirements;
   screen->vk.GetBufferMemoryRequirements(screen->dev, frame->buffer, &requirements);
   if (requirements.size > size) goto failed;
   uint32_t type;
   for (type = 0; type < screen->info.mem_props.memoryTypeCount; ++type)
      if ((requirements.memoryTypeBits & (1u << type)) &&
          (screen->info.mem_props.memoryTypes[type].propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)) break;
   if (type == screen->info.mem_props.memoryTypeCount) goto failed;
   int fd = open("/dev/null", O_RDONLY | O_CLOEXEC);
   if (fd < 0) goto failed;
   VkImportMemoryFdInfoKHR imported = { .sType = VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR,
      .handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT, .fd = fd };
   VkMemoryAllocateInfo allocation = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
      .pNext = &imported, .allocationSize = requirements.size, .memoryTypeIndex = type };
   const struct surface_import record = {3, IOSurfaceGetID(surface), 0, 0, requirements.size};
   stage(&record);
   VkResult result = screen->vk.AllocateMemory(screen->dev, &allocation, NULL, &frame->memory);
   stage(NULL); /* Clear thread-local staged imports on every allocation result. */
   if (result != VK_SUCCESS) {
      close(fd);
      /* A small BAR1 aperture can be full while ordinary GPU memory remains
       * available. Copy directly into the IOSurface's imported system pages
       * in that case. No CPU reads or copies of game pixels are involved.
       * IOSurface locking/seed publication replaces the family-VRAM tag. */
      if (result != VK_ERROR_INVALID_EXTERNAL_HANDLE || !screen->info.have_EXT_external_memory_host)
         goto failed;
      size_t align = screen->info.ext_host_mem_props.minImportedHostPointerAlignment;
      if (!align || (align & (align - 1)) || requirements.size > SIZE_MAX - (align - 1)) goto failed;
      if (IOSurfaceLock(surface, 0, NULL) != kIOReturnSuccess) goto failed;
      frame->host_locked = true;
      void *base = IOSurfaceGetBaseAddress(surface);
      if (!base || (uintptr_t)base % align) goto failed;
      screen->vk.DestroyBuffer(screen->dev, frame->buffer, NULL); frame->buffer = VK_NULL_HANDLE;
      external.handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;
      if (screen->vk.CreateBuffer(screen->dev, &buffer, NULL, &frame->buffer) != VK_SUCCESS) goto failed;
      screen->vk.GetBufferMemoryRequirements(screen->dev, frame->buffer, &requirements);
      if (requirements.size > SIZE_MAX - (align - 1)) goto failed;
      size_t bytes = (requirements.size + align - 1) & ~(align - 1);
      if (bytes > size) goto failed;
      VkMemoryHostPointerPropertiesEXT properties = { .sType = VK_STRUCTURE_TYPE_MEMORY_HOST_POINTER_PROPERTIES_EXT };
      if (screen->vk.GetMemoryHostPointerPropertiesEXT(screen->dev, external.handleTypes, base, &properties) != VK_SUCCESS)
         goto failed;
      for (type = 0; type < screen->info.mem_props.memoryTypeCount; ++type)
         if ((requirements.memoryTypeBits & properties.memoryTypeBits & (1u << type)) &&
             (screen->info.mem_props.memoryTypes[type].propertyFlags &
              (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) ==
             (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) break;
      if (type == screen->info.mem_props.memoryTypeCount) goto failed;
      VkImportMemoryHostPointerInfoEXT host = { .sType = VK_STRUCTURE_TYPE_IMPORT_MEMORY_HOST_POINTER_INFO_EXT,
         .handleType = external.handleTypes, .pHostPointer = base };
      allocation.pNext = &host; allocation.allocationSize = bytes; allocation.memoryTypeIndex = type;
      if (screen->vk.AllocateMemory(screen->dev, &allocation, NULL, &frame->memory) != VK_SUCCESS) goto failed;
   }
   if (screen->vk.BindBufferMemory(screen->dev, frame->buffer, frame->memory, 0) != VK_SUCCESS) goto failed;
   VkCommandPoolCreateInfo pool = { .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
      .queueFamilyIndex = screen->gfx_queue };
   if (screen->vk.CreateCommandPool(screen->dev, &pool, NULL, &frame->pool) != VK_SUCCESS) goto failed;
   VkCommandBuffer command;
   VkCommandBufferAllocateInfo cb = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
      .commandPool = frame->pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1 };
   if (screen->vk.AllocateCommandBuffers(screen->dev, &cb, &command) != VK_SUCCESS) goto failed;
   VkCommandBufferBeginInfo begin = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
      .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT };
   if (screen->vk.BeginCommandBuffer(command, &begin) != VK_SUCCESS) goto failed;
   struct zink_resource *resource = zink_resource(image);
   const VkImageLayout prior = resource->layout;
   if (prior == VK_IMAGE_LAYOUT_UNDEFINED || !resource->obj || resource->obj->is_buffer) goto failed;
   VkImageMemoryBarrier barrier = { .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
      .srcAccessMask = VK_ACCESS_MEMORY_WRITE_BIT, .dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT,
      .oldLayout = prior, .newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
      .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
      .image = resource->obj->image, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1} };
   screen->vk.CmdPipelineBarrier(command, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                                 0, 0, NULL, 0, NULL, 1, &barrier);
   VkBufferImageCopy region = { .bufferRowLength = row / 4, .bufferImageHeight = image->height0,
      .imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
      .imageExtent = {image->width0, image->height0, 1} };
   screen->vk.CmdCopyImageToBuffer(command, resource->obj->image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                                  frame->buffer, 1, &region);
   if (frame->host_locked) {
      VkMemoryBarrier host = { .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
         .srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT, .dstAccessMask = VK_ACCESS_HOST_READ_BIT };
      screen->vk.CmdPipelineBarrier(command, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT,
                                    0, 1, &host, 0, NULL, 0, NULL);
   }
   /* Restore the tracked layout. The application never observes our scratch
    * image, and subsequent Gallium operations retain their normal state. */
   barrier.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
   barrier.dstAccessMask = VK_ACCESS_MEMORY_READ_BIT | VK_ACCESS_MEMORY_WRITE_BIT;
   barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL; barrier.newLayout = prior;
   screen->vk.CmdPipelineBarrier(command, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
                                 0, 0, NULL, 0, NULL, 1, &barrier);
   if (screen->vk.EndCommandBuffer(command) != VK_SUCCESS) goto failed;
   VkFenceCreateInfo fence = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
   if (screen->vk.CreateFence(screen->dev, &fence, NULL, &frame->fence) != VK_SUCCESS) goto failed;
   VkSubmitInfo submit = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
      .commandBufferCount = 1, .pCommandBuffers = &command };
   /* Respect Zink's own queue lock, including other shared GL contexts and its
    * submission worker. The caller has already completed the scratch blit. */
   simple_mtx_lock(screen->queue_lock);
   result = screen->vk.QueueSubmit(screen->queue, 1, &submit, frame->fence);
   simple_mtx_unlock(screen->queue_lock);
   if (result == VK_SUCCESS)
      result = screen->vk.WaitForFences(screen->dev, 1, &frame->fence, VK_TRUE, 5000000000ull);
   nvk_zink_present_observer observer = atomic_load(&present_observer);
   int observed = 1;
   if (result == VK_SUCCESS && observer) {
      uint32_t host_types = 0;
      for (uint32_t i = 0; i < screen->info.mem_props.memoryTypeCount; ++i)
         if ((screen->info.mem_props.memoryTypes[i].propertyFlags &
              (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) ==
             (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) host_types |= 1u << i;
      const struct nvk_zink_present_event event = {
         (void *)screen->vk_GetDeviceProcAddr, (void *)screen->dev, (void *)screen->queue,
         (uint64_t)frame->buffer, (uint32_t)row, image->width0, image->height0, screen->gfx_queue, host_types,
         frame->host_locked};
      simple_mtx_lock(screen->queue_lock);
      observed = observer(&event);
      simple_mtx_unlock(screen->queue_lock);
   }
   if (result != VK_SUCCESS || observed < 0) {
      pthread_mutex_lock(&quarantine_lock);
      frame->next = quarantine; quarantine = frame;
      pthread_mutex_unlock(&quarantine_lock);
      return -1; /* No destruction or drawable publication after uncertain work. */
   }
   int published;
   if (frame->host_locked) {
      published = IOSurfaceUnlock(surface, 0, NULL) == kIOReturnSuccess;
      frame->host_locked = false;
      published = observed && published;
   } else {
      published = observed && dirty(screen->dev, IOSurfaceGetID(surface), 0) == 0 && publish_tag(surface);
   }
   release_frame(screen, frame);
   return published;
failed:
   release_frame(screen, frame);
   return 0;
}

/* ---- One frame ahead (3.0.7) ----------------------------------------------------------------------------------
 *
 * nvk_zink_copy_vram above is the whole of 3.0.6's presentation and is not changed: it imports the surface, copies,
 * waits for the GPU and publishes, all on the thread that swapped. A game that is waiting for the GPU there is not
 * preparing its next frame, and it imported the same three surfaces again for every frame it showed.
 *
 * What follows sends the same copy to the same queue and lets the presentation thread (nvk_present_queue.h) do the
 * waiting and the publishing, in the same order as above: the copy's fence first, then the mark, then the tag, then
 * the drawable. The thread that swapped comes back here before its next frame and waits for that to have happened,
 * so a context never has more than one frame on its way. An import is kept across frames when the kernel names the
 * block it shared (the cookie) and takes a mark for that block only; it is then looked up by its surface. When the
 * kernel refuses the mark of a kept import, the presentation thread sends the frame again through nvk_zink_copy_vram
 * before it answers: whether a frame reaches the screen never depends on the application swapping again.
 *
 * Each half has its own way back, read once per process: NVK_ZINK_PRESENT_AHEAD=0 presents through
 * nvk_zink_copy_vram, NVK_ZINK_SURFACE_KEEP=0 imports for every frame. */
typedef uint32_t (*surface_dirty_at_fn)(VkDevice, uint32_t, uint32_t, uint32_t);
typedef uint32_t (*import_cookie_fn)(void);
typedef int (*fence_dropped_fn)(VkDevice, VkFence);

#define AHEAD_KEPT 4          /* a CAMetalLayer rotates three drawables */
#define AHEAD_AGE 64          /* presents after which a kept import nobody used is let go */
#define AHEAD_MOVED_LIMIT 4   /* marks refused for a kept import after which the context stops keeping them */
/* What the presentation thread answers (NVK_PQ_WORK_UNCERTAIN apart). */
enum { AHEAD_NOT_PUBLISHED = 0, AHEAD_PUBLISHED = 1 };

struct ahead_import {
   IOSurfaceRef surface;   /* retained */
   VkBuffer buffer;
   VkDeviceMemory memory;
   uint32_t cookie;        /* the kernel's name for the block; 0 in an import made for one frame */
   uint64_t used;          /* the count of presents when it was last copied into */
};

/* One per context that presents this way. Everything in it is read and written by the thread inside the context's
 * queue; while a frame is queued, by the presentation thread and by nobody else (it may free a kept import then). */
struct nvk_present_ahead {
   struct zink_screen *screen;
   int *uncertain;         /* the share group's flag */
   void *renderer;
   stage_import_fn stage;
   surface_dirty_fn dirty;
   surface_dirty_at_fn dirty_at;
   import_cookie_fn cookie;
   fence_dropped_fn dropped;
   VkCommandPool pool;
   VkCommandBuffer command;
   VkFence fence;
   struct ahead_import kept[AHEAD_KEPT];
   uint64_t presents;
   unsigned moved;
   bool keep;
   /* the frame in flight */
   struct ahead_import own, *import;   /* its import: one of kept[], or own */
   IOSurfaceRef surface;               /* retained */
   struct pipe_resource *image;
   struct pipe_fence_handle *zink_fence;
   pthread_mutex_t *image_lock;        /* NULL, or what guards the image against the thread that draws (3.0.11) */
   nvk_zink_frame_done done;
   void *arg;
   uint32_t row, width, height;
};

static pthread_once_t ahead_once = PTHREAD_ONCE_INIT;
static int ahead_on, keep_on, newest_on, subdata_on;
static void ahead_read_switches(void)
{
   ahead_on = nvk_zink_cfg_on("NVK_ZINK_PRESENT_AHEAD");
   keep_on = nvk_zink_cfg_on("NVK_ZINK_SURFACE_KEEP");
   newest_on = nvk_zink_cfg_on("NVK_ZINK_PRESENT_NEWEST");
   subdata_on = nvk_zink_cfg_on("NVK_ZINK_SUBDATA_STAGE");
}

/* 3.0.16: whether glBufferSubData and a glBufferData of the same size write through a staging buffer where the
 * threaded context would map the buffer and wait for the GPU (tc_subdata_stage_instead in Mesa's
 * u_threaded_context.h, zink/mesa-zink-nvk.patch). Zink's context creation asks it once per context. */
int nvk_zink_subdata_stage_enabled(void)
{
   pthread_once(&ahead_once, ahead_read_switches);
   return subdata_on;
}

int nvk_present_ahead_enabled(void)
{
   pthread_once(&ahead_once, ahead_read_switches);
   return ahead_on;
}

int nvk_present_newest_enabled(void)
{
   pthread_once(&ahead_once, ahead_read_switches);
   /* The sender sees nothing of what a diagnostic observer is promised (it is called during the swap it was armed
    * for), so a process that has one presents every frame from its swap. */
   return ahead_on && newest_on && !atomic_load(&present_observer);
}

unsigned nvk_present_image_layout(struct pipe_resource *image)
{
   struct zink_resource *resource = image ? zink_resource(image) : NULL;
   return resource && resource->obj && !resource->obj->is_buffer ? (unsigned)resource->layout : 0;
}

static void ahead_import_free(struct nvk_present_ahead *a, struct ahead_import *import)
{
   struct zink_screen *screen = a->screen;
   if (import->buffer) screen->vk.DestroyBuffer(screen->dev, import->buffer, NULL);
   if (import->memory) screen->vk.FreeMemory(screen->dev, import->memory, NULL);
   if (import->surface) CFRelease(import->surface);
   memset(import, 0, sizeof(*import));
}

static void ahead_free(struct nvk_present_ahead *a)
{
   struct zink_screen *screen = a->screen;
   for (unsigned i = 0; i < AHEAD_KEPT; ++i) ahead_import_free(a, &a->kept[i]);
   if (a->pool) screen->vk.DestroyCommandPool(screen->dev, a->pool, NULL);
   if (a->fence) screen->vk.DestroyFence(screen->dev, a->fence, NULL);
   if (a->renderer) dlclose(a->renderer);
   free(a);
}

/* NULL: this screen is not one nvk_zink_copy_vram would present on either, or an object could not be made. */
static struct nvk_present_ahead *ahead_create(struct pipe_screen *pscreen, int *uncertain)
{
   struct zink_screen *screen = zink_screen(pscreen);
   if (screen->info.driver_props.driverID != VK_DRIVER_ID_MESA_NVK || screen->info.props.vendorID != 0x10de ||
       !screen->info.have_KHR_external_memory_fd)
      return NULL;
   /* The private hooks come from the ICD that implements this device's own entry point, as above. */
   if (!util_dl_get_proc_address(screen->loader_lib, "nvkbridgeSetPresentObserver")) return NULL;
   PFN_vkVoidFunction copy = screen->vk_GetDeviceProcAddr(screen->dev, "vkCmdCopyImageToBuffer");
   Dl_info origin = {0};
   if (!copy || !dladdr((void *)copy, &origin) || !origin.dli_fname) return NULL;
   struct nvk_present_ahead *a = calloc(1, sizeof(*a));
   if (!a) return NULL;
   a->screen = screen;
   a->uncertain = uncertain;
   a->renderer = dlopen(origin.dli_fname, RTLD_NOW | RTLD_LOCAL | RTLD_FIRST | RTLD_NOLOAD);
   if (!a->renderer) { free(a); return NULL; }
   PFN_vkGetInstanceProcAddr gipa = (void *)dlsym(a->renderer, "vk_icdGetInstanceProcAddr");
   PFN_vkGetDeviceProcAddr gdpa = gipa ? (void *)gipa(screen->instance, "vkGetDeviceProcAddr") : NULL;
   a->stage = (void *)dlsym(a->renderer, "nvmtl_nvk_stage_import");
   a->dirty = (void *)dlsym(a->renderer, "nvmtl_nvk_surface_dirty");
   if (!gdpa || gdpa(screen->dev, "vkCmdCopyImageToBuffer") != copy || !a->stage || !a->dirty) goto failed;
   /* An NVK from before 3.0.7 has neither of the next two: every frame then gets its own import. */
   a->dirty_at = (void *)dlsym(a->renderer, "nvmtl_nvk_surface_dirty_at");
   a->cookie = (void *)dlsym(a->renderer, "nvmtl_nvk_import_cookie");
   a->dropped = (void *)dlsym(a->renderer, "nvmtl_nvk_fence_dropped_v1");
   a->keep = keep_on && a->dirty_at && a->cookie;
   VkCommandPoolCreateInfo pool = { .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
      .queueFamilyIndex = screen->gfx_queue };
   if (screen->vk.CreateCommandPool(screen->dev, &pool, NULL, &a->pool) != VK_SUCCESS) goto failed;
   VkCommandBufferAllocateInfo cb = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
      .commandPool = a->pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1 };
   if (screen->vk.AllocateCommandBuffers(screen->dev, &cb, &a->command) != VK_SUCCESS) goto failed;
   VkFenceCreateInfo fence = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
   if (screen->vk.CreateFence(screen->dev, &fence, NULL, &a->fence) != VK_SUCCESS) goto failed;
   return a;
failed:
   ahead_free(a);
   return NULL;
}

/* The import of this surface's VRAM: the kept one, or a new one. NULL: NVK did not import it (the caller presents
 * the way 3.0.6 does, which also knows what to do then). */
static struct ahead_import *ahead_import_for(struct nvk_present_ahead *a, IOSurfaceRef surface, VkDeviceSize bytes,
                                             size_t size)
{
   struct zink_screen *screen = a->screen;
   const uint32_t id = IOSurfaceGetID(surface);
   struct ahead_import *slot = NULL;
   for (unsigned i = 0; i < AHEAD_KEPT; ++i) {
      /* A kept import retains its surface, so no other surface can have its ID. */
      if (a->kept[i].surface && IOSurfaceGetID(a->kept[i].surface) == id) return &a->kept[i];
      if (!slot || !a->kept[i].surface || (slot->surface && a->kept[i].used < slot->used)) slot = &a->kept[i];
   }
   struct ahead_import made = {0};
   VkExternalMemoryBufferCreateInfo external = { .sType = VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_BUFFER_CREATE_INFO,
      .handleTypes = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT };
   VkBufferCreateInfo buffer = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .pNext = &external,
      .size = bytes, .usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
      .sharingMode = VK_SHARING_MODE_EXCLUSIVE };
   if (screen->vk.CreateBuffer(screen->dev, &buffer, NULL, &made.buffer) != VK_SUCCESS) return NULL;
   VkMemoryRequirements requirements;
   screen->vk.GetBufferMemoryRequirements(screen->dev, made.buffer, &requirements);
   uint32_t type;
   for (type = 0; type < screen->info.mem_props.memoryTypeCount; ++type)
      if ((requirements.memoryTypeBits & (1u << type)) &&
          (screen->info.mem_props.memoryTypes[type].propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)) break;
   int fd = -1;
   if (requirements.size > size || type == screen->info.mem_props.memoryTypeCount ||
       (fd = open("/dev/null", O_RDONLY | O_CLOEXEC)) < 0)
      goto failed;
   VkImportMemoryFdInfoKHR imported = { .sType = VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR,
      .handleType = VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT, .fd = fd };
   VkMemoryAllocateInfo allocation = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
      .pNext = &imported, .allocationSize = requirements.size, .memoryTypeIndex = type };
   const struct surface_import record = {3, id, 0, 0, requirements.size};
   a->stage(&record);
   VkResult result = screen->vk.AllocateMemory(screen->dev, &allocation, NULL, &made.memory);
   /* The block's name is this thread's until its next import: read before anything else can import here. */
   if (result == VK_SUCCESS && a->keep) made.cookie = a->cookie();
   a->stage(NULL);
   if (result != VK_SUCCESS) { close(fd); goto failed; }
   if (screen->vk.BindBufferMemory(screen->dev, made.buffer, made.memory, 0) != VK_SUCCESS) goto failed;
   made.surface = (IOSurfaceRef)CFRetain(surface);
   if (!made.cookie) slot = &a->own;   /* not named by the kernel: it lives for this frame, as in 3.0.6 */
   else if (slot->surface) ahead_import_free(a, slot);   /* the one used longest ago makes room */
   *slot = made;
   return slot;
failed:
   if (made.buffer) screen->vk.DestroyBuffer(screen->dev, made.buffer, NULL);
   if (made.memory) screen->vk.FreeMemory(screen->dev, made.memory, NULL);
   return NULL;
}

/* The presentation thread. Nothing of Gallium's context is touched here: every Vulkan object used is the screen's or
 * this frame's, and the scratch image is only read, which nobody draws into while its frame is in flight. Of the
 * context's own record (struct nvk_present_ahead) this thread may free kept imports and change keep and moved, which
 * nobody else reads while the frame is queued. */
static int ahead_work(void *frame)
{
   struct nvk_present_ahead *a = frame;
   struct zink_screen *screen = a->screen;
   /* Each frame is judged by its own fence. Once the share group is uncertain no later wait is more than a look, so
    * the frames of other windows are not each held for the full time behind the one that hung; nothing is sent
    * again then either (below). */
   VkResult result = screen->vk.WaitForFences(screen->dev, 1, &a->fence, VK_TRUE,
                                              p_atomic_read(a->uncertain) ? 0 : 5000000000ull);
   nvk_zink_present_observer observer = atomic_load(&present_observer);
   int observed = 1;
   if (result == VK_SUCCESS && observer) {
      uint32_t host_types = 0;
      for (uint32_t i = 0; i < screen->info.mem_props.memoryTypeCount; ++i)
         if ((screen->info.mem_props.memoryTypes[i].propertyFlags &
              (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) ==
             (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) host_types |= 1u << i;
      const struct nvk_zink_present_event event = {
         (void *)screen->vk_GetDeviceProcAddr, (void *)screen->dev, (void *)screen->queue,
         (uint64_t)a->import->buffer, a->row, a->width, a->height, screen->gfx_queue, host_types, 0};
      simple_mtx_lock(screen->queue_lock);
      observed = observer(&event);
      simple_mtx_unlock(screen->queue_lock);
   }
   if (result != VK_SUCCESS || observed < 0) {
      /* The flag before the answer: whoever the answer wakes reads it. */
      p_atomic_set(a->uncertain, 1);
      fprintf(stderr, "NVK Zink: presentation completion uncertain; share group quarantined\n");
      return NVK_PQ_WORK_UNCERTAIN;
   }
   int publish = 0;
   /* A fence also signals when its channel was rebuilt after a fault and the copy never ran: nothing to show then. */
   if (observed && !(a->dropped && a->dropped(screen->dev, a->fence) == 1)) {
      const uint32_t id = IOSurfaceGetID(a->surface);
      const uint32_t status = a->import->cookie ? a->dirty_at(screen->dev, id, 0, a->import->cookie)
                                                : a->dirty(screen->dev, id, 0);
      if (status && a->import->cookie) {
         /* The mark of a kept import was refused: the surface has another block by now (NVRM_SS_MOVED), or the
          * kernel could not say that it has this one. The pixels are not in the surface. The frame is still whole
          * in the scratch image, so it goes out again from here the way 3.0.6 sends every frame: a new import, the
          * copy, the wait, a mark without a name, the tag. It is done here and not left for the thread that next
          * comes for the frame, because an application that draws only when something changes may not come.
          * Not once the share group is uncertain (another window's frame did not finish meanwhile): 3.0.6 sends
          * no work then and frees nothing, and neither does this. */
         if (p_atomic_read(a->uncertain)) return NVK_PQ_WORK_UNCERTAIN;
         ahead_import_free(a, a->import);
         a->import = NULL;
         if (++a->moved >= AHEAD_MOVED_LIMIT && a->keep) {
            a->keep = false;
            for (unsigned i = 0; i < AHEAD_KEPT; ++i) ahead_import_free(a, &a->kept[i]);
            fprintf(stderr, "NVK Zink: marks for kept surfaces keep being refused; importing for every frame from now on\n");
         }
         /* A context that parks its frames (3.0.11) may be drawing the next one into the scratch image now: its
          * lock keeps that, and Zink's record of the image's layout, still for the copy. What goes out is then
          * the frame the image holds, this one or a newer one. */
         if (a->image_lock) pthread_mutex_lock(a->image_lock);
         const int again = nvk_zink_copy_vram(&screen->base, a->image, a->surface);
         if (a->image_lock) pthread_mutex_unlock(a->image_lock);
         if (again < 0) {
            p_atomic_set(a->uncertain, 1);
            fprintf(stderr, "NVK Zink: presentation completion uncertain; share group quarantined\n");
            return NVK_PQ_WORK_UNCERTAIN;
         }
         publish = again == 1;
      } else {
         publish = status == 0 && publish_tag(a->surface);
      }
   }
   return a->done(a->arg, publish) ? AHEAD_PUBLISHED : AHEAD_NOT_PUBLISHED;
}

/* The calling thread is inside the queue and took back a finished frame. 1 = it was published, 2 = it was not,
 * 0 = uncertain (nothing was freed, and the caller frees nothing either). */
static int ahead_settle(struct nvk_present_ahead *a, int result)
{
   struct pipe_screen *pscreen = &a->screen->base;
   /* The copy's fence signalled after everything Zink had submitted for the frame. Zink is told with its own
    * fence, so its record of what has finished keeps up as it did when the swap waited on that fence. */
   if (a->zink_fence) {
      if (!pscreen->fence_finish(pscreen, NULL, a->zink_fence, 0) &&
          !pscreen->fence_finish(pscreen, NULL, a->zink_fence, 5000000000ull)) {
         p_atomic_set(a->uncertain, 1);
         fprintf(stderr, "NVK Zink: completion uncertain; share group quarantined\n");
         return 0;
      }
      pscreen->fence_reference(pscreen, &a->zink_fence, NULL);
   }
   const int published = result == AHEAD_PUBLISHED;
   if (a->import == &a->own) ahead_import_free(a, &a->own);
   a->import = NULL;
   pipe_resource_reference(&a->image, NULL);
   CFRelease(a->surface); a->surface = NULL;
   a->done = NULL; a->arg = NULL;
   for (unsigned i = 0; i < AHEAD_KEPT; ++i)
      if (a->kept[i].surface && a->presents - a->kept[i].used >= AHEAD_AGE) ahead_import_free(a, &a->kept[i]);
   return published ? 1 : 2;
}

/* Take the queue, tidying up the frame that was in flight. 1 = inside, 0 = another thread is, -1 = uncertain. */
static int ahead_enter(struct nvk_pq *queue, int *uncertain, int wait_for_owner, int *settled)
{
   void *frame; int result;
   if (settled) *settled = 1;
   if (p_atomic_read(uncertain)) return -1;
   const int claimed = nvk_pq_claim(queue, wait_for_owner, &frame, &result);
   if (claimed == NVK_PQ_BUSY) return 0;
   if (claimed == NVK_PQ_LOST) {
      if (!p_atomic_read(uncertain))
         fprintf(stderr, "NVK Zink: the frame in flight did not come back; share group quarantined\n");
      p_atomic_set(uncertain, 1);
      return -1;
   }
   if (frame) {
      const int outcome = ahead_settle(frame, result);
      if (settled) *settled = outcome;
      if (!outcome) { nvk_pq_release(queue); return -1; }
   }
   return 1;
}

int nvk_present_ahead_begin(struct nvk_pq *queue, struct nvk_present_ahead **slot, struct pipe_screen *pscreen,
                            int *uncertain)
{
   pthread_once(&ahead_once, ahead_read_switches);
   if (!*slot && !nvk_pq_available()) return -2;
   const int inside = ahead_enter(queue, uncertain, 0, NULL);
   if (inside <= 0) return inside;
   if (!*slot && !(*slot = ahead_create(pscreen, uncertain))) { nvk_pq_release(queue); return -2; }
   return 1;
}

void nvk_present_ahead_cancel(struct nvk_pq *queue)
{
   nvk_pq_release(queue);
}

int nvk_present_ahead_drain(struct nvk_pq *queue, int *uncertain)
{
   int settled;
   const int inside = ahead_enter(queue, uncertain, 0, &settled);
   if (inside < 0) return 0;
   if (inside) nvk_pq_release(queue);
   return settled;
}

void nvk_present_ahead_trim(struct nvk_pq *queue, struct nvk_present_ahead **slot, int *uncertain)
{
   if (ahead_enter(queue, uncertain, 0, NULL) <= 0) return;
   if (*slot)
      for (unsigned i = 0; i < AHEAD_KEPT; ++i) ahead_import_free(*slot, &(*slot)->kept[i]);
   nvk_pq_release(queue);
}

int nvk_present_ahead_destroy(struct nvk_pq *queue, struct nvk_present_ahead **slot, int *uncertain)
{
   const int inside = ahead_enter(queue, uncertain, 1, NULL);
   if (inside <= 0) return 0;
   if (*slot) ahead_free(*slot);
   *slot = NULL;
   nvk_pq_release(queue);
   return 1;
}

int nvk_present_ahead_submit(struct nvk_pq *queue, struct nvk_present_ahead *a, struct pipe_resource *image,
                             unsigned layout, pthread_mutex_t *image_lock, IOSurfaceRef surface,
                             struct pipe_fence_handle *zink_fence, int synchronous, nvk_zink_frame_done done, void *arg)
{
   struct zink_screen *screen = a->screen;
   /* What nvk_zink_copy_vram refuses is refused here; it then refuses it itself, and its caller hears that. */
   if (!image || !surface || IOSurfaceGetPlaneCount(surface) > 1 || IOSurfaceGetPixelFormat(surface) != 0x42475241 ||
       IOSurfaceGetWidth(surface) != image->width0 || IOSurfaceGetHeight(surface) != image->height0 ||
       IOSurfaceGetBytesPerElement(surface) != 4 || image->format != PIPE_FORMAT_B8G8R8A8_UNORM)
      goto refused;
   size_t row = IOSurfaceGetBytesPerRow(surface), size = IOSurfaceGetAllocSize(surface);
   if (!row || row % 4 || row < (uint64_t)image->width0 * 4 || row / 4 > UINT32_MAX ||
       image->height0 > SIZE_MAX / row || row * image->height0 > size)
      goto refused;
   struct zink_resource *resource = zink_resource(image);
   /* The thread that draws reads Zink's record; a sender is told what the record said when the frame was parked. */
   const VkImageLayout prior = layout ? (VkImageLayout)layout : resource->layout;
   if (prior == VK_IMAGE_LAYOUT_UNDEFINED || !resource->obj || resource->obj->is_buffer) goto refused;
   struct ahead_import *import = ahead_import_for(a, surface, row * image->height0, size);
   if (!import) goto refused;
   /* Nothing is in flight, so the one command buffer and the one fence are idle and can be used again. */
   VkCommandBufferBeginInfo begin = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
      .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT };
   if (screen->vk.ResetCommandPool(screen->dev, a->pool, 0) != VK_SUCCESS ||
       screen->vk.ResetFences(screen->dev, 1, &a->fence) != VK_SUCCESS ||
       screen->vk.BeginCommandBuffer(a->command, &begin) != VK_SUCCESS)
      goto unused;
   VkImageMemoryBarrier barrier = { .sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER,
      .srcAccessMask = VK_ACCESS_MEMORY_WRITE_BIT, .dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT,
      .oldLayout = prior, .newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
      .srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
      .image = resource->obj->image, .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1} };
   screen->vk.CmdPipelineBarrier(a->command, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                                 0, 0, NULL, 0, NULL, 1, &barrier);
   VkBufferImageCopy region = { .bufferRowLength = row / 4, .bufferImageHeight = image->height0,
      .imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1},
      .imageExtent = {image->width0, image->height0, 1} };
   screen->vk.CmdCopyImageToBuffer(a->command, resource->obj->image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                                  import->buffer, 1, &region);
   barrier.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
   barrier.dstAccessMask = VK_ACCESS_MEMORY_READ_BIT | VK_ACCESS_MEMORY_WRITE_BIT;
   barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL; barrier.newLayout = prior;
   screen->vk.CmdPipelineBarrier(a->command, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
                                 0, 0, NULL, 0, NULL, 1, &barrier);
   if (screen->vk.EndCommandBuffer(a->command) != VK_SUCCESS) goto unused;
   a->import = import;
   import->used = ++a->presents;
   a->surface = (IOSurfaceRef)CFRetain(surface);
   pipe_resource_reference(&a->image, image);
   a->zink_fence = zink_fence;
   a->image_lock = image_lock;
   a->done = done; a->arg = arg;
   a->row = (uint32_t)row; a->width = image->width0; a->height = image->height0;
   VkSubmitInfo submit = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
      .commandBufferCount = 1, .pCommandBuffers = &a->command };
   /* The same queue, under Zink's own lock, right behind the batch that holds the scratch blit: the caller's flush
    * returned after that batch was submitted, and one queue runs its submissions in the order they were made. */
   simple_mtx_lock(screen->queue_lock);
   VkResult result = screen->vk.QueueSubmit(screen->queue, 1, &submit, a->fence);
   simple_mtx_unlock(screen->queue_lock);
   /* The copy has its place on the queue: the thread that draws may flush its next frame behind it. Nothing below
    * waits with the image's lock held (the presentation thread asks for it when it sends a frame again). */
   if (image_lock) pthread_mutex_unlock(image_lock);
   if (result != VK_SUCCESS) {
      /* As above: after a submission that may or may not have been taken nothing is destroyed or published. */
      p_atomic_set(a->uncertain, 1);
      nvk_pq_release(queue);
      return -1;
   }
   /* From here the frame is the queue's: the drawable is presented and released by `done`, on the presentation
    * thread. */
   if (!nvk_pq_submit(queue, a, ahead_work)) {
      /* No presentation thread after all (a fork's child): this thread does the waiting, as in 3.0.6. */
      const int answer = ahead_work(a);
      const int settled = answer == NVK_PQ_WORK_UNCERTAIN ? 0 : ahead_settle(a, answer);
      nvk_pq_release(queue);
      return !settled ? -1 : settled == 1 ? 2 : 3;
   }
   /* A caller that has to know now what became of the frame waits for it here, with the queue lock long dropped:
    * a diagnostic observer (it is called during the swap it was armed for) and a front-buffer flush (one that was
    * not published is tried again at the next flush). */
   if (synchronous > 0 || (!synchronous && atomic_load(&present_observer))) {
      int settled;
      const int inside = ahead_enter(queue, a->uncertain, 0, &settled);
      if (inside < 0) return -1;
      if (inside) nvk_pq_release(queue);
      return settled == 1 ? 2 : 3;
   }
   return 2;
unused:
   if (import == &a->own) ahead_import_free(a, &a->own);
refused:
   if (image_lock) pthread_mutex_unlock(image_lock);
   nvk_pq_release(queue);
   return -2;
}
