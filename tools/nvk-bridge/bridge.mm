// The native Vulkan presentation frontend (libnvk_minecraft.dylib), from the
// HighDelay/nvidia-macos-driver-nvk fork. Applications load this library as
// their Vulkan provider (Minecraft through -Dorg.lwjgl.vulkan.libname, other
// applications through nvk_bridge_icd.json and the Vulkan loader). Rendering
// goes to the driver's NVK, loaded from the private copy beside this library
// (libnvk_renderer.dylib, its own image, so its handles never mix with the
// NVK instance the Metal plugin runs in the same process). This frontend owns
// VK_EXT_metal_surface surfaces and swapchains: each presented image is copied
// on the GPU into the CAMetalLayer drawable's IOSurface VRAM (imported through
// the driver's nvmtl_nvk_stage_import), marked newest (nvmtl_nvk_surface_dirty)
// and tagged (the plugin's NVMTLVramAt:p0 generation tag) before it is
// presented. A FIFO swapchain presents synchronously: queue_present returns
// after the copy completed and the drawable was presented. An IMMEDIATE
// swapchain shows the newest frame (3.0.9, present_newest.h): queue_present
// hands the frame to a thread of the swapchain and returns, and that thread
// copies the newest frame into a drawable when the layer has one; a frame that
// was overtaken first is not copied. NVK_BRIDGE_PRESENT_NEWEST=0 in the
// settings file makes IMMEDIATE synchronous again. The renderer's own
// window-system extensions and entry points are hidden
// (unsupported_device_extension).
#define VK_NO_PROTOTYPES
#define VK_USE_PLATFORM_METAL_EXT
#include <vulkan/vulkan.h>
#import <Foundation/Foundation.h>
#import <QuartzCore/QuartzCore.h>
#import <Metal/Metal.h>
#import <IOSurface/IOSurface.h>
#include <IOKit/IOKitLib.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>
#include <new>
#include <string>
#include <type_traits>
#include <unordered_map>
#include <vector>
#include "present_newest.h"

extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(VkInstance, const char *);
extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(VkDevice, const char *);
extern "C" VKAPI_ATTR VkResult VKAPI_CALL vkCreateMetalSurfaceEXT(
   VkInstance, const VkMetalSurfaceCreateInfoEXT *, const VkAllocationCallbacks *, VkSurfaceKHR *);

namespace {
struct Share { uint32_t version, surface, plane, pad; uint64_t bytes; };
static_assert(sizeof(Share) == 24);
using StageImport = void (*)(const Share *);
using SurfaceDirty = uint32_t (*)(VkDevice, uint32_t, uint32_t);
using FenceDropped = int (*)(VkDevice, VkFence);
PFN_vkGetInstanceProcAddr backend_gipa;
PFN_vkGetDeviceProcAddr backend_gdpa;
StageImport stage_import;
SurfaceDirty surface_dirty;
FenceDropped fence_dropped;
void *backend;
std::once_flag backend_once;
std::once_flag device_procs_once;
std::atomic<uint64_t> next_handle{0x4e564b0000000000ull};
std::atomic<uint64_t> copies{}, copy_bytes{}, drawable_ns{}, producer_ns{}, import_failures{};
// The swapchains that show the newest frame: frames handed to a presentation thread, frames it showed, frames that
// were overtaken or given up, frames it could not show, and the time it waited for the layer.
std::atomic<uint64_t> newest_presented{}, newest_shown{}, newest_dropped{}, newest_failed{}, newest_drawable_ns{};
// A child of fork has none of the parent's threads: a swapchain made there presents synchronously. (A swapchain the
// parent made is the parent's, with its layer and its device; a child that goes on using it was never supported.)
std::atomic<bool> forked{};
// Whether the machine has slept, for the waits whose running out makes a device's completion uncertain
// (present_newest.h, wait_whole): CLOCK_UPTIME_RAW stands still during a sleep and CLOCK_MONOTONIC_RAW goes on. The
// clock that goes on is read first: a thread held up between the two readings then sees the clocks closer than they
// are, never farther apart, and a reading that is too close counts for nothing (WakeWatch). Never destroyed: a
// presentation thread may ask while the process exits.
uint64_t machine_woke() {
   static auto &watch=*new nvkbridge::WakeWatch;
   const uint64_t all=clock_gettime_nsec_np(CLOCK_MONOTONIC_RAW);
   const uint64_t awake=clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
   return watch.look(awake,all);
}
using PresentObserver=void (*)(PFN_vkGetDeviceProcAddr,VkDevice,VkQueue,VkBuffer,uint32_t,uint32_t,uint32_t,uint32_t);
std::atomic<PresentObserver> present_observer{};
std::mutex registry;

struct Instance { VkInstance raw{}; };
struct Device;
struct Surface {
   CAMetalLayer *__strong layer;
   // A presentation thread could not show a frame on this layer (no drawable within the layer's timeout: a hidden
   // window; or a drawable of another size: a resize): presents are synchronous, and so report what is wrong, until
   // one of them reaches the layer. The swapchain made after a resize starts under it too.
   std::atomic<bool> held{};
};
struct Queue { VkQueue raw{}; uint32_t family{}; std::weak_ptr<Device> dev; };
// done: on a swapchain that shows the newest frame, the fence vkQueuePresentKHR submits behind the frame's rendering.
struct Image { VkImage raw{}; VkDeviceMemory memory{}; bool acquired{}; VkFence done{}; };
struct Frame;
struct Chain;
// The GPU and the layer of one swapchain, as present_newest.h asks for them. The swapchain owns its Newest, so the
// pointer holds for as long as the Newest does.
struct NewestBackend {
   Chain *chain;
   bool uncertain() const;
   void set_uncertain();
   void hold();
   void dropped() { newest_dropped++; }
   nvkbridge::Wait wait_done(uint32_t k,uint64_t ns);
   uint64_t woke() { return machine_woke(); }
   id<CAMetalDrawable> next_drawable();
   nvkbridge::Copy copy(uint32_t k,id<CAMetalDrawable> drawable);
};
struct Chain : std::enable_shared_from_this<Chain> {
   std::shared_ptr<Device> dev;
   std::shared_ptr<Surface> surface;
   VkSwapchainKHR handle{};
   VkFormat format{};
   VkExtent2D extent{};
   VkPresentModeKHR present_mode{};
   std::vector<Image> images;
   std::mutex mutex;
   std::condition_variable available;
   uint32_t cursor{};
   bool retired{};
   // Set for a swapchain that shows the newest frame: it then owns the images' states, and mutex, available, cursor
   // and Image::acquired above are not used.
   std::unique_ptr<nvkbridge::Newest<NewestBackend>> newest;
   VkAllocationCallbacks allocator{};
   bool custom_allocator{};
   const VkAllocationCallbacks *alloc() const { return custom_allocator ? &allocator : nullptr; }
};
struct Device {
   VkDevice raw{};
   VkPhysicalDevice physical{};
   std::shared_ptr<Instance> instance;
   VkPhysicalDeviceProperties properties{};
   VkPhysicalDeviceMemoryProperties memory{};
   id<MTLDevice> __strong metal;
   uint32_t copy_family{}, copy_index{};
   VkQueue copy_queue{};
   bool swapchain{};
   bool private_copy_queue{};
   VkDeviceSize host_present_alignment{};
   std::atomic<bool> uncertain{};
   std::recursive_mutex submit;
};
std::unordered_map<VkInstance, std::shared_ptr<Instance>> instances;
std::unordered_map<VkPhysicalDevice, std::shared_ptr<Instance>> physicals;
std::unordered_map<VkDevice, std::shared_ptr<Device>> devices;
std::unordered_map<VkQueue, std::shared_ptr<Queue>> queues;
std::unordered_map<VkSurfaceKHR, std::shared_ptr<Surface>> surfaces;
std::unordered_map<VkSwapchainKHR, std::shared_ptr<Chain>> chains;

template<class T, class K> std::shared_ptr<T> find(const std::unordered_map<K, std::shared_ptr<T>> &map, K key) {
   std::lock_guard<std::mutex> lock(registry);
   auto it = map.find(key);
   return it == map.end() ? nullptr : it->second;
}
template<class T> T global(const char *name) { return reinterpret_cast<T>(backend_gipa(VK_NULL_HANDLE, name)); }
template<class T> T iproc(VkInstance i, const char *name) { return reinterpret_cast<T>(backend_gipa(i, name)); }
template<class T> T dproc(VkDevice d, const char *name) { return reinterpret_cast<T>(backend_gdpa(d, name)); }
#define DP(dev, name) dproc<PFN_vk##name>((dev)->raw, "vk" #name)
#define IP(inst, name) iproc<PFN_vk##name>((inst)->raw, "vk" #name)


bool initialize() {
   std::call_once(backend_once, [] {
      Dl_info self{};
      if (!dladdr(reinterpret_cast<void *>(&vkGetInstanceProcAddr), &self) || !self.dli_fname) return;
      std::string path(self.dli_fname);
      path.resize(path.find_last_of('/') + 1);
      path += "libnvk_renderer.dylib";
      backend = dlopen(path.c_str(), RTLD_NOW | RTLD_LOCAL | RTLD_FIRST);
      if (!backend) { fprintf(stderr, "NVK Bridge: renderer load failed: %s\n", dlerror()); return; }
      backend_gipa = reinterpret_cast<PFN_vkGetInstanceProcAddr>(dlsym(backend, "vk_icdGetInstanceProcAddr"));
      stage_import = reinterpret_cast<StageImport>(dlsym(backend, "nvmtl_nvk_stage_import"));
      surface_dirty = reinterpret_cast<SurfaceDirty>(dlsym(backend, "nvmtl_nvk_surface_dirty"));
      // Since 3.0.1; a renderer without it answers every fence as before.
      fence_dropped = reinterpret_cast<FenceDropped>(dlsym(backend, "nvmtl_nvk_fence_dropped_v1"));
      if (!backend_gipa || !stage_import || !surface_dirty) backend_gipa = nullptr;
      fprintf(stderr, "NVK Bridge: renderer=%s; presentation=gpu-copy; game-frame CPU pixel traffic=0\n", path.c_str());
   });
   return backend_gipa != nullptr;
}

template<class T> VkResult enumerate(const std::vector<T> &items, uint32_t *count, T *out) {
   if (!count) return VK_ERROR_INITIALIZATION_FAILED;
   if (!out) { *count = static_cast<uint32_t>(items.size()); return VK_SUCCESS; }
   uint32_t n = std::min(*count, static_cast<uint32_t>(items.size()));
   std::copy_n(items.begin(), n, out);
   *count = n;
   return n < items.size() ? VK_INCOMPLETE : VK_SUCCESS;
}
VkExtensionProperties extension(const char *name, uint32_t version) {
   VkExtensionProperties e{};
   strlcpy(e.extensionName, name, sizeof e.extensionName); e.specVersion = version;
   return e;
}
bool instance_owned(const char *name) {
   return !strcmp(name, VK_KHR_SURFACE_EXTENSION_NAME) || !strcmp(name, VK_EXT_METAL_SURFACE_EXTENSION_NAME) ||
          !strcmp(name, VK_KHR_GET_SURFACE_CAPABILITIES_2_EXTENSION_NAME);
}
// The driver's NVK has its own window-system layer (a CAMetalLayer copy done by the CPU), so it offers the surface and
// swapchain extensions below too. This frontend replaces that layer and implements none of them: they are taken out
// of what the renderer offers and refused when an application asks for them, so that no application hands one of this
// frontend's surface or swapchain handles to the renderer's window-system layer.
bool unsupported_instance_extension(const char *name) {
   for (const char *wsi : {VK_EXT_HEADLESS_SURFACE_EXTENSION_NAME, "VK_KHR_surface_maintenance1", "VK_EXT_surface_maintenance1"})
      if (!strcmp(name, wsi)) return true;
   return false;
}
bool unsupported_device_extension(const char *name) {
   for (const char *wsi : {"VK_KHR_present_id", "VK_KHR_present_id2", "VK_KHR_present_wait", "VK_KHR_present_wait2",
        "VK_KHR_swapchain_maintenance1", "VK_EXT_swapchain_maintenance1", "VK_KHR_swapchain_mutable_format",
        "VK_KHR_incremental_present", "VK_EXT_present_timing", "VK_GOOGLE_display_timing", "VK_EXT_hdr_metadata",
        "VK_EXT_full_screen_exclusive", "VK_KHR_display_swapchain", "VK_EXT_display_control"})
      if (!strcmp(name, wsi)) return true;
   return false;
}
// Entry points that take a surface, a swapchain or a presentation: this frontend answers them alone (lookup), and one
// it does not implement is absent rather than the renderer's.
bool window_system_proc(const char *name) {
   return strstr(name, "Surface") || strstr(name, "Swapchain") || strstr(name, "Present") ||
          !strcmp(name, "vkAcquireNextImageKHR") || !strcmp(name, "vkAcquireNextImage2KHR") ||
          !strcmp(name, "vkSetHdrMetadataEXT") || !strcmp(name, "vkGetRefreshCycleDurationGOOGLE");
}
bool valid_format(VkFormat f) { return f == VK_FORMAT_B8G8R8A8_UNORM || f == VK_FORMAT_B8G8R8A8_SRGB; }
uint64_t elapsed(std::chrono::steady_clock::time_point start) {
   return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now() - start).count();
}
int transfer_family(VkPhysicalDevice physical, const std::shared_ptr<Instance> &instance) {
   uint32_t count = 0; IP(instance, GetPhysicalDeviceQueueFamilyProperties)(physical, &count, nullptr);
   std::vector<VkQueueFamilyProperties> props(count);
   IP(instance, GetPhysicalDeviceQueueFamilyProperties)(physical, &count, props.data());
   for (uint32_t i = 0; i < count; i++)
      if (props[i].queueCount && (props[i].queueFlags & VK_QUEUE_TRANSFER_BIT) &&
          !(props[i].queueFlags & (VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT))) return static_cast<int>(i);
   for (uint32_t i = 0; i < count; i++)
      if (props[i].queueCount && (props[i].queueFlags & VK_QUEUE_TRANSFER_BIT)) return static_cast<int>(i);
   return -1;
}
id<MTLDevice> metal_device(const VkPhysicalDeviceProperties &props) {
   if (props.vendorID != 0x10de) return nil;
   io_iterator_t iterator{};
   if (IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("NVAccel"), &iterator)) return nil;
   uint64_t selected = 0; unsigned matches = 0;
   for (io_service_t service; (service = IOIteratorNext(iterator)); IOObjectRelease(service)) {
      io_registry_entry_t parent{};
      if (IORegistryEntryGetParentEntry(service, kIOServicePlane, &parent)) continue;
      CFTypeRef data = IORegistryEntryCreateCFProperty(parent, CFSTR("device-id"), kCFAllocatorDefault, 0);
      uint32_t device_id = 0;
      if (data && CFGetTypeID(data) == CFDataGetTypeID() && CFDataGetLength((CFDataRef)data) >= 4)
         memcpy(&device_id, CFDataGetBytePtr((CFDataRef)data), 4);
      if (data) CFRelease(data);
      IOObjectRelease(parent);
      if (device_id == props.deviceID) { IORegistryEntryGetRegistryEntryID(service, &selected); matches++; }
   }
   IOObjectRelease(iterator);
   if (matches != 1) return nil;
   for (id<MTLDevice> device in MTLCopyAllDevices())
      if (device.registryID == selected) return device;
   return nil;
}

