// Host checks of the native Vulkan presentation frontend. It includes the
// frontend's source and replaces the renderer with fakes, so no GPU is used.
// Build and run on a Mac:
//   xcrun clang++ -arch x86_64 -mmacosx-version-min=15.0 -std=c++17 -fobjc-arc -O2 \
//     -I<Vulkan headers> nvk/native-macos/host_test.mm -framework Foundation \
//     -framework QuartzCore -framework Metal -framework IOSurface -framework IOKit -o host_test
//   ./host_test
#include "bridge.mm"
#include <cassert>



static std::atomic<int> submits{}, waits{}, destroyed_semaphores{};
static std::atomic<VkResult> injected{VK_SUCCESS};
static VkResult fake_submit(VkQueue,uint32_t count,const VkSubmitInfo *infos,VkFence) {
   assert(count==1);
   if (infos->waitSemaphoreCount) assert(infos->pWaitSemaphores && infos->pWaitDstStageMask);
   submits++; return injected;
}
static VkResult fake_idle(VkQueue) { waits++;return VK_SUCCESS; }
static VkResult fake_semaphore(VkDevice,const VkSemaphoreCreateInfo *,const VkAllocationCallbacks *,VkSemaphore *out) {
   *out=(VkSemaphore)(uintptr_t)0x44;return VK_SUCCESS;
}
static void fake_destroy_semaphore(VkDevice,VkSemaphore,const VkAllocationCallbacks *) { destroyed_semaphores++; }
static VkResult fake_device_idle(VkDevice) { return VK_SUCCESS; }
static void fake_destroy_device(VkDevice,const VkAllocationCallbacks *) {}
static PFN_vkVoidFunction fake_gdpa(VkDevice,const char *name) {
   if (!strcmp(name,"vkQueueSubmit")) return (PFN_vkVoidFunction)fake_submit;
   if (!strcmp(name,"vkQueueWaitIdle")) return (PFN_vkVoidFunction)fake_idle;
   if (!strcmp(name,"vkCreateSemaphore")) return (PFN_vkVoidFunction)fake_semaphore;
   if (!strcmp(name,"vkDestroySemaphore")) return (PFN_vkVoidFunction)fake_destroy_semaphore;
   if (!strcmp(name,"vkDeviceWaitIdle")) return (PFN_vkVoidFunction)fake_device_idle;
   if (!strcmp(name,"vkDestroyDevice")) return (PFN_vkVoidFunction)fake_destroy_device;
   return nullptr;
}
static void fake_properties(VkPhysicalDevice,VkPhysicalDeviceProperties *out) { *out={};strcpy(out->deviceName,"host fixture"); }
static void fake_memory_properties(VkPhysicalDevice,VkPhysicalDeviceMemoryProperties *out) { *out={}; }
static bool advertise_host_memory=false;
static int host_property_queries{};
static VkDeviceSize host_import_alignment=4096;
static void fake_properties2(VkPhysicalDevice physical,VkPhysicalDeviceProperties2 *out) {
   fake_properties(physical,&out->properties);host_property_queries++;
   assert(out->pNext && ((VkBaseOutStructure *)out->pNext)->sType==VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_EXTERNAL_MEMORY_HOST_PROPERTIES_EXT);
   ((VkPhysicalDeviceExternalMemoryHostPropertiesEXT *)out->pNext)->minImportedHostPointerAlignment=host_import_alignment;
}
static VkResult fake_create_device(VkPhysicalDevice,const VkDeviceCreateInfo *,const VkAllocationCallbacks *,VkDevice *out) {
   *out=(VkDevice)(uintptr_t)0x100;return VK_SUCCESS;
}
static void fake_families(VkPhysicalDevice,uint32_t *count,VkQueueFamilyProperties *out) {
   if (out && *count) { *out={};out->queueCount=1;out->queueFlags=VK_QUEUE_GRAPHICS_BIT|VK_QUEUE_COMPUTE_BIT|VK_QUEUE_TRANSFER_BIT; }
   *count=1;
}
static VkResult fake_device_extensions(VkPhysicalDevice,const char *,uint32_t *count,VkExtensionProperties *out) {
   std::vector<VkExtensionProperties> available{extension("VK_EXT_hdr_metadata",1),
      extension("VK_KHR_external_memory_fd",1),extension("VK_KHR_external_semaphore_fd",1),
      extension("VK_KHR_external_fence_fd",1),extension("VK_EXT_transform_feedback",1),
      extension("VK_KHR_swapchain",1),extension("VK_KHR_present_wait",1),extension("VK_KHR_swapchain_maintenance1",1)};
   if(advertise_host_memory)available.push_back(extension(VK_EXT_EXTERNAL_MEMORY_HOST_EXTENSION_NAME,1));
   return enumerate(available,count,out);
}
static VkResult fake_instance_extensions(const char *,uint32_t *count,VkExtensionProperties *out) {
   return enumerate(std::vector<VkExtensionProperties>{extension(VK_KHR_SURFACE_EXTENSION_NAME,25),
      extension(VK_EXT_METAL_SURFACE_EXTENSION_NAME,1),extension(VK_EXT_HEADLESS_SURFACE_EXTENSION_NAME,1),
      extension("VK_KHR_surface_maintenance1",1),extension("VK_KHR_get_physical_device_properties2",2)},count,out);
}
static bool listed(const std::vector<VkExtensionProperties> &all,uint32_t n,const char *name,int times=1) {
   return std::count_if(all.begin(),all.begin()+n,[&](auto &e){return !strcmp(e.extensionName,name);})==times;
}
static PFN_vkVoidFunction fake_gipa(VkInstance,const char *name) {
   if (!strcmp(name,"vkEnumerateInstanceExtensionProperties")) return (PFN_vkVoidFunction)fake_instance_extensions;
   if (!strcmp(name,"vkGetPhysicalDeviceProperties")) return (PFN_vkVoidFunction)fake_properties;
   if (!strcmp(name,"vkGetPhysicalDeviceProperties2")) return (PFN_vkVoidFunction)fake_properties2;
   if (!strcmp(name,"vkGetPhysicalDeviceMemoryProperties")) return (PFN_vkVoidFunction)fake_memory_properties;
   if (!strcmp(name,"vkCreateDevice")) return (PFN_vkVoidFunction)fake_create_device;
   if (!strcmp(name,"vkGetDeviceProcAddr")) return (PFN_vkVoidFunction)fake_gdpa;
   if (!strcmp(name,"vkGetPhysicalDeviceQueueFamilyProperties")) return (PFN_vkVoidFunction)fake_families;
   if (!strcmp(name,"vkEnumerateDeviceExtensionProperties")) return (PFN_vkVoidFunction)fake_device_extensions;
   return (PFN_vkVoidFunction)fake_idle;
}
static VkResult throw_oom() { throw std::bad_alloc(); }
static VkResult throw_error() { throw std::runtime_error("injected"); }

