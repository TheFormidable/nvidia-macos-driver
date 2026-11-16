/* v301_vk_nvrm.mm: two fixes of release 3.0.1 in NVK's NVRM back end (nvk/nvk-nvrm-fixes.patch), through the native
 * Vulkan runtime's renderer (/Library/GPUBundles/nvmtl/minecraft/lib/libnvk_renderer.dylib, its path the argument),
 * opened by its path as tools/nvk-bridge/window_check.mm's renderer mode opens it, after a Metal device of this driver
 * is made. Cases:
 *   placed       VK_EXT_map_memory_placed is not offered, and the features memoryMapPlaced, memoryMapRangePlaced and
 *                memoryUnmapReserve all read VK_FALSE: the NVRM map ignores the address asked for, so the extension is
 *                withdrawn on this back end (Zink turned on client-pointer maps when it saw it)
 *   map-threads  4 threads at once each make, map, write, read, unmap and free 200 allocations of 4 KiB to 1 MiB
 *                in every host-visible memory type in turn, each keeping its last 3 alive and freeing every third one
 *                while it is still mapped; every word written (one per 4 KiB page) reads back as written. The table of
 *                CPU mappings had no lock, so two threads mapping or freeing two memories at once could corrupt the
 *                heap; the row runs with MallocScribble=1 and fails on NVK's "[!] ent == NULL" and "[!] nvRes:" lines.
 *                NVK prints that "[!] nvRes:" line for an allocation, a map or an unmap the RM refused, alike, so a
 *                refused allocation fails the case too: none of these few allocations of at most 1 MiB should be
 *                refused
 * The memory heaps and types, and whether a host-visible device-local type is in the largest device-local heap (BAR1
 * covers the video memory: the case where release 3.0.1 also moves NVK's own mapped memory behind BAR1), are
 * information.
 *   /usr/bin/xcrun clang++ -arch x86_64 -mmacosx-version-min=15.0 -std=c++17 -fobjc-arc -O2 -I<Vulkan headers> \
 *     tools/card/v301_vk_nvrm.mm -framework Foundation -framework Metal -o <run>/bin/<id>/nvmtltest
 * It skips on a Metal device of another driver and when the renderer's first physical device is not NVIDIA's. It writes
 * no file and opens no window. Exit 0 pass, 1 a case failed, 2 setup only, 3 skip, 70 crash. Not yet run on a Mac. */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <vulkan/vulkan.h>
#include <atomic>
#include <dlfcn.h>
#include <execinfo.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <string>
#include <unistd.h>
#include <vector>