VkResult VKAPI_CALL create_instance(const VkInstanceCreateInfo *info, const VkAllocationCallbacks *alloc, VkInstance *result) {
   if (!initialize()) return VK_ERROR_INCOMPATIBLE_DRIVER;
   std::vector<const char *> extensions;
   for (uint32_t i = 0; i < info->enabledExtensionCount; i++) {
      const char *name = info->ppEnabledExtensionNames[i];
      if (unsupported_instance_extension(name)) return VK_ERROR_EXTENSION_NOT_PRESENT;
      if (!instance_owned(name)) extensions.push_back(name);
   }
   VkInstanceCreateInfo copy = *info;
   copy.enabledExtensionCount = static_cast<uint32_t>(extensions.size()); copy.ppEnabledExtensionNames = extensions.data();
   VkResult status = global<PFN_vkCreateInstance>("vkCreateInstance")(&copy, alloc, result);
   if (status != VK_SUCCESS) return status;
   auto state = std::make_shared<Instance>(); state->raw = *result;
   std::lock_guard<std::mutex> lock(registry); instances[*result] = state;
   return VK_SUCCESS;
}
void VKAPI_CALL destroy_instance(VkInstance instance, const VkAllocationCallbacks *alloc) {
   if (!instance) return;
   auto state = find(instances, instance);
   if (!state) return;
   { std::lock_guard<std::mutex> lock(registry);
     for (auto it = physicals.begin(); it != physicals.end();) {
        if (it->second == state) it = physicals.erase(it); else ++it;
     }
     instances.erase(instance);
   }
   IP(state, DestroyInstance)(instance, alloc);
}
VkResult VKAPI_CALL instance_extensions(const char *layer, uint32_t *count, VkExtensionProperties *out) {
   if (!initialize()) return VK_ERROR_INCOMPATIBLE_DRIVER;
   auto fn = global<PFN_vkEnumerateInstanceExtensionProperties>("vkEnumerateInstanceExtensionProperties");
   if (layer) return fn(layer, count, out);
   uint32_t n = 0; VkResult r = fn(nullptr, &n, nullptr); if (r != VK_SUCCESS) return r;
   std::vector<VkExtensionProperties> all(n); r = fn(nullptr, &n, all.data()); if (r != VK_SUCCESS) return r;
   all.resize(n);
   all.erase(std::remove_if(all.begin(), all.end(), [](auto &e) { return instance_owned(e.extensionName) ||
      unsupported_instance_extension(e.extensionName); }), all.end());
   all.push_back(extension(VK_KHR_SURFACE_EXTENSION_NAME, VK_KHR_SURFACE_SPEC_VERSION));
   all.push_back(extension(VK_EXT_METAL_SURFACE_EXTENSION_NAME, VK_EXT_METAL_SURFACE_SPEC_VERSION));
   all.push_back(extension(VK_KHR_GET_SURFACE_CAPABILITIES_2_EXTENSION_NAME, VK_KHR_GET_SURFACE_CAPABILITIES_2_SPEC_VERSION));
   return enumerate(all, count, out);
}
VkResult VKAPI_CALL instance_layers(uint32_t *count, VkLayerProperties *out) {
   return enumerate(std::vector<VkLayerProperties>{}, count, out);
}
VkResult VKAPI_CALL instance_version(uint32_t *version) {
   if (!initialize()) return VK_ERROR_INCOMPATIBLE_DRIVER;
   auto fn = global<PFN_vkEnumerateInstanceVersion>("vkEnumerateInstanceVersion");
   return fn ? fn(version) : (*version = VK_API_VERSION_1_0, VK_SUCCESS);
}
VkResult VKAPI_CALL enumerate_physical(VkInstance instance, uint32_t *count, VkPhysicalDevice *out) {
   auto state = find(instances, instance); if (!state) return VK_ERROR_INITIALIZATION_FAILED;
   VkResult result = IP(state, EnumeratePhysicalDevices)(instance, count, out);
   if (out && result >= 0) { std::lock_guard<std::mutex> lock(registry); for (uint32_t i=0;i<*count;i++) physicals[out[i]] = state; }
   return result;
}
VkResult VKAPI_CALL device_extensions(VkPhysicalDevice physical, const char *layer, uint32_t *count, VkExtensionProperties *out) {
   auto state = find(physicals, physical); if (!state) return VK_ERROR_INITIALIZATION_FAILED;
   auto fn = IP(state, EnumerateDeviceExtensionProperties);
   if (layer) return fn(physical, layer, count, out);
   uint32_t n=0; VkResult r=fn(physical, nullptr, &n, nullptr); if (r != VK_SUCCESS) return r;
   std::vector<VkExtensionProperties> all(n); r=fn(physical, nullptr, &n, all.data()); if (r != VK_SUCCESS) return r;
   all.resize(n);
   all.erase(std::remove_if(all.begin(),all.end(),[](auto &e) {
      return !strcmp(e.extensionName, VK_KHR_SWAPCHAIN_EXTENSION_NAME) || unsupported_device_extension(e.extensionName);
   }),all.end());
   if (transfer_family(physical, state) >= 0) all.push_back(extension(VK_KHR_SWAPCHAIN_EXTENSION_NAME, VK_KHR_SWAPCHAIN_SPEC_VERSION));
   return enumerate(all,count,out);
}

bool limited_bar_memory(const VkPhysicalDeviceMemoryProperties &memory) {
   VkDeviceSize vram=0;
   for(uint32_t h=0;h<memory.memoryHeapCount;h++)
      if(memory.memoryHeaps[h].flags&VK_MEMORY_HEAP_DEVICE_LOCAL_BIT)
         vram=std::max(vram,memory.memoryHeaps[h].size);
   if(!vram)return false;
   bool system_uploads=false;
   for(uint32_t t=0;t<memory.memoryTypeCount;t++) {
      const auto &type=memory.memoryTypes[t];
      if(type.heapIndex>=memory.memoryHeapCount)continue;
      const auto flags=type.propertyFlags;
      if((flags&(VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT|VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT))==
         (VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT|VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT) &&
         memory.memoryHeaps[type.heapIndex].size>=vram)return false;
      if(!(flags&VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) &&
         (flags&(VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT))==
         (VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT))system_uploads=true;
   }
   return system_uploads;
}

void enable_host_present(const std::shared_ptr<Device> &d,std::vector<const char *> &extensions) {
   // Full BAR keeps its original device extensions and VRAM import path. Only
   // limited-BAR devices may recover a refused drawable import using its host
   // pages. Pixels still travel directly from the GPU.
   if(!limited_bar_memory(d->memory))return;
   uint32_t count=0;
   auto enumerate_extensions=IP(d->instance,EnumerateDeviceExtensionProperties);
   if(enumerate_extensions(d->physical,nullptr,&count,nullptr)!=VK_SUCCESS)return;
   std::vector<VkExtensionProperties> available(count);
   if(enumerate_extensions(d->physical,nullptr,&count,available.data())!=VK_SUCCESS)return;
   available.resize(count);
   if(std::none_of(available.begin(),available.end(),[](const auto &e) {
      return !strcmp(e.extensionName,VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME);
   }))return;
   auto properties=IP(d->instance,GetPhysicalDeviceProperties2);
   if(!properties)properties=iproc<PFN_vkGetPhysicalDeviceProperties2>(d->instance->raw,"vkGetPhysicalDeviceProperties2KHR");
   if(!properties)return;
   VkPhysicalDeviceExternalMemoryHostPropertiesEXT host{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_MEMORY_HOST_PROPERTIES_EXT};
   VkPhysicalDeviceProperties2 all{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2};all.pNext=&host;
   properties(d->physical,&all);
   const auto alignment=host.minImportedHostPointerAlignment;
   if(!alignment || alignment>SIZE_MAX || (alignment&(alignment-1)))return;
   if(std::none_of(extensions.begin(),extensions.end(),[](auto name) {
      return !strcmp(name,VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME);
   }))extensions.push_back(VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME);
   d->host_present_alignment=alignment;
}