int main() { @autoreleasepool {
   std::call_once(backend_once,[]{});backend_gipa=fake_gipa;backend_gdpa=fake_gdpa;
   uint32_t count=0;int out=99;
   assert(enumerate(std::vector<int>{1,2},&count,&out)==VK_INCOMPLETE && count==0 && out==99);
   count=1;assert(enumerate(std::vector<int>{1,2},&count,&out)==VK_INCOMPLETE && count==1 && out==1);
   assert(enumerate(std::vector<int>{1,2},&count,(int *)nullptr)==VK_SUCCESS && count==2);
   puts("PASS bounded enumeration and zero-capacity writes");

   auto i=std::make_shared<Instance>();i->raw=(VkInstance)(uintptr_t)1;
   VkPhysicalDevice p=(VkPhysicalDevice)(uintptr_t)2;
   auto s=std::make_shared<Surface>();
   VkSurfaceKHR surface=(VkSurfaceKHR)(uintptr_t)3;
   physicals[p]=i;surfaces[surface]=s;
   {
      std::vector<VkExtensionProperties> all(16);count=16;
      assert(instance_extensions(nullptr,&count,all.data())==VK_SUCCESS);
      assert(listed(all,count,VK_KHR_SURFACE_EXTENSION_NAME) && listed(all,count,VK_EXT_METAL_SURFACE_EXTENSION_NAME) &&
             listed(all,count,VK_KHR_GET_SURFACE_CAPABILITIES_2_EXTENSION_NAME) &&
             listed(all,count,"VK_KHR_get_physical_device_properties2") &&
             listed(all,count,VK_EXT_HEADLESS_SURFACE_EXTENSION_NAME,0) && listed(all,count,"VK_KHR_surface_maintenance1",0));
      count=16;assert(device_extensions(p,nullptr,&count,all.data())==VK_SUCCESS);
      assert(listed(all,count,VK_KHR_SWAPCHAIN_EXTENSION_NAME) && listed(all,count,"VK_KHR_external_memory_fd") &&
             listed(all,count,"VK_EXT_hdr_metadata",0) && listed(all,count,"VK_KHR_present_wait",0) &&
             listed(all,count,"VK_KHR_swapchain_maintenance1",0));
      const char *asked="VK_KHR_surface_maintenance1";VkInstance never{};
      VkInstanceCreateInfo instanceInfo{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
      instanceInfo.enabledExtensionCount=1;instanceInfo.ppEnabledExtensionNames=&asked;
      assert(create_instance(&instanceInfo,nullptr,&never)==VK_ERROR_EXTENSION_NOT_PRESENT && !never);
      float priority=1;
      VkDeviceQueueCreateInfo queueInfo{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
      queueInfo.queueCount=1;queueInfo.pQueuePriorities=&priority;
      VkDeviceCreateInfo deviceInfo{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
      deviceInfo.queueCreateInfoCount=1;deviceInfo.pQueueCreateInfos=&queueInfo;
      asked="VK_KHR_present_wait";deviceInfo.enabledExtensionCount=1;deviceInfo.ppEnabledExtensionNames=&asked;
      VkDevice none{};assert(create_device(p,&deviceInfo,nullptr,&none)==VK_ERROR_EXTENSION_NOT_PRESENT && !none);
      assert(vkGetInstanceProcAddr(i->raw,"vkGetPhysicalDeviceSurfacePresentModes2EXT")==nullptr);
      assert(vkGetInstanceProcAddr(i->raw,"vkCreateHeadlessSurfaceEXT")==nullptr);
      puts("PASS the renderer's own surface and swapchain extensions are neither offered nor accepted");
   }
   {
      auto topology=std::make_shared<Device>();topology->physical=p;topology->instance=i;
      auto &m=topology->memory;
      constexpr VkDeviceSize gib=1ull<<30;
      m.memoryHeapCount=2;m.memoryHeaps[0]={8*gib,VK_MEMORY_HEAP_DEVICE_LOCAL_BIT};m.memoryHeaps[1]={12*gib,0};
      m.memoryTypeCount=3;m.memoryTypes[0]={VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT,0};
      m.memoryTypes[1]={VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT|VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT,0};
      m.memoryTypes[2]={VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT|VK_MEMORY_PROPERTY_HOST_COHERENT_BIT|VK_MEMORY_PROPERTY_HOST_CACHED_BIT,1};
      std::vector<const char *> extensions{VK_KHR_EXTERNAL_MEMORY_FD_EXTENSION_NAME};
      advertise_host_memory=true;const int before=host_property_queries;
      assert(!limited_bar_memory(m));enable_host_present(topology,extensions);
      assert(!topology->host_present_alignment && host_property_queries==before && extensions.size()==1);
      // The old limited-BAR layout has a small visible heap; the new one has
      // no visible VRAM type. Both permit the same presentation recovery.
      m.memoryHeapCount=3;m.memoryHeaps[2]={256ull<<20,VK_MEMORY_HEAP_DEVICE_LOCAL_BIT};m.memoryTypes[1].heapIndex=2;
      assert(limited_bar_memory(m));enable_host_present(topology,extensions);
      assert(topology->host_present_alignment==4096 && extensions.size()==2);
      enable_host_present(topology,extensions);assert(extensions.size()==2);
      m.memoryTypes[1]=m.memoryTypes[2];m.memoryTypeCount=2;m.memoryHeapCount=2;
      assert(limited_bar_memory(m));
      topology->host_present_alignment=0;extensions.resize(1);advertise_host_memory=false;
      enable_host_present(topology,extensions);assert(!topology->host_present_alignment && extensions.size()==1);
      advertise_host_memory=true;
      for(auto bad:{0ull,3ull}) {
         host_import_alignment=bad;enable_host_present(topology,extensions);
         assert(!topology->host_present_alignment && extensions.size()==1);
      }
      host_import_alignment=4096;enable_host_present(topology,extensions);assert(topology->host_present_alignment==4096);
      m.memoryHeaps[0].size=0;assert(!limited_bar_memory(m));
      advertise_host_memory=false;
      puts("PASS limited-BAR host recovery capability checks; full BAR leaves extensions and queries unchanged");
   }
   {
      float priority=1;
      VkDeviceQueueCreateInfo queueInfo{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
      queueInfo.queueCount=1;queueInfo.pQueuePriorities=&priority;
      VkDeviceCreateInfo deviceInfo{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
      deviceInfo.queueCreateInfoCount=1;deviceInfo.pQueueCreateInfos=&queueInfo;
      VkDevice created{};assert(create_device(p,&deviceInfo,nullptr,&created)==VK_SUCCESS);
      auto state=find(devices,created);assert(state && !state->swapchain && !state->private_copy_queue);
      destroy_device(created,nullptr);assert(!find(devices,created));
      puts("PASS device creation without a swapchain registers and releases the device");
   }
   VkPhysicalDeviceSurfaceInfo2KHR info{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SURFACE_INFO_2_KHR};info.surface=surface;
   VkSurfaceFormat2KHR format{VK_STRUCTURE_TYPE_SURFACE_FORMAT_2_KHR};format.surfaceFormat.format=VK_FORMAT_UNDEFINED;
   count=0;assert(surface_formats2(p,&info,&count,&format)==VK_INCOMPLETE && !count && format.surfaceFormat.format==VK_FORMAT_UNDEFINED);
   count=1;assert(surface_formats2(p,&info,&count,&format)==VK_INCOMPLETE && count==1 && format.surfaceFormat.format==VK_FORMAT_B8G8R8A8_SRGB);
   puts("PASS surface-formats2 preserves caller bounds");

   auto d=std::make_shared<Device>();d->raw=(VkDevice)(uintptr_t)4;d->copy_queue=(VkQueue)(uintptr_t)5;d->swapchain=true;
   auto c=std::make_shared<Chain>();c->dev=d;c->surface=s;c->images.resize(2);c->handle=(VkSwapchainKHR)(uintptr_t)6;
   devices[d->raw]=d;chains[c->handle]=c;
   uint32_t index=99;VkSemaphore semaphore=(VkSemaphore)(uintptr_t)7;
   assert(acquire_image(d->raw,c->handle,0,semaphore,VK_NULL_HANDLE,&index)==VK_SUCCESS && index==0 && submits==1);
   assert(acquire_image(d->raw,c->handle,0,semaphore,VK_NULL_HANDLE,&index)==VK_SUCCESS && index==1 && submits==2);
   assert(acquire_image(d->raw,c->handle,0,semaphore,VK_NULL_HANDLE,&index)==VK_NOT_READY && submits==2);
   assert(acquire_image(d->raw,c->handle,1000,semaphore,VK_NULL_HANDLE,&index)==VK_TIMEOUT && submits==2);
   puts("PASS acquire reservations, immediate exhaustion and bounded timeout");
   c->images[0].acquired=false;injected=VK_ERROR_OUT_OF_DEVICE_MEMORY;
   assert(acquire_image(d->raw,c->handle,0,semaphore,VK_NULL_HANDLE,&index)==injected && !c->images[0].acquired);
   injected=VK_SUCCESS;c->retired=true;
   assert(acquire_image(d->raw,c->handle,UINT64_MAX,semaphore,VK_NULL_HANDLE,&index)==VK_ERROR_OUT_OF_DATE_KHR);
   assert(acquire_image(d->raw,c->handle,0,VK_NULL_HANDLE,VK_NULL_HANDLE,&index)==VK_ERROR_INITIALIZATION_FAILED);
   puts("PASS failed submission rolls back acquisition; retired chains unblock");

   auto q=std::make_shared<Queue>();q->raw=(VkQueue)(uintptr_t)8;q->dev=d;queues[q->raw]=q;
   VkSwapchainKHR absent=(VkSwapchainKHR)(uintptr_t)99;uint32_t zero=0;
   VkPresentInfoKHR present{VK_STRUCTURE_TYPE_PRESENT_INFO_KHR};present.swapchainCount=1;present.pSwapchains=&absent;present.pImageIndices=&zero;
   submits=waits=destroyed_semaphores=0;
   assert(queue_present(q->raw,&present)==VK_ERROR_OUT_OF_DATE_KHR && submits==2 && waits==1 && destroyed_semaphores==1);
   injected=VK_ERROR_OUT_OF_DEVICE_MEMORY;submits=waits=destroyed_semaphores=0;
   assert(queue_present(q->raw,&present)==injected && submits==1 && waits==0 && destroyed_semaphores==1);
   injected=VK_SUCCESS;
   puts("PASS rejected presents consume queued handoff once; failed submissions unwind");
   assert(vkGetInstanceProcAddr(VK_NULL_HANDLE,"vkCreateSwapchainKHR")==nullptr);
   assert(vkGetDeviceProcAddr(d->raw,"vkCreateInstance")==nullptr);
   assert(vkGetDeviceProcAddr(d->raw,"vkQueuePresentKHR")!=nullptr);
   for (const char *absent : {"vkWaitForPresentKHR","vkSetHdrMetadataEXT","vkGetSwapchainStatusKHR",
        "vkReleaseSwapchainImagesKHR","vkGetPastPresentationTimingGOOGLE"})
      assert(vkGetDeviceProcAddr(d->raw,absent)==nullptr);
   d->swapchain=false;assert(vkGetDeviceProcAddr(d->raw,"vkCreateSwapchainKHR")==nullptr);
   assert(ApiGuard<throw_oom>::call()==VK_ERROR_OUT_OF_HOST_MEMORY);
   assert(ApiGuard<throw_error>::call()==VK_ERROR_UNKNOWN);
   puts("PASS command scope, extension gating and C ABI exception containment");
   assert(lookup("vkCreateMetalSurfaceEXT")==reinterpret_cast<PFN_vkVoidFunction>(&vkCreateMetalSurfaceEXT));
   VkMetalSurfaceCreateInfoEXT metalInfo{VK_STRUCTURE_TYPE_METAL_SURFACE_CREATE_INFO_EXT};
   VkSurfaceKHR untouched=(VkSurfaceKHR)(uintptr_t)0x1234;
   assert(vkCreateMetalSurfaceEXT(VK_NULL_HANDLE,&metalInfo,nullptr,&untouched)==VK_ERROR_INITIALIZATION_FAILED);
   assert(untouched==(VkSurfaceKHR)(uintptr_t)0x1234);
   puts("PASS direct Metal surface export shares guarded dispatch and rejects invalid input");
   puts("HOST CHECKS PASSED; bridge submission functions were mocked");
} }