namespace {

std::atomic<unsigned> passed{0}, failed{0}, setup_failed{0};
pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
std::string first_failure;

void crashed(int sig)
{
   const char *name = sig == SIGSEGV ? "SIGSEGV"
                      : sig == SIGBUS ? "SIGBUS"
                      : sig == SIGILL ? "SIGILL"
                      : sig == SIGFPE ? "SIGFPE"
                      : sig == SIGABRT ? "SIGABRT"
                                       : "SIGNAL";
   write(2, "CRASH ", 6);
   write(2, name, strlen(name));
   write(2, "\n", 1);
   void *frames[64];
   int count = backtrace(frames, 64);
   backtrace_symbols_fd(frames, count, 2);
   _exit(70);
}

std::string format(const char *fmt, va_list a)
{
   char text[600];
   vsnprintf(text, sizeof text, fmt, a);
   return text;
}

void line(const char *kind, const char *name, const std::string &text, std::atomic<unsigned> *count)
{
   pthread_mutex_lock(&lock);
   if (name)
      printf("%s %s%s%s\n", kind, name, !strcmp(kind, "FAIL") ? ": " : " ", text.c_str());
   else
      printf("%s %s\n", kind, text.c_str());
   if (!strcmp(kind, "FAIL") && first_failure.empty()) first_failure = std::string(name ? name : "") + ": " + text;
   pthread_mutex_unlock(&lock);
   if (count) count->fetch_add(1);
}

__attribute__((format(printf, 2, 3))) void pass(const char *name, const char *fmt, ...)
{
   va_list a;
   va_start(a, fmt);
   std::string t = format(fmt, a);
   va_end(a);
   line("PASS", name, t, &passed);
}

__attribute__((format(printf, 2, 3))) void fail(const char *name, const char *fmt, ...)
{
   va_list a;
   va_start(a, fmt);
   std::string t = format(fmt, a);
   va_end(a);
   line("FAIL", name, t, &failed);
}

__attribute__((format(printf, 2, 3))) void setup(const char *name, const char *fmt, ...)
{
   va_list a;
   va_start(a, fmt);
   std::string t = "setup: " + format(fmt, a);
   va_end(a);
   line("FAIL", name, t, &setup_failed);
}

__attribute__((format(printf, 1, 2))) void info(const char *fmt, ...)
{
   va_list a;
   va_start(a, fmt);
   std::string t = format(fmt, a);
   va_end(a);
   line("INFO", nullptr, t, nullptr);
}

[[noreturn]] __attribute__((format(printf, 1, 2))) void skip(const char *fmt, ...)
{
   va_list a;
   va_start(a, fmt);
   std::string t = format(fmt, a);
   va_end(a);
   printf("SKIP: %s\n", t.c_str());
   printf("RESULT skip %u passed, %u failed (%s)\n", passed.load(), failed.load(), t.c_str());
   exit(3);
}

int finish()
{
   unsigned p = passed.load(), f = failed.load(), s = setup_failed.load();
   if (!p && !f && !s) {
      printf("FAIL nothing: no case ran\n");
      first_failure = "nothing: no case ran";
      f = 1;
   }
   if (!f && !s) {
      printf("RESULT pass %u passed, 0 failed\n", p);
      return 0;
   }
   printf("RESULT fail %u passed, %u failed, %u setup (first: %s)\n", p, f, s, first_failure.c_str());
   return f ? 1 : 2;
}

struct Vk {
   PFN_vkGetInstanceProcAddr gipa = nullptr;
   VkInstance inst = VK_NULL_HANDLE;
   VkPhysicalDevice pd = VK_NULL_HANDLE;
   VkDevice dev = VK_NULL_HANDLE;
   PFN_vkGetDeviceProcAddr gdpa = nullptr;
   PFN_vkAllocateMemory alloc = nullptr;
   PFN_vkFreeMemory free_memory = nullptr;
   PFN_vkMapMemory map = nullptr;
   PFN_vkUnmapMemory unmap = nullptr;
   VkPhysicalDeviceMemoryProperties mem{};
   std::vector<uint32_t> host_types;
};
Vk vk;

enum { THREADS = 4, ITERATIONS = 200, KEEP = 3, PAGE = 4096 };

struct Live {
   VkDeviceMemory memory = VK_NULL_HANDLE;
   volatile uint32_t *words = nullptr;
   VkDeviceSize size = 0;
   uint32_t tag = 0;
};

struct Worker {
   unsigned index = 0;
   unsigned made = 0, refused = 0, wrong = 0, map_failed = 0;
   std::string first;
};

void release(Live &l, bool while_mapped)
{
   if (!l.memory) return;
   if (!while_mapped && l.words) vk.unmap(vk.dev, l.memory);
   vk.free_memory(vk.dev, l.memory, nullptr); // a memory freed while mapped is unmapped with it
   l = Live{};
}

void *work(void *p)
{
   Worker *w = static_cast<Worker *>(p);
   Live ring[KEEP];
   for (unsigned i = 0; i < ITERATIONS; i++) {
      Live &slot = ring[i % KEEP];
      if (slot.memory) {
         // every third one is freed while still mapped; the others are unmapped first; each is read once more first
         for (VkDeviceSize page = 0; page < slot.size / PAGE; page++) {
            const uint32_t want = slot.tag ^ (uint32_t)page;
            const uint32_t got = slot.words[page * (PAGE / 4)];
            if (got != want && !w->wrong++) {
               char text[200];
               snprintf(text, sizeof text, "allocation %08x page %llu read %08x before its free, %08x was written",
                        slot.tag, (unsigned long long)page, got, want);
               w->first = text;
            }
         }
         release(slot, i % 3 == 0);
      }
      const uint32_t type = vk.host_types[(w->index + i) % vk.host_types.size()];
      const VkDeviceSize size = (VkDeviceSize)PAGE << ((i * 7 + w->index) % 9); // 4 KiB .. 1 MiB
      VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
      ai.allocationSize = size;
      ai.memoryTypeIndex = type;
      VkDeviceMemory memory = VK_NULL_HANDLE;
      VkResult r = vk.alloc(vk.dev, &ai, nullptr, &memory);
      if (r != VK_SUCCESS) {
         if (!w->refused++ && w->first.empty())
            w->first = "vkAllocateMemory of " + std::to_string((unsigned long long)size) + " bytes in type " +
                       std::to_string(type) + " -> " + std::to_string((int)r);
         continue;
      }
      void *at = nullptr;
      r = vk.map(vk.dev, memory, 0, VK_WHOLE_SIZE, 0, &at);
      if (r != VK_SUCCESS || !at) {
         w->map_failed++;
         if (w->first.empty()) w->first = "vkMapMemory -> " + std::to_string((int)r);
         vk.free_memory(vk.dev, memory, nullptr);
         continue;
      }
      w->made++;
      slot.memory = memory;
      slot.words = static_cast<volatile uint32_t *>(at);
      slot.size = size;
      slot.tag = (w->index << 28) ^ (i << 8) ^ type;
      for (VkDeviceSize page = 0; page < size / PAGE; page++) slot.words[page * (PAGE / 4)] = slot.tag ^ (uint32_t)page;
      for (VkDeviceSize page = 0; page < size / PAGE; page++) {
         const uint32_t want = slot.tag ^ (uint32_t)page, got = slot.words[page * (PAGE / 4)];
         if (got != want && !w->wrong++) {
            char text[200];
            snprintf(text, sizeof text, "allocation %08x page %llu read %08x, %08x was written", slot.tag,
                     (unsigned long long)page, got, want);
            w->first = text;
         }
      }
   }
   for (Live &l : ring) release(l, false);
   return nullptr;
}

template <typename T> T proc(const char *name)
{
   return reinterpret_cast<T>(vk.gipa(vk.inst, name));
}

bool has(const std::vector<VkExtensionProperties> &list, const char *name)
{
   for (const VkExtensionProperties &e : list)
      if (!strcmp(e.extensionName, name)) return true;
   return false;
}

std::string flags_of(VkMemoryPropertyFlags f)
{
   std::string s;
   if (f & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) s += "device-local ";
   if (f & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT) s += "host-visible ";
   if (f & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT) s += "coherent ";
   if (f & VK_MEMORY_PROPERTY_HOST_CACHED_BIT) s += "cached ";
   if (s.empty()) s = "none ";
   s.pop_back();
   return s;
}

} // namespace