VkResult VKAPI_CALL create_device(VkPhysicalDevice physical, const VkDeviceCreateInfo *info, const VkAllocationCallbacks *alloc, VkDevice *result) {
   auto instance = find(physicals,physical); if (!instance) return VK_ERROR_INITIALIZATION_FAILED;
   auto state = std::make_shared<Device>(); state->physical=physical; state->instance=instance;
   IP(instance, GetPhysicalDeviceProperties)(physical,&state->properties);
   IP(instance, GetPhysicalDeviceMemoryProperties)(physical,&state->memory);
   std::vector<const char *> extensions;
   for (uint32_t i=0;i<info->enabledExtensionCount;i++) {
      const char *name=info->ppEnabledExtensionNames[i];
      if (unsupported_device_extension(name)) return VK_ERROR_EXTENSION_NOT_PRESENT;
      if (!strcmp(name,VK_KHR_SWAPCHAIN_EXTENSION_NAME)) state->swapchain=true;
      else extensions.push_back(name);
   }
   std::vector<VkDeviceQueueCreateInfo> queue_info(info->pQueueCreateInfos,info->pQueueCreateInfos+info->queueCreateInfoCount);
   float priority=1.0f;
   std::vector<float> expanded_priorities;
   if (state->swapchain) {
      int family=transfer_family(physical,instance);
      if (family<0 || !(state->metal=metal_device(state->properties))) {
         fprintf(stderr,"NVK Bridge: a matching NVIDIA Metal device and transfer-capable queue are required\n");
         return VK_ERROR_FEATURE_NOT_PRESENT;
      }
      state->copy_family=static_cast<uint32_t>(family);
      bool present=false;
      for (auto &q:queue_info) if (q.queueFamilyIndex==state->copy_family) {
         uint32_t n=0; IP(instance,GetPhysicalDeviceQueueFamilyProperties)(physical,&n,nullptr);
         std::vector<VkQueueFamilyProperties> props(n); IP(instance,GetPhysicalDeviceQueueFamilyProperties)(physical,&n,props.data());
         state->copy_index=q.queueCount<props[family].queueCount?q.queueCount:0;
         if (state->copy_index) {
            state->private_copy_queue=true;
            expanded_priorities.assign(q.pQueuePriorities,q.pQueuePriorities+q.queueCount);
            expanded_priorities.push_back(priority); q.queueCount++; q.pQueuePriorities=expanded_priorities.data();
         }
         present=true; break;
      }
      if (!present) {
         state->private_copy_queue=true;
         VkDeviceQueueCreateInfo q{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
         q.queueFamilyIndex=state->copy_family; q.queueCount=1; q.pQueuePriorities=&priority; queue_info.push_back(q);
      }
      if (std::none_of(extensions.begin(),extensions.end(),[](auto name) { return !strcmp(name,VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME); }))
         extensions.push_back(VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME);
      enable_host_present(state,extensions);
   }
   VkDeviceCreateInfo copy=*info;
   copy.enabledExtensionCount=static_cast<uint32_t>(extensions.size()); copy.ppEnabledExtensionNames=extensions.data();
   copy.queueCreateInfoCount=static_cast<uint32_t>(queue_info.size()); copy.pQueueCreateInfos=queue_info.data();
   VkResult status=IP(instance,CreateDevice)(physical,&copy,alloc,result); if (status!=VK_SUCCESS) return status;
   state->raw=*result;
   std::call_once(device_procs_once,[&] { backend_gdpa=IP(instance,GetDeviceProcAddr); });
   if (state->swapchain) DP(state,GetDeviceQueue)(*result,state->copy_family,state->copy_index,&state->copy_queue);
   { std::lock_guard<std::mutex> lock(registry); devices[*result]=state; }
   fprintf(stderr,"NVK Bridge: native device=%s; copy family=%u; Metal registry=%llu\n",
      state->properties.deviceName,state->copy_family,(unsigned long long)state->metal.registryID);
   fprintf(stderr,"NVK Bridge: presentation completion=by swapchain; private copy queue=%s\n",state->private_copy_queue?"yes":"no");
   return VK_SUCCESS;
}
void VKAPI_CALL get_queue(VkDevice device,uint32_t family,uint32_t index,VkQueue *out) {
   auto state=find(devices,device); if (!state) { *out=VK_NULL_HANDLE; return; }
   DP(state,GetDeviceQueue)(device,family,index,out);
   auto q=std::make_shared<Queue>(); q->raw=*out; q->family=family; q->dev=state;
   std::lock_guard<std::mutex> lock(registry); queues[*out]=q;
}
void VKAPI_CALL get_queue2(VkDevice device,const VkDeviceQueueInfo2 *info,VkQueue *out) {
   auto state=find(devices,device); if (!state) { *out=VK_NULL_HANDLE; return; }
   DP(state,GetDeviceQueue2)(device,info,out);
   auto q=std::make_shared<Queue>(); q->raw=*out; q->family=info->queueFamilyIndex; q->dev=state;
   std::lock_guard<std::mutex> lock(registry); queues[*out]=q;
}
VkResult VKAPI_CALL queue_submit(VkQueue queue,uint32_t count,const VkSubmitInfo *infos,VkFence fence) {
   auto q=find(queues,queue); auto d=q?q->dev.lock():nullptr; if (!d || d->uncertain) return VK_ERROR_DEVICE_LOST;
   std::lock_guard<std::recursive_mutex> lock(d->submit); return d->uncertain?VK_ERROR_DEVICE_LOST:DP(d,QueueSubmit)(queue,count,infos,fence);
}
VkResult VKAPI_CALL queue_submit2(VkQueue queue,uint32_t count,const VkSubmitInfo2 *infos,VkFence fence) {
   auto q=find(queues,queue); auto d=q?q->dev.lock():nullptr; if (!d || d->uncertain) return VK_ERROR_DEVICE_LOST;
   std::lock_guard<std::recursive_mutex> lock(d->submit);
   if(d->uncertain)return VK_ERROR_DEVICE_LOST;
   auto fn=DP(d,QueueSubmit2); if (!fn) fn=dproc<PFN_vkQueueSubmit2>(d->raw,"vkQueueSubmit2KHR");
   return fn?fn(queue,count,infos,fence):VK_ERROR_EXTENSION_NOT_PRESENT;
}
VkResult VKAPI_CALL bind_sparse(VkQueue queue,uint32_t count,const VkBindSparseInfo *infos,VkFence fence) {
   auto q=find(queues,queue); auto d=q?q->dev.lock():nullptr; if (!d || d->uncertain) return VK_ERROR_DEVICE_LOST;
   std::lock_guard<std::recursive_mutex> lock(d->submit); return d->uncertain?VK_ERROR_DEVICE_LOST:DP(d,QueueBindSparse)(queue,count,infos,fence);
}
VkResult VKAPI_CALL queue_idle(VkQueue queue) {
   auto q=find(queues,queue); auto d=q?q->dev.lock():nullptr; if (!d || d->uncertain) return VK_ERROR_DEVICE_LOST;
   std::lock_guard<std::recursive_mutex> lock(d->submit); return d->uncertain?VK_ERROR_DEVICE_LOST:DP(d,QueueWaitIdle)(queue);
}
VkResult VKAPI_CALL device_idle(VkDevice device) {
   auto d=find(devices,device); if (!d || d->uncertain) return VK_ERROR_DEVICE_LOST;
   std::lock_guard<std::recursive_mutex> lock(d->submit); return d->uncertain?VK_ERROR_DEVICE_LOST:DP(d,DeviceWaitIdle)(device);
}
void VKAPI_CALL destroy_device(VkDevice device,const VkAllocationCallbacks *alloc) {
   auto d=find(devices,device); if (!d) return;
   // A swapchain the application did not destroy first: its presentation thread starts no copy from here on. Before
   // the submission lock is taken, because that thread needs the lock to finish the copy it is in.
   std::vector<std::shared_ptr<Chain>> left;
   { std::lock_guard<std::mutex> guard(registry);
     for (auto &entry:chains) if (entry.second->dev==d && entry.second->newest) left.push_back(entry.second);
   }
   for (auto &c:left) c->newest->retire();
   std::lock_guard<std::recursive_mutex> lock(d->submit);
   if (d->uncertain) { fprintf(stderr,"NVK Bridge: uncertain GPU completion; retaining native device until process exit\n"); return; }
   DP(d,DeviceWaitIdle)(device);
   { std::lock_guard<std::mutex> guard(registry);
     for (auto it=queues.begin();it!=queues.end();) { if (it->second->dev.lock()==d) it=queues.erase(it); else ++it; }
     devices.erase(device);
   }
   DP(d,DestroyDevice)(device,alloc);
}

VkResult VKAPI_CALL create_surface(VkInstance instance,const VkMetalSurfaceCreateInfoEXT *info,const VkAllocationCallbacks *,VkSurfaceKHR *out) {
   if (!find(instances,instance) || !info || !info->pLayer || info->flags) return VK_ERROR_INITIALIZATION_FAILED;
   auto surface=std::make_shared<Surface>(); surface->layer=(CAMetalLayer *)info->pLayer;
   if (![surface->layer isKindOfClass:[CAMetalLayer class]]) return VK_ERROR_INITIALIZATION_FAILED;
   *out=(VkSurfaceKHR)(uintptr_t)next_handle.fetch_add(1);
   std::lock_guard<std::mutex> lock(registry); surfaces[*out]=surface;
   return VK_SUCCESS;
}
void VKAPI_CALL destroy_surface(VkInstance,VkSurfaceKHR surface,const VkAllocationCallbacks *) {
   std::lock_guard<std::mutex> lock(registry);
   surfaces.erase(surface);
}
VkResult VKAPI_CALL surface_support(VkPhysicalDevice physical,uint32_t family,VkSurfaceKHR surface,VkBool32 *supported) {
   *supported=VK_FALSE;
   auto i=find(physicals,physical); auto s=find(surfaces,surface); if (!i || !s) return VK_ERROR_SURFACE_LOST_KHR;
   uint32_t count=0; IP(i,GetPhysicalDeviceQueueFamilyProperties)(physical,&count,nullptr);
   if (family>=count) return VK_SUCCESS;
   std::vector<VkQueueFamilyProperties> props(count); IP(i,GetPhysicalDeviceQueueFamilyProperties)(physical,&count,props.data());
   VkPhysicalDeviceProperties device{}; IP(i,GetPhysicalDeviceProperties)(physical,&device);
   id<MTLDevice> m=metal_device(device);
   *supported= m && transfer_family(physical,i)>=0 &&
      (props[family].queueFlags & (VK_QUEUE_GRAPHICS_BIT|VK_QUEUE_COMPUTE_BIT))==(VK_QUEUE_GRAPHICS_BIT|VK_QUEUE_COMPUTE_BIT) &&
      (!s->layer.device || s->layer.device.registryID==m.registryID);
   return VK_SUCCESS;
}
VkResult VKAPI_CALL surface_caps(VkPhysicalDevice physical,VkSurfaceKHR surface,VkSurfaceCapabilitiesKHR *caps) {
   auto i=find(physicals,physical); auto s=find(surfaces,surface); if (!i || !s) return VK_ERROR_SURFACE_LOST_KHR;
   VkPhysicalDeviceProperties props{}; IP(i,GetPhysicalDeviceProperties)(physical,&props);
   *caps={}; caps->minImageCount=2; caps->maxImageCount=8;
   CGSize size=s->layer.bounds.size; CGFloat scale=s->layer.contentsScale;
   uint32_t w=(uint32_t)std::max(0.0,size.width*scale),h=(uint32_t)std::max(0.0,size.height*scale);
   caps->currentExtent=w&&h?VkExtent2D{w,h}:VkExtent2D{UINT32_MAX,UINT32_MAX};
   caps->minImageExtent={1,1}; caps->maxImageExtent={props.limits.maxImageDimension2D,props.limits.maxImageDimension2D};
   caps->maxImageArrayLayers=1; caps->supportedTransforms=VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR; caps->currentTransform=VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR;
   caps->supportedCompositeAlpha=VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR|VK_COMPOSITE_ALPHA_PRE_MULTIPLIED_BIT_KHR;
   caps->supportedUsageFlags=VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT|VK_IMAGE_USAGE_TRANSFER_SRC_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT;
   return VK_SUCCESS;
}
VkResult VKAPI_CALL surface_caps2(VkPhysicalDevice physical,const VkPhysicalDeviceSurfaceInfo2KHR *info,VkSurfaceCapabilities2KHR *out) {
   for (VkBaseOutStructure *p=(VkBaseOutStructure *)out->pNext;p;p=p->pNext)
      if (p->sType==VK_STRUCTURE_TYPE_SURFACE_PROTECTED_CAPABILITIES_KHR)
         ((VkSurfaceProtectedCapabilitiesKHR *)p)->supportsProtected=VK_FALSE;
   return surface_caps(physical,info->surface,&out->surfaceCapabilities);
}
VkResult VKAPI_CALL surface_formats(VkPhysicalDevice physical,VkSurfaceKHR surface,uint32_t *count,VkSurfaceFormatKHR *out) {
   if (!find(physicals,physical) || !find(surfaces,surface)) return VK_ERROR_SURFACE_LOST_KHR;
   return enumerate(std::vector<VkSurfaceFormatKHR>{{VK_FORMAT_B8G8R8A8_SRGB,VK_COLOR_SPACE_SRGB_NONLINEAR_KHR},
      {VK_FORMAT_B8G8R8A8_UNORM,VK_COLOR_SPACE_SRGB_NONLINEAR_KHR}},count,out);
}
VkResult VKAPI_CALL surface_formats2(VkPhysicalDevice physical,const VkPhysicalDeviceSurfaceInfo2KHR *info,uint32_t *count,VkSurfaceFormat2KHR *out) {
   if (!out) return surface_formats(physical,info->surface,count,nullptr);
   uint32_t n=0;VkResult r=surface_formats(physical,info->surface,&n,nullptr);if (r!=VK_SUCCESS) return r;
   std::vector<VkSurfaceFormatKHR> formats(n);
   r=surface_formats(physical,info->surface,&n,formats.data());if (r!=VK_SUCCESS) return r;
   uint32_t written=std::min(n,*count);
   for (uint32_t j=0;j<written;j++) out[j].surfaceFormat=formats[j]; *count=written;
   return written<n?VK_INCOMPLETE:VK_SUCCESS;
}
VkResult VKAPI_CALL present_modes(VkPhysicalDevice physical,VkSurfaceKHR surface,uint32_t *count,VkPresentModeKHR *out) {
   if (!find(physicals,physical) || !find(surfaces,surface)) return VK_ERROR_SURFACE_LOST_KHR;
   return enumerate(std::vector<VkPresentModeKHR>{VK_PRESENT_MODE_FIFO_KHR,VK_PRESENT_MODE_IMMEDIATE_KHR},count,out);
}

int memory_type(const Device &d,uint32_t bits,VkMemoryPropertyFlags flags) {
   for (uint32_t i=0;i<d.memory.memoryTypeCount;i++) if ((bits&(1u<<i)) && (d.memory.memoryTypes[i].propertyFlags&flags)==flags) return (int)i;
   return -1;
}
// A wait for a fence on a presentation thread, or for one of its frames: the whole budget in one call, made again
// when the machine slept through it (present_newest.h, wait_whole). VK_TIMEOUT: the GPU did not get there while the
// machine was awake.
VkResult wait_fence_whole(Device *d,VkFence fence) {
   VkResult r=VK_TIMEOUT;
   nvkbridge::wait_whole([&] {
      r=DP(d,WaitForFences)(d->raw,1,&fence,VK_TRUE,nvkbridge::NewestLimits{}.done_ns);
      return r==VK_SUCCESS?nvkbridge::Wait::Signalled:r==VK_TIMEOUT?nvkbridge::Wait::Pending:nvkbridge::Wait::Failed;
   },machine_woke);
   return r;
}
void release_images(const std::shared_ptr<Chain> &c) {
   if (c->dev->uncertain) return;
   for (auto &im:c->images) {
      if (im.raw) DP(c->dev,DestroyImage)(c->dev->raw,im.raw,c->alloc());
      if (im.memory) DP(c->dev,FreeMemory)(c->dev->raw,im.memory,c->alloc());
      if (im.done) DP(c->dev,DestroyFence)(c->dev->raw,im.done,c->alloc());
      im={};
   }
}
// The way back of the swapchains that show the newest frame: a line NVK_BRIDGE_PRESENT_NEWEST=0 of the settings file
// (the file alone: this frontend reads no environment variable). Read once, when the first IMMEDIATE swapchain is
// made, so that a process that never makes one (every OpenGL application on Zink, which loads a copy of this
// library as its Vulkan driver) does not open the file for it.
bool newest_wanted() {
   static std::once_flag once;
   static bool on=true;
   std::call_once(once,[] {
      char text[8192];
      size_t have=0;
      int fd=open("/Library/GPUBundles/nvmtl/nvrm610.conf",O_RDONLY|O_CLOEXEC);
      if (fd>=0) {
         for (ssize_t n;have<sizeof text-1 && (n=read(fd,text+have,sizeof text-1-have))>0;) have+=static_cast<size_t>(n);
         close(fd);
      }
      text[have]=0;
      on=nvkbridge::settings_switch_on(text,"NVK_BRIDGE_PRESENT_NEWEST");
      if (!on) fprintf(stderr,"NVK Bridge: NVK_BRIDGE_PRESENT_NEWEST=0: IMMEDIATE swapchains present synchronously\n");
   });
   return on;
}
// A swapchain's presentation thread. It holds the swapchain, and with it the device and the layer, until it ends:
// when the swapchain is retired, at the latest after the layer's own timeout (1 s) if it was waiting for a drawable.
// It may so still be inside the layer's nextDrawable while vkCreateSwapchainKHR sets the layer up for the swapchain
// that replaces this one; the drawable it then gets is given back unpresented. Each round has its own autorelease
// pool: a drawable the layer handed out goes back to it when the pool is drained, and the layer has two or three
// (maximumDrawableCount).
void *newest_main(void *arg) {
   std::shared_ptr<Chain> c;
   { std::unique_ptr<std::shared_ptr<Chain>> given(static_cast<std::shared_ptr<Chain> *>(arg));c=*given; }
   pthread_setname_np("NVK Bridge presentation");
   for (bool more=true;more;) {
      @autoreleasepool {
         // Nothing may leave this thread: nobody would catch it. catch (...) takes an Objective-C exception too.
         try { more=c->newest->step(); }
         catch (...) {
            fprintf(stderr,"NVK Bridge: the presentation thread ended on an exception; this swapchain presents synchronously\n");
            c->newest->abandon();more=false;
         }
      }
   }
   return nullptr;
}
bool start_presentation(const std::shared_ptr<Chain> &c) {
   static std::once_flag once;
   static bool pinned;
   std::call_once(once,[] {
      pthread_atfork(nullptr,nullptr,[] { forked=true; });
      // The thread runs this library's code whatever the application does with the library afterwards. A library
      // that cannot be pinned gets no thread (as zink/appkit.m does with its observers).
      Dl_info self{};
      pinned=dladdr(reinterpret_cast<void *>(&vkGetInstanceProcAddr),&self) && self.dli_fname &&
             dlopen(self.dli_fname,RTLD_NOW|RTLD_LOCAL|RTLD_NODELETE);
   });
   if (!pinned) return false;
   pthread_attr_t attr;
   if (pthread_attr_init(&attr)) return false;
   pthread_attr_setdetachstate(&attr,PTHREAD_CREATE_DETACHED);
   // The frame is on its way to the screen: this thread's work is what the user is looking at.
   pthread_attr_set_qos_class_np(&attr,QOS_CLASS_USER_INTERACTIVE,0);
   // The thread belongs to no runtime of the application (a JVM's signal handlers are not written for it), so it
   // starts with every signal blocked that is sent to a process, and the calling thread's mask is put back. The
   // signals of a fault stay open: blocked, a fault on this thread would fault again for ever instead of ending the
   // process. As the Zink frontend's presentation thread (zink/nvk_present_queue.c).
   sigset_t all,saved;
   sigfillset(&all);
   sigdelset(&all,SIGSEGV);sigdelset(&all,SIGBUS);sigdelset(&all,SIGILL);
   sigdelset(&all,SIGFPE);sigdelset(&all,SIGTRAP);sigdelset(&all,SIGSYS);
   bool started=false;
   auto *given=new (std::nothrow) std::shared_ptr<Chain>(c);
   if (given && pthread_sigmask(SIG_SETMASK,&all,&saved)==0) {
      pthread_t thread;
      started=pthread_create(&thread,&attr,newest_main,given)==0;
      pthread_sigmask(SIG_SETMASK,&saved,nullptr);
   }
   if (!started) delete given;
   pthread_attr_destroy(&attr);
   return started;
}
VkResult VKAPI_CALL create_swapchain(VkDevice device,const VkSwapchainCreateInfoKHR *info,const VkAllocationCallbacks *alloc,VkSwapchainKHR *out) {
   auto d=find(devices,device); auto s=find(surfaces,info->surface);
   if (!d || !d->swapchain || !s) return VK_ERROR_SURFACE_LOST_KHR;
   if (d->uncertain) return VK_ERROR_DEVICE_LOST;
   if (!valid_format(info->imageFormat) || info->imageColorSpace!=VK_COLOR_SPACE_SRGB_NONLINEAR_KHR) return VK_ERROR_FORMAT_NOT_SUPPORTED;
   if (info->flags || info->imageArrayLayers!=1 || info->minImageCount<2 || info->minImageCount>8 ||
       !info->imageExtent.width || !info->imageExtent.height ||
       info->imageExtent.width>d->properties.limits.maxImageDimension2D || info->imageExtent.height>d->properties.limits.maxImageDimension2D ||
       info->preTransform!=VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR ||
       (info->presentMode!=VK_PRESENT_MODE_FIFO_KHR && info->presentMode!=VK_PRESENT_MODE_IMMEDIATE_KHR)) return VK_ERROR_INITIALIZATION_FAILED;
   VkSurfaceCapabilitiesKHR caps{}; surface_caps(d->physical,info->surface,&caps);
   if ((info->imageUsage&~caps.supportedUsageFlags) || !(info->compositeAlpha&caps.supportedCompositeAlpha)) return VK_ERROR_FEATURE_NOT_PRESENT;
   // The swapchain this one replaces presents nothing more, before the layer changes under it: a frame of the old
   // size must not reach the layer after one of the new. Before the submission lock is taken, because its
   // presentation thread needs the lock to finish the copy it is in.
   auto old=find(chains,info->oldSwapchain);
   if (old && old->newest) old->newest->retire();
   std::lock_guard<std::recursive_mutex> submit(d->submit);
   if(d->uncertain)return VK_ERROR_DEVICE_LOST;
   if (s->layer.device && s->layer.device.registryID!=d->metal.registryID) return VK_ERROR_SURFACE_LOST_KHR;
   s->layer.device=d->metal; s->layer.pixelFormat=info->imageFormat==VK_FORMAT_B8G8R8A8_SRGB?MTLPixelFormatBGRA8Unorm_sRGB:MTLPixelFormatBGRA8Unorm;
   s->layer.framebufferOnly=NO; s->layer.allowsNextDrawableTimeout=YES;
   s->layer.maximumDrawableCount=std::min(3u,info->minImageCount);
   s->layer.drawableSize=CGSizeMake(info->imageExtent.width,info->imageExtent.height);
   s->layer.displaySyncEnabled=info->presentMode!=VK_PRESENT_MODE_IMMEDIATE_KHR;
   s->layer.opaque=info->compositeAlpha==VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR;
   auto c=std::make_shared<Chain>(); c->dev=d;c->surface=s;c->format=info->imageFormat;c->extent=info->imageExtent;c->present_mode=info->presentMode;
   if (alloc) { c->allocator=*alloc; c->custom_allocator=true; }
   c->images.resize(info->minImageCount);
   std::vector<uint32_t> families;
   if (info->imageSharingMode==VK_SHARING_MODE_CONCURRENT && info->queueFamilyIndexCount)
      families.assign(info->pQueueFamilyIndices,info->pQueueFamilyIndices+info->queueFamilyIndexCount);
   if (families.empty()) {
      uint32_t n=0; IP(d->instance,GetPhysicalDeviceQueueFamilyProperties)(d->physical,&n,nullptr);
      for (uint32_t j=0;j<n;j++) families.push_back(j);
   }
   if (std::find(families.begin(),families.end(),d->copy_family)==families.end()) families.push_back(d->copy_family);
   std::sort(families.begin(),families.end()); families.erase(std::unique(families.begin(),families.end()),families.end());
   VkImageCreateInfo image{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
   image.imageType=VK_IMAGE_TYPE_2D;image.format=c->format;image.extent={c->extent.width,c->extent.height,1};
   image.mipLevels=1;image.arrayLayers=1;image.samples=VK_SAMPLE_COUNT_1_BIT;image.tiling=VK_IMAGE_TILING_OPTIMAL;
   image.usage=info->imageUsage|VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
   image.sharingMode=families.size()>1?VK_SHARING_MODE_CONCURRENT:VK_SHARING_MODE_EXCLUSIVE;
   image.queueFamilyIndexCount=families.size()>1?(uint32_t)families.size():0;image.pQueueFamilyIndices=families.data();
   for (auto &im:c->images) {
      VkResult r=DP(d,CreateImage)(device,&image,c->alloc(),&im.raw); if (r!=VK_SUCCESS) { release_images(c);return r; }
      VkMemoryRequirements req{};DP(d,GetImageMemoryRequirements)(device,im.raw,&req);
      int mt=memory_type(*d,req.memoryTypeBits,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
      if (mt<0) { release_images(c);return VK_ERROR_OUT_OF_DEVICE_MEMORY; }
      VkMemoryAllocateInfo allocation{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};allocation.allocationSize=req.size;allocation.memoryTypeIndex=mt;
      r=DP(d,AllocateMemory)(device,&allocation,c->alloc(),&im.memory);
      if (r==VK_SUCCESS) r=DP(d,BindImageMemory)(device,im.raw,im.memory,0);
      if (r!=VK_SUCCESS) { release_images(c);return r; }
   }
   // IMMEDIATE shows the newest frame, on a thread of the swapchain. Only with a copy queue of this frontend's own:
   // that thread submits to it, and an application's queue is the application's to synchronize.
   if (info->presentMode==VK_PRESENT_MODE_IMMEDIATE_KHR && d->private_copy_queue && !forked && newest_wanted()) {
      VkFenceCreateInfo signalled{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};signalled.flags=VK_FENCE_CREATE_SIGNALED_BIT;
      for (auto &im:c->images) {
         VkResult r=DP(d,CreateFence)(device,&signalled,c->alloc(),&im.done); if (r!=VK_SUCCESS) { release_images(c);return r; }
      }
      c->newest=std::make_unique<nvkbridge::Newest<NewestBackend>>(NewestBackend{c.get()},(uint32_t)c->images.size());
   }
   c->handle=(VkSwapchainKHR)(uintptr_t)next_handle.fetch_add(1);*out=c->handle;
   { std::lock_guard<std::mutex> lock(registry);chains[c->handle]=c; }
   // The thread starts last, when nothing after it can fail: a thread holds its swapchain, and a swapchain the
   // application was never given could not be retired.
   if (c->newest && !start_presentation(c)) {
      c->newest.reset();
      for (auto &im:c->images) { DP(d,DestroyFence)(device,im.done,c->alloc());im.done=VK_NULL_HANDLE; }
      fprintf(stderr,"NVK Bridge: no presentation thread; this swapchain presents synchronously\n");
   }
   if (old) { std::lock_guard<std::mutex> lock(old->mutex);old->retired=true;old->available.notify_all(); }
   fprintf(stderr,"NVK Bridge: swapchain %ux%u, %u Vulkan images, %lu Metal slots\n",c->extent.width,c->extent.height,info->minImageCount,(unsigned long)s->layer.maximumDrawableCount);
   fprintf(stderr,"NVK Bridge: swapchain presentation=%s\n",c->newest?"newest frame":"synchronous");
   return VK_SUCCESS;
}
void VKAPI_CALL destroy_swapchain(VkDevice device,VkSwapchainKHR swapchain,const VkAllocationCallbacks *) {
   auto c=find(chains,swapchain);if (!c || c->dev->raw!=device) return;
   { std::lock_guard<std::mutex> lock(c->mutex);c->retired=true;c->available.notify_all(); }
   if (c->newest) {
      // Before the submission lock is taken: the presentation thread needs it to finish the copy it is in.
      c->newest->retire();
      // Frames that were presented and never copied: the application's rendering of them ends before their images go.
      auto d=c->dev;
      for (uint32_t k:c->newest->pending()) {
         if (d->uncertain) break;
         VkResult r=wait_fence_whole(d.get(),c->images[k].done);
         if (r!=VK_SUCCESS) NewestBackend{c.get()}.set_uncertain();
      }
   }
   std::lock_guard<std::recursive_mutex> submit(c->dev->submit);
   if (c->dev->uncertain) return;
   release_images(c);
   { std::lock_guard<std::mutex> lock(registry);chains.erase(swapchain); }
   fprintf(stderr,"NVK Bridge: gpu_copy_count=%llu gpu_copy_bytes=%llu cpu_pixel_readback_bytes=0 metal_cpu_upload_bytes=0 "
      "newest_frames_presented=%llu newest_frames_shown=%llu newest_frames_dropped=%llu\n",
      (unsigned long long)copies.load(),(unsigned long long)copy_bytes.load(),(unsigned long long)newest_presented.load(),
      (unsigned long long)newest_shown.load(),(unsigned long long)newest_dropped.load());
}
VkResult VKAPI_CALL swapchain_images(VkDevice device,VkSwapchainKHR swapchain,uint32_t *count,VkImage *out) {
   auto c=find(chains,swapchain);if (!c || c->dev->raw!=device) return VK_ERROR_OUT_OF_DATE_KHR;
   std::vector<VkImage> images;for (auto &im:c->images) images.push_back(im.raw);return enumerate(images,count,out);
}
VkResult VKAPI_CALL acquire_image(VkDevice device,VkSwapchainKHR swapchain,uint64_t timeout,VkSemaphore semaphore,VkFence fence,uint32_t *index) {
   auto c=find(chains,swapchain);if (!c || c->dev->raw!=device) return VK_ERROR_OUT_OF_DATE_KHR;
   if (!semaphore && !fence) return VK_ERROR_INITIALIZATION_FAILED;
   auto d=c->dev;if (d->uncertain) return VK_ERROR_DEVICE_LOST;
   if (c->newest) {
      // It waits for the GPU to finish a frame that was overtaken, or for the presentation thread when that has
      // every image the application does not hold; with one image acquired at a time, never for a drawable. Before
      // the submission lock is taken below: the thread needs it to finish a copy. The image it hands out is free of
      // the GPU and of the thread, so the semaphore and fence below are true at once.
      switch (c->newest->acquire(timeout,index)) {
         case nvkbridge::Acquire::Got: break;
         case nvkbridge::Acquire::NotReady: return VK_NOT_READY;
         case nvkbridge::Acquire::Timeout: return VK_TIMEOUT;
         case nvkbridge::Acquire::Retired: return VK_ERROR_OUT_OF_DATE_KHR;
         case nvkbridge::Acquire::Lost: return VK_ERROR_DEVICE_LOST;
      }
   } else {
      std::unique_lock<std::mutex> lock(c->mutex);
      auto ready=[&] { return c->retired || d->uncertain || std::any_of(c->images.begin(),c->images.end(),[](auto &im){return !im.acquired;}); };
      if (!ready()) {
         if (!timeout) return VK_NOT_READY;
         if (timeout==UINT64_MAX) c->available.wait(lock,ready);
         else if (!c->available.wait_for(lock,std::chrono::nanoseconds(std::min(timeout,(uint64_t)INT64_MAX)),ready)) return VK_TIMEOUT;
      }
      if (c->retired) return VK_ERROR_OUT_OF_DATE_KHR;
      if (d->uncertain) return VK_ERROR_DEVICE_LOST;
      for (uint32_t j=0;j<c->images.size();j++) {
         uint32_t k=(c->cursor+j)%(uint32_t)c->images.size();
         if (!c->images[k].acquired) { c->images[k].acquired=true;*index=k;c->cursor=(k+1)%(uint32_t)c->images.size();break; }
      }
   }
   VkSubmitInfo signal{VK_STRUCTURE_TYPE_SUBMIT_INFO};signal.signalSemaphoreCount=semaphore?1:0;signal.pSignalSemaphores=&semaphore;
   VkResult r;
   { std::lock_guard<std::recursive_mutex> submit(d->submit);r=d->uncertain?VK_ERROR_DEVICE_LOST:DP(d,QueueSubmit)(d->copy_queue,1,&signal,fence); }
   if (r!=VK_SUCCESS) {
      if (c->newest) c->newest->unacquire(*index);
      else { std::lock_guard<std::mutex> rollback(c->mutex);c->images[*index].acquired=false;c->available.notify_all(); }
   }
   return r;
}
VkResult VKAPI_CALL acquire_image2(VkDevice device,const VkAcquireNextImageInfoKHR *info,uint32_t *index) {
   if (info->deviceMask!=1) return VK_ERROR_FEATURE_NOT_PRESENT;
   return acquire_image(device,info->swapchain,info->timeout,info->semaphore,info->fence,index);
}

struct Frame {
   std::shared_ptr<Device> dev;
   // Set only when the frame is quarantined (its GPU completion is uncertain),
   // so the swapchain's images outlive the copy that may still read them.
   std::shared_ptr<Chain> chain;
   id<CAMetalDrawable> __strong drawable;
   IOSurfaceRef surface{};
   VkBuffer buffer{};
   VkDeviceMemory memory{};
   VkCommandPool pool{};
   VkCommandBuffer commands{};
   VkFence fence{};
   uint64_t bytes{};
   size_t row{}, allocation_size{};
   void *host_pointer{};
   bool host_locked{};
   ~Frame() {
      auto d=dev;
      if (d->uncertain) return;
      if (pool) DP(d,DestroyCommandPool)(d->raw,pool,nullptr);
      if (fence) DP(d,DestroyFence)(d->raw,fence,nullptr);
      if (buffer) DP(d,DestroyBuffer)(d->raw,buffer,nullptr);
      if (memory) DP(d,FreeMemory)(d->raw,memory,nullptr);
      if (host_locked) IOSurfaceUnlock(surface,0,nullptr);
      if (surface) CFRelease(surface);
   }
};
// Never destroyed: a presentation thread may add to it while the process exits.
auto &quarantine=*new std::vector<std::shared_ptr<Frame>>;

VkResult import_host_frame(const std::shared_ptr<Frame> &frame,VkBufferCreateInfo buffer) {
   auto d=frame->dev;
   const size_t alignment=d->host_present_alignment;
   auto host_properties=DP(d,GetMemoryHostPointerPropertiesEXT);
   if(!alignment || !host_properties)return VK_ERROR_INVALID_EXTERNAL_HANDLE;
   if(IOSurfaceLock(frame->surface,0,nullptr)!=kIOReturnSuccess)return VK_ERROR_INVALID_EXTERNAL_HANDLE;
   frame->host_locked=true;
   void *base=IOSurfaceGetBaseAddress(frame->surface);
   if(!base || reinterpret_cast<uintptr_t>(base)%alignment)return VK_ERROR_INVALID_EXTERNAL_HANDLE;
   DP(d,DestroyBuffer)(d->raw,frame->buffer,nullptr);frame->buffer=VK_NULL_HANDLE;
   VkExternalMemoryBufferCreateInfo external{VK_STRUCTURE_TYPE_EXTERNAL_MEMORY_BUFFER_CREATE_INFO};
   constexpr auto handle_type=VK_EXTERNAL_MEMORY_HANDLE_TYPE_HOST_ALLOCATION_BIT_EXT;
   external.handleTypes=handle_type;buffer.pNext=&external;
   VkResult r=DP(d,CreateBuffer)(d->raw,&buffer,nullptr,&frame->buffer);if(r!=VK_SUCCESS)return r;
   VkMemoryRequirements requirements{};DP(d,GetBufferMemoryRequirements)(d->raw,frame->buffer,&requirements);
   if(requirements.size>SIZE_MAX-(alignment-1))return VK_ERROR_OUT_OF_DEVICE_MEMORY;
   const size_t bytes=(requirements.size+alignment-1)&~(alignment-1);
   if(bytes>frame->allocation_size)return VK_ERROR_OUT_OF_DEVICE_MEMORY;
   VkMemoryHostPointerPropertiesEXT properties{VK_STRUCTURE_TYPE_MEMORY_HOST_POINTER_PROPERTIES_EXT};
   r=host_properties(d->raw,handle_type,base,&properties);if(r!=VK_SUCCESS)return r;
   int type=memory_type(*d,requirements.memoryTypeBits&properties.memoryTypeBits,
      VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
   if(type<0)return VK_ERROR_INVALID_EXTERNAL_HANDLE;
   VkImportMemoryHostPointerInfoEXT host{VK_STRUCTURE_TYPE_IMPORT_MEMORY_HOST_POINTER_INFO_EXT};
   host.handleType=handle_type;host.pHostPointer=base;
   VkMemoryAllocateInfo allocation{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
   allocation.pNext=&host;allocation.allocationSize=bytes;allocation.memoryTypeIndex=type;
   r=DP(d,AllocateMemory)(d->raw,&allocation,nullptr,&frame->memory);
   if(r==VK_SUCCESS)frame->host_pointer=base;
   return r;
}

void publish_tag(IOSurfaceRef surface) {
   struct Tag { uint32_t magic,seed,generation; } tag{0x564d5634u,IOSurfaceGetSeed(surface),0};
   CFTypeRef old=IOSurfaceCopyValue(surface,CFSTR("NVMTLVramAt:p0"));
   if (old && CFGetTypeID(old)==CFDataGetTypeID() && CFDataGetLength((CFDataRef)old)==sizeof tag) {
      Tag prior{};memcpy(&prior,CFDataGetBytePtr((CFDataRef)old),sizeof prior);
      if (prior.magic==tag.magic) tag.generation=prior.generation+1;
   }
   if (old) CFRelease(old);
   CFDataRef data=CFDataCreate(nullptr,(const UInt8 *)&tag,sizeof tag);
   if (data) { IOSurfaceSetValue(surface,CFSTR("NVMTLVramAt:p0"),data);CFRelease(data); }
}
// The copy of one presented image into a drawable of the layer, its publication and its present.
// threaded false: the synchronous path, on the application's thread inside vkQueuePresentKHR, which holds the
// device's submission lock for the whole call; the lock is taken here again from the first native object on, as
// before 3.0.9.
// threaded true: a swapchain's presentation thread. It takes the lock for the copy's submission alone: the import of
// the drawable's video memory and the mark are calls into the kernel, and the application's own vkQueueSubmit does
// not wait behind them. The frame's rendering has finished (its `done` fence), so there is no handoff to wait for.
VkResult copy_drawable(const std::shared_ptr<Chain> &c,uint32_t index,id<CAMetalDrawable> drawable,VkSemaphore handoff,bool &consumed,bool threaded) {
   auto d=c->dev;
   id<MTLTexture> texture=drawable.texture;
   if (texture.device.registryID!=d->metal.registryID || texture.iosurfacePlane!=0 || !texture.iosurface)
      return VK_ERROR_SURFACE_LOST_KHR;
   // A valid layer can resize before its old swapchain is replaced. Let the
   // application recreate that chain rather than reporting a lost window.
   if (texture.width!=c->extent.width || texture.height!=c->extent.height)
      return VK_ERROR_OUT_OF_DATE_KHR;
   MTLPixelFormat expected=c->format==VK_FORMAT_B8G8R8A8_SRGB?MTLPixelFormatBGRA8Unorm_sRGB:MTLPixelFormatBGRA8Unorm;
   if (texture.pixelFormat!=expected) return VK_ERROR_FORMAT_NOT_SUPPORTED;
   IOSurfaceRef surface=texture.iosurface;
   size_t row=IOSurfaceGetBytesPerRow(surface),size=IOSurfaceGetAllocSize(surface);
   if (IOSurfaceGetPlaneCount(surface)>1 || row%4 || row<(uint64_t)c->extent.width*4 ||
       c->extent.height>SIZE_MAX/row || (uint64_t)row*c->extent.height>size) return VK_ERROR_FORMAT_NOT_SUPPORTED;
   std::unique_lock<std::recursive_mutex> backend_lock(d->submit,std::defer_lock);
   if(!threaded)backend_lock.lock();
   if(d->uncertain)return VK_ERROR_DEVICE_LOST;
   // Every frame imports the drawable's IOSurface on its own; the frame and
   // its native objects are released when this function returns, unless the
   // GPU completion is uncertain (then they are quarantined below).
   auto frame=std::make_shared<Frame>();frame->dev=d;
   frame->surface=(IOSurfaceRef)CFRetain(surface);frame->row=row;frame->allocation_size=size;
   frame->bytes=(uint64_t)row*c->extent.height;
   VkBufferCreateInfo buffer{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};buffer.size=frame->bytes;buffer.usage=VK_BUFFER_USAGE_TRANSFER_DST_BIT|VK_BUFFER_USAGE_TRANSFER_SRC_BIT;buffer.sharingMode=VK_SHARING_MODE_EXCLUSIVE;
   VkResult r=DP(d,CreateBuffer)(d->raw,&buffer,nullptr,&frame->buffer);if (r!=VK_SUCCESS) return r;
   VkMemoryRequirements req{};DP(d,GetBufferMemoryRequirements)(d->raw,frame->buffer,&req);
   if (req.size>size) return VK_ERROR_OUT_OF_DEVICE_MEMORY;
   int mt=memory_type(*d,req.memoryTypeBits,VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);if (mt<0) return VK_ERROR_OUT_OF_DEVICE_MEMORY;
   // The driver's IOSurface import (NVMTL_NVK_SHARE_VERSION 3): the record
   // staged on this thread names the surface and plane, and the descriptor
   // only carries the opaque-fd import, as in the Metal plugin.
   VkImportMemoryFdInfoKHR imported{VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR};
   imported.handleType=VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT;imported.fd=open("/dev/null",O_RDONLY|O_CLOEXEC);
   if (imported.fd<0) return VK_ERROR_OUT_OF_HOST_MEMORY;
   VkMemoryAllocateInfo allocation{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
   allocation.pNext=&imported;allocation.allocationSize=req.size;allocation.memoryTypeIndex=mt;
   Share share{3,IOSurfaceGetID(frame->surface),0,0,req.size};
   stage_import(&share);r=DP(d,AllocateMemory)(d->raw,&allocation,nullptr,&frame->memory);stage_import(nullptr);
   if (r!=VK_SUCCESS) {
      close(imported.fd);import_failures++;
      if(r!=VK_ERROR_INVALID_EXTERNAL_HANDLE || !d->host_present_alignment)return r;
      r=import_host_frame(frame,buffer);if(r!=VK_SUCCESS)return r;
   }
   r=DP(d,BindBufferMemory)(d->raw,frame->buffer,frame->memory,0);if (r!=VK_SUCCESS) return r;
   VkCommandPoolCreateInfo pool{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};pool.queueFamilyIndex=d->copy_family;
   r=DP(d,CreateCommandPool)(d->raw,&pool,nullptr,&frame->pool);if (r!=VK_SUCCESS) return r;
   VkCommandBufferAllocateInfo cb{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};cb.commandPool=frame->pool;cb.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY;cb.commandBufferCount=1;
   r=DP(d,AllocateCommandBuffers)(d->raw,&cb,&frame->commands);if (r!=VK_SUCCESS) return r;
   VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};r=DP(d,CreateFence)(d->raw,&fi,nullptr,&frame->fence);if (r!=VK_SUCCESS) return r;
   frame->drawable=drawable;
   VkCommandBufferBeginInfo begin{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};begin.flags=VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
   r=DP(d,BeginCommandBuffer)(frame->commands,&begin);if (r!=VK_SUCCESS) return r;
   VkImageMemoryBarrier barrier{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
   barrier.srcAccessMask=VK_ACCESS_MEMORY_WRITE_BIT;barrier.dstAccessMask=VK_ACCESS_TRANSFER_READ_BIT;
   barrier.oldLayout=VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;barrier.newLayout=VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
   barrier.srcQueueFamilyIndex=barrier.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED;
   barrier.image=c->images[index].raw;barrier.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,1,0,1};
   DP(d,CmdPipelineBarrier)(frame->commands,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,0,nullptr,0,nullptr,1,&barrier);
   VkBufferImageCopy region{};region.bufferRowLength=(uint32_t)(row/4);region.bufferImageHeight=c->extent.height;
   region.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,0,0,1};region.imageExtent={c->extent.width,c->extent.height,1};
   DP(d,CmdCopyImageToBuffer)(frame->commands,c->images[index].raw,VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,frame->buffer,1,&region);
   if(frame->host_locked) {
      VkMemoryBarrier host{VK_STRUCTURE_TYPE_MEMORY_BARRIER};
      host.srcAccessMask=VK_ACCESS_TRANSFER_WRITE_BIT;host.dstAccessMask=VK_ACCESS_HOST_READ_BIT;
      DP(d,CmdPipelineBarrier)(frame->commands,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,1,&host,0,nullptr,0,nullptr);
   }
   barrier.srcAccessMask=VK_ACCESS_TRANSFER_READ_BIT;barrier.dstAccessMask=0;
   barrier.oldLayout=VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;barrier.newLayout=VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
   DP(d,CmdPipelineBarrier)(frame->commands,VK_PIPELINE_STAGE_TRANSFER_BIT,VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT,0,0,nullptr,0,nullptr,1,&barrier);
   r=DP(d,EndCommandBuffer)(frame->commands);if (r!=VK_SUCCESS) return r;
   VkPipelineStageFlags transfer=VK_PIPELINE_STAGE_TRANSFER_BIT;
   VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};submit.waitSemaphoreCount=handoff?1:0;submit.pWaitSemaphores=&handoff;submit.pWaitDstStageMask=&transfer;
   submit.commandBufferCount=1;submit.pCommandBuffers=&frame->commands;
   if(threaded) { backend_lock.lock();if(d->uncertain)return VK_ERROR_DEVICE_LOST; }
   r=DP(d,QueueSubmit)(d->copy_queue,1,&submit,frame->fence);
   if (r!=VK_SUCCESS) return r;
   if (handoff) consumed=true;
   // On the synchronous path queue_present holds the outer recursive lock for
   // the whole call, so this unlock keeps the device's submissions ordered
   // behind the presentation. A presentation thread lets the lock go here.
   backend_lock.unlock();
   auto start=std::chrono::steady_clock::now();
   r=threaded?wait_fence_whole(d.get(),frame->fence):DP(d,WaitForFences)(d->raw,1,&frame->fence,VK_TRUE,5000000000ull);
   if(!threaded)backend_lock.lock();
   producer_ns+=elapsed(start);
   if (r!=VK_SUCCESS) {
      d->uncertain=true;
      frame->chain=c;
      { std::lock_guard<std::mutex> lock(registry);quarantine.push_back(frame); }
      fprintf(stderr,"NVK Bridge: presentation completion uncertain (%d); resources quarantined\n",r);
      return VK_ERROR_DEVICE_LOST;
   }
   // The observer is called on the thread that presents, with the submission lock held: a present made while one is
   // set is synchronous (queue_present).
   if (!threaded) if (auto observer=present_observer.load()) observer(backend_gdpa,d->raw,d->copy_queue,frame->buffer,(uint32_t)row,c->extent.width,c->extent.height,d->copy_family);
   // A fence also signals when its channel was rebuilt after a fault and the copy never ran: the drawable then holds
   // what it held before, and is not published. Asked on the presentation thread only; the synchronous path is
   // 3.0.8's.
   if (threaded && fence_dropped && fence_dropped(d->raw,frame->fence)==1) return VK_ERROR_UNKNOWN;
   // Publication. The GPU copy has completed (the fence above), so the VRAM is
   // marked newest for plane 0 (a blocking mark, as the plugin's own
   // nvmtl_vk_surface_dirty), and the generation tag is written after the mark
   // with the seed the mark left: the Metal plugin in WindowServer then takes
   // the VRAM as current and does not copy system memory over it.
   if(frame->host_locked) {
      if(IOSurfaceUnlock(frame->surface,0,nullptr)!=kIOReturnSuccess)return VK_ERROR_SURFACE_LOST_KHR;
      frame->host_locked=false;
   } else {
      if (surface_dirty(d->raw,IOSurfaceGetID(frame->surface),0)) return VK_ERROR_SURFACE_LOST_KHR;
      publish_tag(frame->surface);
   }
   [frame->drawable present];
   frame->drawable=nil;
   copies++;copy_bytes+=frame->bytes;
   return VK_SUCCESS;
}
// The synchronous path: the layer's next drawable on the application's thread, which waits for it.
VkResult copy_frame(const std::shared_ptr<Chain> &c,uint32_t index,VkSemaphore handoff,bool &consumed) {
   auto start=std::chrono::steady_clock::now();
   id<CAMetalDrawable> drawable=[c->surface->layer nextDrawable];
   drawable_ns+=elapsed(start);
   if (!drawable) return VK_ERROR_OUT_OF_DATE_KHR;
   VkResult r=copy_drawable(c,index,drawable,handoff,consumed,false);
   // A frame reached the layer: a swapchain that shows the newest frame hands its next one to its thread again.
   if (r==VK_SUCCESS) c->surface->held=false;
   return r;
}

bool NewestBackend::uncertain() const { return chain->dev->uncertain.load(); }
void NewestBackend::set_uncertain() {
   if (!chain->dev->uncertain.exchange(true))
      fprintf(stderr,"NVK Bridge: presentation completion uncertain (a presented frame did not finish rendering); resources retained\n");
}
void NewestBackend::hold() { chain->surface->held=true; }
nvkbridge::Wait NewestBackend::wait_done(uint32_t k,uint64_t ns) {
   Device *d=chain->dev.get();
   VkFence done=chain->images[k].done;
   VkResult r=ns?DP(d,WaitForFences)(d->raw,1,&done,VK_TRUE,ns):DP(d,GetFenceStatus)(d->raw,done);
   if (r==VK_SUCCESS) return nvkbridge::Wait::Signalled;
   return r==VK_TIMEOUT || r==VK_NOT_READY?nvkbridge::Wait::Pending:nvkbridge::Wait::Failed;
}
id<CAMetalDrawable> NewestBackend::next_drawable() {
   auto start=std::chrono::steady_clock::now();
   id<CAMetalDrawable> drawable=[chain->surface->layer nextDrawable];
   newest_drawable_ns+=elapsed(start);
   return drawable;
}
nvkbridge::Copy NewestBackend::copy(uint32_t k,id<CAMetalDrawable> drawable) {
   // The presentation thread holds the swapchain while it runs (newest_main).
   auto c=chain->shared_from_this();
   // The frame's fence also signals when a channel rebuild dropped the submission behind it. The image may then
   // hold an older or a partly drawn frame, so it is not shown. Nothing goes on drawing into it: a rebuild drops or
   // replays what its channel had, and the channel of a waiter is rebuilt only when the one it waits on is idle
   // (nvk/nvk-channel-recover.patch), so the image is the application's to acquire again.
   if (fence_dropped && fence_dropped(c->dev->raw,c->images[k].done)==1) {
      newest_failed++;
      fprintf(stderr,"NVK Bridge: a channel rebuild dropped a presented frame; the next present is synchronous\n");
      return nvkbridge::Copy::Failed;
   }
   bool consumed=false;
   VkResult r=copy_drawable(c,k,drawable,VK_NULL_HANDLE,consumed,true);
   if (c->dev->uncertain) return nvkbridge::Copy::Uncertain;
   if (r==VK_SUCCESS) { newest_shown++;return nvkbridge::Copy::Shown; }
   // The copy did not start, or it completed and its publication was refused: the image has no reader left. The
   // next present is synchronous and answers what is wrong (a layer of another size: VK_ERROR_OUT_OF_DATE_KHR).
   newest_failed++;
   fprintf(stderr,"NVK Bridge: the presentation thread could not show a frame (%d); the next present is synchronous\n",r);
   return nvkbridge::Copy::Failed;
}

VkResult VKAPI_CALL queue_present(VkQueue queue,const VkPresentInfoKHR *info) {
   auto q=find(queues,queue);auto d=q?q->dev.lock():nullptr;if (!d || !d->swapchain || d->uncertain) return VK_ERROR_DEVICE_LOST;
   // The swapchains that show the newest frame, before the submission lock is taken: a presentation thread needs
   // that lock to finish a copy, so nobody waits for one while holding it. A present of one such swapchain alone is
   // handed to its thread. Any other present of one is synchronous, below, and first gives up the frame its thread
   // has not taken and waits for the copy it is in, so that no older frame reaches the layer after this one: a
   // present observer is set (it is called on the presenting thread), several swapchains are presented at once, the
   // layer gave that thread nothing or something it could not use (Surface::held), the thread is gone, the
   // swapchain is retired, or the image is not the application's.
   std::shared_ptr<Chain> handed;
   for (uint32_t j=0;j<info->swapchainCount;j++) {
      auto c=find(chains,info->pSwapchains[j]);
      if (!c || c->dev!=d || !c->newest) continue;
      if (info->swapchainCount==1 && !present_observer.load() && !c->surface->held &&
          c->newest->accepts(info->pImageIndices[j])) handed=c;
      else c->newest->quiesce();
   }
   std::lock_guard<std::recursive_mutex> lock(d->submit);
   if(d->uncertain)return VK_ERROR_DEVICE_LOST;
   if (handed) {
      // The frame's rendering dependencies are consumed here, on the application's queue, as on the synchronous
      // path; what they signal is the image's `done` fence, which the presentation thread waits for before it
      // copies, and vkAcquireNextImageKHR before it hands out the image of a frame that was overtaken.
      const uint32_t k=info->pImageIndices[0];
      VkFence done=handed->images[k].done;
      VkResult r=DP(d,ResetFences)(d->raw,1,&done);if (r!=VK_SUCCESS) return r;
      std::vector<VkPipelineStageFlags> stages(info->waitSemaphoreCount,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT);
      VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};submit.waitSemaphoreCount=info->waitSemaphoreCount;submit.pWaitSemaphores=info->pWaitSemaphores;
      submit.pWaitDstStageMask=stages.data();
      // A failure leaves the image with the application, as on the synchronous path. Its fence is then reset with
      // nothing submitted: nobody waits for the fence of an image the application holds.
      r=DP(d,QueueSubmit)(queue,1,&submit,done);if (r!=VK_SUCCESS) return r;
      handed->newest->put(k);newest_presented++;
      if (info->pResults) info->pResults[0]=VK_SUCCESS;
      return VK_SUCCESS;
   }
   VkSemaphore handoff{};VkSemaphoreCreateInfo sci{VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO};
   VkResult r=DP(d,CreateSemaphore)(d->raw,&sci,nullptr,&handoff);if (r!=VK_SUCCESS) return r;
   std::vector<VkPipelineStageFlags> stages(info->waitSemaphoreCount,VK_PIPELINE_STAGE_ALL_COMMANDS_BIT);
   VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};submit.waitSemaphoreCount=info->waitSemaphoreCount;submit.pWaitSemaphores=info->pWaitSemaphores;
   submit.pWaitDstStageMask=stages.data();submit.signalSemaphoreCount=1;submit.pSignalSemaphores=&handoff;
   r=DP(d,QueueSubmit)(queue,1,&submit,VK_NULL_HANDLE);if (r!=VK_SUCCESS) { DP(d,DestroySemaphore)(d->raw,handoff,nullptr);return r; }
   VkResult overall=VK_SUCCESS;bool consumed=false;
   for (uint32_t j=0;j<info->swapchainCount;j++) {
      auto c=find(chains,info->pSwapchains[j]);VkResult result=VK_ERROR_OUT_OF_DATE_KHR;uint32_t k=info->pImageIndices[j];
      if (c && c->dev==d && k<c->images.size()) {
         bool valid;
         if (c->newest) valid=c->newest->app_owns(k);
         else { std::lock_guard<std::mutex> guard(c->mutex);valid=c->images[k].acquired && !c->retired; }
         if (valid) {
            @autoreleasepool { result=copy_frame(c,k,consumed?VK_NULL_HANDLE:handoff,consumed); }
         }
         if (!d->uncertain) {
            if (c->newest) c->newest->release(k);
            else { std::lock_guard<std::mutex> guard(c->mutex);c->images[k].acquired=false;c->available.notify_all(); }
         }
      }
      if (info->pResults) info->pResults[j]=result;
      if (result!=VK_SUCCESS) overall=result;
      if (d->uncertain) return VK_ERROR_DEVICE_LOST;
   }
   // Even an out-of-date drawable must consume and complete the caller's
   // rendering dependencies before its source image or semaphore is released.
   if (!consumed) {
      VkPipelineStageFlags all=VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;VkSubmitInfo drain{VK_STRUCTURE_TYPE_SUBMIT_INFO};
      drain.waitSemaphoreCount=1;drain.pWaitSemaphores=&handoff;drain.pWaitDstStageMask=&all;
      r=DP(d,QueueSubmit)(d->copy_queue,1,&drain,VK_NULL_HANDLE);
      if (r==VK_SUCCESS) r=DP(d,QueueWaitIdle)(d->copy_queue);
      if (r!=VK_SUCCESS) { d->uncertain=true;return VK_ERROR_DEVICE_LOST; }
   }
   DP(d,DestroySemaphore)(d->raw,handoff,nullptr);
   return overall;
}
VkResult VKAPI_CALL group_caps(VkDevice device,VkDeviceGroupPresentCapabilitiesKHR *out) {
   if (!find(devices,device)) return VK_ERROR_DEVICE_LOST;
   memset(out->presentMask,0,sizeof out->presentMask);out->presentMask[0]=1;out->modes=VK_DEVICE_GROUP_PRESENT_MODE_LOCAL_BIT_KHR;return VK_SUCCESS;
}
VkResult VKAPI_CALL group_modes(VkDevice device,VkSurfaceKHR surface,VkDeviceGroupPresentModeFlagsKHR *out) {
   if (!find(devices,device) || !find(surfaces,surface)) return VK_ERROR_SURFACE_LOST_KHR;
   *out=VK_DEVICE_GROUP_PRESENT_MODE_LOCAL_BIT_KHR;return VK_SUCCESS;
}
VkResult VKAPI_CALL present_rects(VkPhysicalDevice physical,VkSurfaceKHR surface,uint32_t *count,VkRect2D *out) {
   VkSurfaceCapabilitiesKHR caps{};VkResult r=surface_caps(physical,surface,&caps);if (r!=VK_SUCCESS) return r;
   return enumerate(std::vector<VkRect2D>{{{0,0},caps.currentExtent}},count,out);
}

template<auto Function,class Signature=decltype(Function)> struct ApiGuard;
template<auto Function,class R,class... Args> struct ApiGuard<Function,R(*)(Args...)> {
   static R call(Args... args) noexcept {
      try { return Function(args...); }
      catch (const std::bad_alloc &) {
         if constexpr (std::is_same_v<R,VkResult>) return VK_ERROR_OUT_OF_HOST_MEMORY;
         else if constexpr (!std::is_void_v<R>) return R{};
      } catch (...) {
         if constexpr (std::is_same_v<R,VkResult>) return VK_ERROR_UNKNOWN;
         else if constexpr (!std::is_void_v<R>) return R{};
      }
   }
};
PFN_vkVoidFunction lookup(const char *name) {
   if (!strcmp(name,"vkGetInstanceProcAddr")) return (PFN_vkVoidFunction)&vkGetInstanceProcAddr;
   if (!strcmp(name,"vkGetDeviceProcAddr")) return (PFN_vkVoidFunction)&vkGetDeviceProcAddr;
   if (!strcmp(name,"vkCreateMetalSurfaceEXT")) return (PFN_vkVoidFunction)&vkCreateMetalSurfaceEXT;
#define MAP(vkname, fn) if (!strcmp(name,"vk" #vkname)) return reinterpret_cast<PFN_vkVoidFunction>(&ApiGuard<fn>::call)
   MAP(CreateInstance,create_instance); MAP(DestroyInstance,destroy_instance); MAP(EnumerateInstanceExtensionProperties,instance_extensions);
   MAP(EnumerateInstanceLayerProperties,instance_layers); MAP(EnumerateInstanceVersion,instance_version);
   MAP(EnumeratePhysicalDevices,enumerate_physical); MAP(EnumerateDeviceExtensionProperties,device_extensions);
   MAP(CreateDevice,create_device); MAP(DestroyDevice,destroy_device); MAP(GetDeviceQueue,get_queue); MAP(GetDeviceQueue2,get_queue2);
   MAP(QueueSubmit,queue_submit); MAP(QueueSubmit2,queue_submit2); MAP(QueueSubmit2KHR,queue_submit2);
   MAP(QueueBindSparse,bind_sparse); MAP(QueueWaitIdle,queue_idle); MAP(DeviceWaitIdle,device_idle);
   MAP(DestroySurfaceKHR,destroy_surface);
   MAP(GetPhysicalDeviceSurfaceSupportKHR,surface_support); MAP(GetPhysicalDeviceSurfaceCapabilitiesKHR,surface_caps);
   MAP(GetPhysicalDeviceSurfaceCapabilities2KHR,surface_caps2); MAP(GetPhysicalDeviceSurfaceFormatsKHR,surface_formats);
   MAP(GetPhysicalDeviceSurfaceFormats2KHR,surface_formats2); MAP(GetPhysicalDeviceSurfacePresentModesKHR,present_modes);
   MAP(CreateSwapchainKHR,create_swapchain); MAP(DestroySwapchainKHR,destroy_swapchain); MAP(GetSwapchainImagesKHR,swapchain_images);
   MAP(AcquireNextImageKHR,acquire_image); MAP(AcquireNextImage2KHR,acquire_image2); MAP(QueuePresentKHR,queue_present);
   MAP(GetDeviceGroupPresentCapabilitiesKHR,group_caps); MAP(GetDeviceGroupSurfacePresentModesKHR,group_modes);
   MAP(GetPhysicalDevicePresentRectanglesKHR,present_rects);
#undef MAP
   return nullptr;
}
}

extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(VkInstance instance,const char *name) {
   if (!name || !initialize()) return nullptr;
   if (!instance && strcmp(name,"vkGetInstanceProcAddr") && strcmp(name,"vkCreateInstance") &&
       strcmp(name,"vkEnumerateInstanceExtensionProperties") && strcmp(name,"vkEnumerateInstanceLayerProperties") &&
       strcmp(name,"vkEnumerateInstanceVersion")) return nullptr;
   if (auto fn=lookup(name)) return fn;
   if (window_system_proc(name)) return nullptr;
   return backend_gipa(instance,name);
}
extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetDeviceProcAddr(VkDevice device,const char *name) {
   auto d=find(devices,device);
   if (!name || !d) return nullptr;
   if (strstr(name,"vkGetPhysicalDevice")==name || strstr(name,"vkEnumerate")==name ||
       !strcmp(name,"vkGetInstanceProcAddr") || !strcmp(name,"vkCreateInstance") || !strcmp(name,"vkDestroyInstance") ||
       !strcmp(name,"vkCreateDevice") || !strcmp(name,"vkCreateMetalSurfaceEXT") || !strcmp(name,"vkDestroySurfaceKHR")) return nullptr;
   if (window_system_proc(name)) return d->swapchain ? lookup(name) : nullptr;
   if (auto fn=lookup(name)) return fn;
   return backend_gdpa(device,name);
}
// Exported by name too, for applications and libraries that look the Metal
// surface entry up in the library itself: the same guarded implementation
// that vkGetInstanceProcAddr returns.
extern "C" VKAPI_ATTR VkResult VKAPI_CALL vkCreateMetalSurfaceEXT(
   VkInstance instance,const VkMetalSurfaceCreateInfoEXT *info,const VkAllocationCallbacks *alloc,VkSurfaceKHR *out) {
   return ApiGuard<create_surface>::call(instance,info,alloc,out);
}
extern "C" void nvkbridgeGetStats(uint64_t out[7]) {
   out[0]=copies;out[1]=copy_bytes;out[2]=drawable_ns;out[3]=producer_ns;out[4]=import_failures;out[5]=0;out[6]=0;
}
// The swapchains that show the newest frame, for the process: frames handed to a presentation thread, frames it
// showed, frames overtaken or given up, frames it could not show, nanoseconds it waited for the layer. count: how
// many of them out[] has room for.
extern "C" void nvkbridgeGetPresentStats(uint64_t *out,uint32_t count) {
   const uint64_t all[5]={newest_presented,newest_shown,newest_dropped,newest_failed,newest_drawable_ns};
   for (uint32_t j=0;out && j<count && j<5;j++) out[j]=all[j];
}
extern "C" void nvkbridgeSetPresentObserver(PresentObserver observer) { present_observer=observer; }
// Loader/ICD interface. The frontend already owns portable surfaces and
// swapchains, so it must negotiate at least version 3: older interface versions
// make the loader keep its own opaque surface wrapper and pass that to the ICD.
extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vk_icdGetInstanceProcAddr(
   VkInstance instance,const char *name) {
   return vkGetInstanceProcAddr(instance,name);
}
extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vk_icdGetPhysicalDeviceProcAddr(
   VkInstance instance,const char *name) {
   if (!instance || !name) return nullptr;
   if (strncmp(name,"vkGetPhysicalDevice",19) &&
       strcmp(name,"vkEnumerateDeviceExtensionProperties") &&
       strcmp(name,"vkEnumerateDeviceLayerProperties")) return nullptr;
   return vkGetInstanceProcAddr(instance,name);
}
extern "C" VKAPI_ATTR VkResult VKAPI_CALL vk_icdNegotiateLoaderICDInterfaceVersion(
   uint32_t *version) {
   if (!version) return VK_ERROR_INITIALIZATION_FAILED;
   if (*version > 5) *version=5;
   if (*version < 3) return VK_ERROR_INCOMPATIBLE_DRIVER;
   return VK_SUCCESS;
}