int main(int argc, char **argv)
{
   @autoreleasepool {
      const int crash_signals[] = {SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT};
      for (int sig : crash_signals) signal(sig, crashed);
      setvbuf(stdout, nullptr, _IOLBF, 0);
      alarm(60);
      if (argc < 2) {
         printf("FAIL usage: v301_vk_nvrm <libnvk_renderer.dylib>\n");
         return 2;
      }
      id<MTLDevice> card = MTLCreateSystemDefaultDevice();
      NSString *cls = card ? NSStringFromClass([(id)card class]) : @"none";
      if (![cls isEqualToString:@"NVMTLDevice"]) skip("no device of this driver (%s)", cls.UTF8String);

      void *lib = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL | RTLD_FIRST);
      if (!lib) {
         setup("load", "dlopen %s: %s", argv[1], dlerror());
         return finish();
      }
      vk.gipa = reinterpret_cast<PFN_vkGetInstanceProcAddr>(dlsym(lib, "vk_icdGetInstanceProcAddr"));
      auto create = vk.gipa ? proc<PFN_vkCreateInstance>("vkCreateInstance") : nullptr;
      if (!create) {
         setup("load", "%s answers no vk_icdGetInstanceProcAddr or vkCreateInstance", argv[1]);
         return finish();
      }
      VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
      app.pApplicationName = "nvmtltest";
      app.apiVersion = VK_API_VERSION_1_3;
      VkInstanceCreateInfo ci{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
      ci.pApplicationInfo = &app;
      VkResult r = create(&ci, nullptr, &vk.inst);
      if (r != VK_SUCCESS) {
         setup("instance", "vkCreateInstance (API 1.3, no extensions) -> %d", (int)r);
         return finish();
      }
      auto destroy_instance = proc<PFN_vkDestroyInstance>("vkDestroyInstance");
      auto physicals = proc<PFN_vkEnumeratePhysicalDevices>("vkEnumeratePhysicalDevices");
      auto properties = proc<PFN_vkGetPhysicalDeviceProperties>("vkGetPhysicalDeviceProperties");
      auto features2 = proc<PFN_vkGetPhysicalDeviceFeatures2>("vkGetPhysicalDeviceFeatures2");
      auto dev_ext = proc<PFN_vkEnumerateDeviceExtensionProperties>("vkEnumerateDeviceExtensionProperties");
      auto memory = proc<PFN_vkGetPhysicalDeviceMemoryProperties>("vkGetPhysicalDeviceMemoryProperties");
      auto families = proc<PFN_vkGetPhysicalDeviceQueueFamilyProperties>("vkGetPhysicalDeviceQueueFamilyProperties");
      auto create_device = proc<PFN_vkCreateDevice>("vkCreateDevice");
      vk.gdpa = proc<PFN_vkGetDeviceProcAddr>("vkGetDeviceProcAddr");
      if (!destroy_instance || !physicals || !properties || !features2 || !dev_ext || !memory || !families ||
          !create_device || !vk.gdpa) {
         setup("instance", "missing instance functions");
         return finish();
      }
      uint32_t n = 0;
      r = physicals(vk.inst, &n, nullptr);
      std::vector<VkPhysicalDevice> list(n);
      if (r == VK_SUCCESS && n) r = physicals(vk.inst, &n, list.data());
      if (r != VK_SUCCESS || !n) {
         setup("devices", "vkEnumeratePhysicalDevices -> %d, %u devices", (int)r, n);
         destroy_instance(vk.inst, nullptr);
         return finish();
      }
      vk.pd = list[0];
      VkPhysicalDeviceProperties props{};
      properties(vk.pd, &props);
      if (props.vendorID != 0x10de)
         skip("the renderer's first device is %s (vendor 0x%x), not NVIDIA's", props.deviceName, props.vendorID);
      info("the renderer's %s (device 0x%04x, API %u.%u.%u)", props.deviceName, props.deviceID,
           VK_API_VERSION_MAJOR(props.apiVersion), VK_API_VERSION_MINOR(props.apiVersion),
           VK_API_VERSION_PATCH(props.apiVersion));

      // placed
      uint32_t ne = 0;
      std::vector<VkExtensionProperties> ext;
      r = dev_ext(vk.pd, nullptr, &ne, nullptr);
      ext.resize(ne);
      if (r == VK_SUCCESS && ne) r = dev_ext(vk.pd, nullptr, &ne, ext.data());
      VkPhysicalDeviceMapMemoryPlacedFeaturesEXT placed{
         VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_MAP_MEMORY_PLACED_FEATURES_EXT};
      placed.memoryMapPlaced = placed.memoryMapRangePlaced = placed.memoryUnmapReserve = VK_TRUE;
      VkPhysicalDeviceFeatures2 f2{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2};
      f2.pNext = &placed;
      features2(vk.pd, &f2);
      if (r != VK_SUCCESS)
         setup("placed", "vkEnumerateDeviceExtensionProperties -> %d", (int)r);
      else if (has(ext, VK_EXT_MAP_MEMORY_PLACED_EXTENSION_NAME))
         fail("placed", "%s is offered on the NVRM back end", VK_EXT_MAP_MEMORY_PLACED_EXTENSION_NAME);
      else if (placed.memoryMapPlaced || placed.memoryMapRangePlaced || placed.memoryUnmapReserve)
         fail("placed", "memoryMapPlaced %u, memoryMapRangePlaced %u, memoryUnmapReserve %u (all VK_FALSE expected)",
              placed.memoryMapPlaced, placed.memoryMapRangePlaced, placed.memoryUnmapReserve);
      else
         pass("placed", "%s is not offered among %u device extensions, and its three features read VK_FALSE",
              VK_EXT_MAP_MEMORY_PLACED_EXTENSION_NAME, ne);

      // the memory, as information
      memory(vk.pd, &vk.mem);
      uint32_t vram = UINT32_MAX;
      for (uint32_t h = 0; h < vk.mem.memoryHeapCount; h++) {
         const VkMemoryHeap &heap = vk.mem.memoryHeaps[h];
         info("heap %u: %llu MB%s", h, (unsigned long long)(heap.size >> 20),
              heap.flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT ? ", device-local" : "");
         if ((heap.flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT) &&
             (vram == UINT32_MAX || heap.size > vk.mem.memoryHeaps[vram].size))
            vram = h;
      }
      bool bar_all = false;
      for (uint32_t t = 0; t < vk.mem.memoryTypeCount; t++) {
         const VkMemoryType &type = vk.mem.memoryTypes[t];
         info("type %u: heap %u, %s", t, type.heapIndex, flags_of(type.propertyFlags).c_str());
         if (type.propertyFlags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT) vk.host_types.push_back(t);
         const VkMemoryPropertyFlags both = VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT | VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT;
         if ((type.propertyFlags & both) == both && type.heapIndex == vram) bar_all = true;
      }
      info("a host-visible device-local type is in the largest device-local heap: %s",
           bar_all ? "yes (BAR1 covers the video memory)" : "no (BAR1 does not cover the video memory)");

      // map-threads
      uint32_t nf = 0;
      families(vk.pd, &nf, nullptr);
      if (!nf || vk.host_types.empty()) {
         setup("map-threads", "%u queue families, %zu host-visible memory types", nf, vk.host_types.size());
         destroy_instance(vk.inst, nullptr);
         return finish();
      }
      const float priority = 1.0f;
      VkDeviceQueueCreateInfo qi{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
      qi.queueFamilyIndex = 0;
      qi.queueCount = 1;
      qi.pQueuePriorities = &priority;
      VkDeviceCreateInfo di{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
      di.queueCreateInfoCount = 1;
      di.pQueueCreateInfos = &qi;
      r = create_device(vk.pd, &di, nullptr, &vk.dev);
      if (r != VK_SUCCESS) {
         setup("map-threads", "vkCreateDevice -> %d", (int)r);
         destroy_instance(vk.inst, nullptr);
         return finish();
      }
      auto destroy_device = reinterpret_cast<PFN_vkDestroyDevice>(vk.gdpa(vk.dev, "vkDestroyDevice"));
      vk.alloc = reinterpret_cast<PFN_vkAllocateMemory>(vk.gdpa(vk.dev, "vkAllocateMemory"));
      vk.free_memory = reinterpret_cast<PFN_vkFreeMemory>(vk.gdpa(vk.dev, "vkFreeMemory"));
      vk.map = reinterpret_cast<PFN_vkMapMemory>(vk.gdpa(vk.dev, "vkMapMemory"));
      vk.unmap = reinterpret_cast<PFN_vkUnmapMemory>(vk.gdpa(vk.dev, "vkUnmapMemory"));
      if (!destroy_device || !vk.alloc || !vk.free_memory || !vk.map || !vk.unmap) {
         setup("map-threads", "missing device functions");
         return finish();
      }
      Worker workers[THREADS];
      pthread_t th[THREADS];
      unsigned started = 0;
      for (unsigned i = 0; i < THREADS; i++) {
         workers[i].index = i;
         if (!pthread_create(&th[i], nullptr, work, &workers[i])) started++;
         else break;
      }
      for (unsigned i = 0; i < started; i++) pthread_join(th[i], nullptr);
      destroy_device(vk.dev, nullptr);
      destroy_instance(vk.inst, nullptr);
      unsigned made = 0, refused = 0, wrong = 0, map_failed = 0;
      std::string first;
      for (unsigned i = 0; i < started; i++) {
         made += workers[i].made;
         refused += workers[i].refused;
         wrong += workers[i].wrong;
         map_failed += workers[i].map_failed;
         if (first.empty()) first = workers[i].first;
      }
      if (started != THREADS)
         setup("map-threads", "%u of %u threads started", started, (unsigned)THREADS);
      else if (wrong || map_failed || refused)
         fail("map-threads", "%u words read wrong, %u maps failed, %u allocations refused; the first: %s", wrong,
              map_failed, refused, first.c_str());
      else if (made < THREADS * ITERATIONS / 2)
         fail("map-threads", "only %u of %u allocations were made", made, (unsigned)(THREADS * ITERATIONS));
      else
         pass("map-threads", "%u threads made, mapped, wrote, read and freed %u allocations of 4 KiB to 1 MiB at once "
              "in %zu host-visible types; every word read back as written",
              (unsigned)THREADS, made, vk.host_types.size());
      return finish();
   }
}
