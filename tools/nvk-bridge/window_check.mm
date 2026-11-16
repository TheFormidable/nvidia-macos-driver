// window_check.mm: the native Vulkan runtime (libnvk_minecraft.dylib, README.md) presenting into an AppKit window on
// the card, without SDL3 and without any screen capture. One mode per process, so that a crash, a hang or a device
// that the frontend keeps after an uncertain copy ends one run only:
//   nvmtltest direct <frontend>            the frontend opened by its path, as Minecraft opens it
//   nvmtltest loader <loader> <frontend>   a Vulkan loader opened instead; VK_DRIVER_FILES names the runtime's
//                                          manifest, and the loader must route to <frontend>
//   nvmtltest renderer <renderer>          the control, with no window and no device: which of the window-system
//                                          extensions that the frontend hides its renderer offers
// The windowed modes check what the frontend offers and hides (instance and device extensions, entry points, the
// loader interface), the Metal surface with its capabilities, formats, modes and rectangle, and the swapchain's
// refusals. Then they present into a window of their own: FIFO with 3 images (and the acquire limits), a resize, a
// minimize and a restore, IMMEDIATE sRGB with 2 images, and 8 images with premultiplied alpha. Every presented copy is
// read back through the frontend's present observer (nvkbridgeSetPresentObserver, the hook smoke.mm's
// NVKBRIDGE_VERIFY_PIXELS uses) and every pixel compared. The layer is a CAMetalLayer subclass that watches its
// drawables: one nextDrawable per present, their size, format and device, the NVMTLVramAt:p0 generation tag and the
// presented handlers. Every tenth frame is also read by the CPU through a read-only IOSurface lock, a Metal blit runs
// in the same process while the Vulkan device lives, and the frontend's counters (nvkbridgeGetStats) must count every
// copy, with no refused VRAM import, on a device whose BAR covers its VRAM.
// Conventions of tools/card: exit 0 pass, 1 a check failed, 2 setup error (the program's own window, layer or pipe,
// and a window that does not minimize or restore within 5 s),
// 3 skip (first line "SKIP: <why>"); one line per case and a last line RESULT; its own alarm; crash handlers that print
// CRASH <signal> and exit 70. It writes no file and captures nothing. The window floats, is ordered front without
// activating the program and is closed at the end; the activation policy is Regular (a Dock icon while it runs), so
// that the window can be minimized and restored.
// Build: xcrun clang++ -arch x86_64 -mmacosx-version-min=15.0 -std=c++17 -fobjc-arc -O2 -Wall -Wextra
//        -Wno-missing-field-initializers -I<Vulkan headers> window_check.mm -framework Foundation -framework AppKit
//        -framework QuartzCore -framework Metal -framework IOSurface -framework CoreGraphics -o nvmtltest
// The plugin always grants a Metal device to a program named nvmtltest. Not yet run on a Mac: written on Linux and
// checked there only by a syntax-only compile against a stand-in SDK.
#define VK_NO_PROTOTYPES
#define VK_USE_PLATFORM_METAL_EXT
#include <vulkan/vulkan.h>
#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>
#import <IOSurface/IOSurface.h>
#import <Metal/Metal.h>
#import <QuartzCore/QuartzCore.h>
#include <CoreGraphics/CoreGraphics.h>
#include <dlfcn.h>
#include <errno.h>
#include <execinfo.h>
#include <fcntl.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr uint32_t kTagMagic = 0x564d5634u;   // the NVMTLVramAt:p<plane> tag's magic (plugin/NVMTLDevice.m:1469-1470)
constexpr uint32_t kSlots = 4096;             // drawables whose presented handlers are tracked
constexpr uint64_t kSecond = 1000000000ull;

// ---- output -------------------------------------------------------------------------------------------------------

std::mutex out_mutex;
int passed, failed;

double now()
{
   timespec t{};
   clock_gettime(CLOCK_MONOTONIC, &t);
   return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

__attribute__((format(printf, 1, 0))) std::string vformat(const char *fmt, va_list ap)
{
   char text[2048];
   vsnprintf(text, sizeof text, fmt, ap);
   return text;
}

__attribute__((format(printf, 1, 2))) std::string format(const char *fmt, ...)
{
   va_list ap;
   va_start(ap, fmt);
   std::string s = vformat(fmt, ap);
   va_end(ap);
   return s;
}

void line(const std::string &text)
{
   std::string s = text;
   for (char &c : s)
      if (c == '\n' || c == '\r') c = ' ';
   std::lock_guard<std::mutex> lock(out_mutex);
   fputs(s.c_str(), stdout);
   fputc('\n', stdout);
   fflush(stdout);
}

__attribute__((format(printf, 2, 3))) void pass(const char *name, const char *fmt, ...)
{
   va_list ap;
   va_start(ap, fmt);
   line(std::string("PASS ") + name + " " + vformat(fmt, ap));
   va_end(ap);
   ++passed;
}

__attribute__((format(printf, 2, 3))) void fail(const char *name, const char *fmt, ...)
{
   va_list ap;
   va_start(ap, fmt);
   line(std::string("FAIL ") + name + ": " + vformat(fmt, ap));
   va_end(ap);
   ++failed;
}

__attribute__((format(printf, 1, 2))) void info(const char *fmt, ...)
{
   va_list ap;
   va_start(ap, fmt);
   line("INFO " + vformat(fmt, ap));
   va_end(ap);
}

void check(bool ok, const char *name, const std::string &good, const std::string &bad)
{
   if (ok) pass(name, "%s", good.c_str());
   else fail(name, "%s", bad.c_str());
}

void tee_stop();

[[noreturn]] void leave(int status)
{
   tee_stop();
   fflush(stdout);
   alarm(10);   // what exit runs (the drivers' destructors) has 10 s; then SIGALRM ends it as a time-out
   exit(status);
}

[[noreturn]] void finish()
{
   const bool ok = failed == 0 && passed > 0;
   line(format("RESULT %s %d passed, %d failed", ok ? "pass" : "fail", passed, failed));
   leave(ok ? 0 : 1);
}

// A check that the rest of the run depends on failed: report it and end the run.
[[noreturn]] __attribute__((format(printf, 2, 3))) void fatal(const char *name, const char *fmt, ...)
{
   va_list ap;
   va_start(ap, fmt);
   line(std::string("FAIL ") + name + ": " + vformat(fmt, ap));
   va_end(ap);
   ++failed;
   finish();
}

[[noreturn]] __attribute__((format(printf, 1, 2))) void skip(const char *fmt, ...)
{
   va_list ap;
   va_start(ap, fmt);
   line("SKIP: " + vformat(fmt, ap));
   va_end(ap);
   line(format("RESULT skip %d passed, %d failed", passed, failed));
   leave(3);
}

[[noreturn]] __attribute__((format(printf, 1, 2))) void setup_error(const char *fmt, ...)
{
   va_list ap;
   va_start(ap, fmt);
   line("FAIL setup: " + vformat(fmt, ap));
   va_end(ap);
   line(format("RESULT fail %d passed, %d failed", passed, failed + 1));
   leave(2);
}

// ---- crash handlers and alarm -------------------------------------------------------------------------------------

int err_fd = STDERR_FILENO;   // the real standard error (fd 2 is the copy's pipe while the copy runs)
volatile sig_atomic_t crashing;

void write_all(int fd, const char *s, size_t n)
{
   while (n) {
      ssize_t w = write(fd, s, n);
      if (w < 0 && errno == EINTR) continue;
      if (w <= 0) return;
      s += w;
      n -= (size_t)w;
   }
}

void on_crash(int sig)
{
   if (crashing) _exit(70);
   crashing = 1;
   const char *name = sig == SIGSEGV ? "CRASH SIGSEGV\n" : sig == SIGBUS ? "CRASH SIGBUS\n"
                    : sig == SIGILL ? "CRASH SIGILL\n" : sig == SIGFPE ? "CRASH SIGFPE\n"
                    : sig == SIGABRT ? "CRASH SIGABRT\n" : "CRASH signal\n";
   write_all(STDOUT_FILENO, name, strlen(name));
   write_all(err_fd, name, strlen(name));
   void *frames[64];
   int count = backtrace(frames, 64);
   backtrace_symbols_fd(frames, count, err_fd);
   _exit(70);
}

// seconds: the program's own limit (five times its expected time, at least 20 s); SIGALRM keeps its default action,
// which the runner reports as a time-out.
void install_handlers(unsigned seconds)
{
   static char stack[1 << 16];
   stack_t alt{};
   alt.ss_sp = stack;
   alt.ss_size = sizeof stack;
   sigaltstack(&alt, nullptr);
   void *warm[4];
   (void)backtrace(warm, 4);   // binds backtrace's lazy symbols now, outside a signal handler
   for (int sig : {SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT}) {
      struct sigaction sa{};
      sa.sa_handler = on_crash;
      sa.sa_flags = SA_ONSTACK;
      sigemptyset(&sa.sa_mask);
      sigaction(sig, &sa, nullptr);
   }
   alarm(seconds < 20 ? 20 : seconds);
}

// ---- the copy of standard error -----------------------------------------------------------------------------------
// The frontend reports what it did on standard error ("NVK Bridge: ..." lines, bridge.mm), and so does the loader
// with VK_LOADER_DEBUG=driver. fd 2 becomes a pipe that a thread copies on to the real standard error unchanged; it
// keeps the frontend's lines and the loader's first line naming the runtime's manifest, so that the program can check
// them. A sync mark written to fd 2 tells when everything written before it has been read.

std::mutex tee_mutex;
std::condition_variable tee_cv;
std::vector<std::string> bridge_lines;   // the "NVK Bridge: " lines, in order (at most 4096)
std::string manifest_line;               // the first line that names nvk_bridge_icd.json
unsigned tee_marks, tee_sent;
bool tee_running, tee_done;
int tee_read_fd = -1;
const char kMark[] = "\033window-check-sync";

void tee_take(const std::string &text)
{
   const size_t mark = strlen(kMark);
   std::string body = text;
   bool marked = body.size() >= mark && body.compare(body.size() - mark, mark, kMark) == 0;
   if (marked) body.resize(body.size() - mark);   // another writer's unfinished line may precede the mark
   if (!body.empty() || !marked) {
      std::string out = body + "\n";
      write_all(err_fd, out.data(), out.size());
   }
   std::lock_guard<std::mutex> lock(tee_mutex);
   if (body.compare(0, 12, "NVK Bridge: ") == 0 && bridge_lines.size() < 4096) bridge_lines.push_back(body);
   if (manifest_line.empty() && body.find("nvk_bridge_icd.json") != std::string::npos) manifest_line = body;
   if (marked) {
      ++tee_marks;
      tee_cv.notify_all();
   }
}

void tee_thread()
{
   std::string pending;
   char buf[4096];
   for (;;) {
      ssize_t n = read(tee_read_fd, buf, sizeof buf);
      if (n < 0 && errno == EINTR) continue;
      if (n <= 0) break;
      for (ssize_t i = 0; i < n; ++i) {
         if (buf[i] != '\n') {
            pending.push_back(buf[i]);
            if (pending.size() < 65536) continue;
         }
         tee_take(pending);
         pending.clear();
      }
   }
   if (!pending.empty()) write_all(err_fd, pending.data(), pending.size());
   std::lock_guard<std::mutex> lock(tee_mutex);
   tee_done = true;
   tee_cv.notify_all();
}

void tee_start()
{
   int p[2];
   if (pipe(p)) setup_error("no pipe for the copy of standard error: %s", strerror(errno));
   int real = dup(STDERR_FILENO);
   if (real < 0) setup_error("no copy of standard error: %s", strerror(errno));
   fcntl(real, F_SETFD, FD_CLOEXEC);
   fcntl(p[0], F_SETFD, FD_CLOEXEC);
   fflush(stderr);
   if (dup2(p[1], STDERR_FILENO) < 0)
      setup_error("standard error could not be pointed at the pipe: %s", strerror(errno));
   close(p[1]);
   err_fd = real;
   tee_read_fd = p[0];
   tee_running = true;
   std::thread(tee_thread).detach();
}

// -> true when everything written to fd 2 before the call has been read (2 s at most)
bool tee_sync()
{
   if (!tee_running) return false;
   const unsigned want = ++tee_sent;
   std::string mark = std::string(kMark) + "\n";
   write_all(STDERR_FILENO, mark.data(), mark.size());
   std::unique_lock<std::mutex> lock(tee_mutex);
   tee_cv.wait_for(lock, std::chrono::seconds(2), [&] { return tee_marks >= want || tee_done; });
   return tee_marks >= want;
}

void tee_stop()
{
   if (!tee_running) return;
   tee_sync();
   tee_running = false;
   dup2(err_fd, STDERR_FILENO);   // the pipe's last writer: the thread reads the end and stops
   std::unique_lock<std::mutex> lock(tee_mutex);
   tee_cv.wait_for(lock, std::chrono::seconds(1), [] { return tee_done; });
}

std::vector<std::string> bridge_with(const char *part)
{
   std::lock_guard<std::mutex> lock(tee_mutex);
   std::vector<std::string> found;
   for (auto &l : bridge_lines)
      if (l.find(part) != std::string::npos) found.push_back(l);
   return found;
}

// ---- the session, the device, paths -------------------------------------------------------------------------------

void require_gui()
{
   CFDictionaryRef session = CGSessionCopyCurrentDictionary();
   if (!session) skip("no GUI session for this process (CGSessionCopyCurrentDictionary is NULL)");
   CFTypeRef console = CFDictionaryGetValue(session, kCGSessionOnConsoleKey);
   CFTypeRef locked = CFDictionaryGetValue(session, CFSTR("CGSSessionScreenIsLocked"));
   const bool on_console =
      console && CFGetTypeID(console) == CFBooleanGetTypeID() && CFBooleanGetValue((CFBooleanRef)console);
   const bool is_locked =
      locked && CFGetTypeID(locked) == CFBooleanGetTypeID() && CFBooleanGetValue((CFBooleanRef)locked);
   CFRelease(session);
   if (!on_console) skip("the GUI session is not on the console");
   if (is_locked) skip("the screen is locked");
   if (CGDisplayIsAsleep(CGMainDisplayID())) skip("the main display is asleep");
}

id<MTLDevice> require_device()
{
   id<MTLDevice> device = MTLCreateSystemDefaultDevice();
   NSString *name = device ? NSStringFromClass([(id)device class]) : @"none";
   if (!device || ![name isEqualToString:@"NVMTLDevice"]) skip("no device of this driver (%s)", name.UTF8String);
   return device;
}

std::string real(const std::string &path)
{
   char buf[PATH_MAX];
   return realpath(path.c_str(), buf) ? std::string(buf) : std::string();
}

std::string folder_of(const std::string &path)
{
   size_t slash = path.find_last_of('/');
   return slash == std::string::npos ? std::string(".") : path.substr(0, slash);
}

std::string base_of(const std::string &path)
{
   size_t slash = path.find_last_of('/');
   return slash == std::string::npos ? path : path.substr(slash + 1);
}

// -> the name dyld gave the mapped image whose realpath is `want`, or ""
std::string mapped_as(const std::string &want)
{
   for (uint32_t i = 0; i < _dyld_image_count(); ++i) {
      const char *name = _dyld_get_image_name(i);
      if (!name || base_of(name) != base_of(want)) continue;
      if (real(name) == want) return name;
   }
   return "";
}

std::string image_of(const void *function)
{
   Dl_info i{};
   if (!function || !dladdr(function, &i) || !i.dli_fname) return "(none)";
   std::string r = real(i.dli_fname);
   return r.empty() ? std::string(i.dli_fname) : r;
}

// ---- Vulkan -------------------------------------------------------------------------------------------------------

struct Api {
   PFN_vkGetInstanceProcAddr GetInstanceProcAddr;
   PFN_vkCreateInstance CreateInstance;
   PFN_vkEnumerateInstanceExtensionProperties EnumerateInstanceExtensionProperties;
   PFN_vkEnumerateInstanceLayerProperties EnumerateInstanceLayerProperties;
   PFN_vkEnumerateInstanceVersion EnumerateInstanceVersion;
   PFN_vkDestroyInstance DestroyInstance;
   PFN_vkEnumeratePhysicalDevices EnumeratePhysicalDevices;
   PFN_vkGetPhysicalDeviceProperties GetPhysicalDeviceProperties;
   PFN_vkGetPhysicalDeviceMemoryProperties GetPhysicalDeviceMemoryProperties;
   PFN_vkGetPhysicalDeviceQueueFamilyProperties GetPhysicalDeviceQueueFamilyProperties;
   PFN_vkEnumerateDeviceExtensionProperties EnumerateDeviceExtensionProperties;
   PFN_vkCreateDevice CreateDevice;
   PFN_vkGetDeviceProcAddr GetDeviceProcAddr;
   PFN_vkCreateMetalSurfaceEXT CreateMetalSurfaceEXT;
   PFN_vkDestroySurfaceKHR DestroySurfaceKHR;
   PFN_vkGetPhysicalDeviceSurfaceSupportKHR GetPhysicalDeviceSurfaceSupportKHR;
   PFN_vkGetPhysicalDeviceSurfaceCapabilitiesKHR GetPhysicalDeviceSurfaceCapabilitiesKHR;
   PFN_vkGetPhysicalDeviceSurfaceCapabilities2KHR GetPhysicalDeviceSurfaceCapabilities2KHR;
   PFN_vkGetPhysicalDeviceSurfaceFormatsKHR GetPhysicalDeviceSurfaceFormatsKHR;
   PFN_vkGetPhysicalDeviceSurfaceFormats2KHR GetPhysicalDeviceSurfaceFormats2KHR;
   PFN_vkGetPhysicalDeviceSurfacePresentModesKHR GetPhysicalDeviceSurfacePresentModesKHR;
   PFN_vkGetPhysicalDevicePresentRectanglesKHR GetPhysicalDevicePresentRectanglesKHR;
   PFN_vkDestroyDevice DestroyDevice;
   PFN_vkGetDeviceQueue GetDeviceQueue;
   PFN_vkCreateCommandPool CreateCommandPool;
   PFN_vkDestroyCommandPool DestroyCommandPool;
   PFN_vkResetCommandPool ResetCommandPool;
   PFN_vkAllocateCommandBuffers AllocateCommandBuffers;
   PFN_vkBeginCommandBuffer BeginCommandBuffer;
   PFN_vkEndCommandBuffer EndCommandBuffer;
   PFN_vkCmdPipelineBarrier CmdPipelineBarrier;
   PFN_vkCmdClearColorImage CmdClearColorImage;
   PFN_vkQueueSubmit QueueSubmit;
   PFN_vkCreateSemaphore CreateSemaphore;
   PFN_vkDestroySemaphore DestroySemaphore;
   PFN_vkCreateFence CreateFence;
   PFN_vkDestroyFence DestroyFence;
   PFN_vkWaitForFences WaitForFences;
   PFN_vkResetFences ResetFences;
   PFN_vkCreateSwapchainKHR CreateSwapchainKHR;
   PFN_vkDestroySwapchainKHR DestroySwapchainKHR;
   PFN_vkGetSwapchainImagesKHR GetSwapchainImagesKHR;
   PFN_vkAcquireNextImageKHR AcquireNextImageKHR;
   PFN_vkQueuePresentKHR QueuePresentKHR;
} vk;

using GetStats = void (*)(uint64_t *);
using PresentObserver = void (*)(PFN_vkGetDeviceProcAddr, VkDevice, VkQueue, VkBuffer, uint32_t, uint32_t, uint32_t,
                                 uint32_t);
using SetObserver = void (*)(PresentObserver);
using Negotiate = VkResult (*)(uint32_t *);

const char *const kOwnedInstance[] = {VK_KHR_SURFACE_EXTENSION_NAME, VK_EXT_METAL_SURFACE_EXTENSION_NAME,
                                      VK_KHR_GET_SURFACE_CAPABILITIES_2_EXTENSION_NAME};
// The window-system extensions the frontend takes out of what the renderer offers and refuses (bridge.mm:160-172).
const char *const kHiddenInstance[] = {"VK_EXT_headless_surface", "VK_KHR_surface_maintenance1",
                                       "VK_EXT_surface_maintenance1"};
const char *const kHiddenDevice[] = {"VK_KHR_present_id", "VK_KHR_present_id2", "VK_KHR_present_wait",
                                     "VK_KHR_present_wait2", "VK_KHR_swapchain_maintenance1",
                                     "VK_EXT_swapchain_maintenance1", "VK_KHR_swapchain_mutable_format",
                                     "VK_KHR_incremental_present", "VK_EXT_present_timing", "VK_GOOGLE_display_timing",
                                     "VK_EXT_hdr_metadata", "VK_EXT_full_screen_exclusive", "VK_KHR_display_swapchain",
                                     "VK_EXT_display_control"};
// Entry points of those extensions, which a device with VK_KHR_swapchain answers with NULL (bridge.mm:173-179, :899).
const char *const kHiddenEntries[] = {"vkWaitForPresentKHR", "vkSetHdrMetadataEXT", "vkGetSwapchainStatusKHR",
                                      "vkReleaseSwapchainImagesKHR", "vkGetPastPresentationTimingGOOGLE",
                                      "vkGetRefreshCycleDurationGOOGLE"};

unsigned count_of(const std::vector<VkExtensionProperties> &list, const char *name)
{
   unsigned n = 0;
   for (auto &e : list)
      if (!strcmp(e.extensionName, name)) ++n;
   return n;
}

VkResult instance_extensions(PFN_vkEnumerateInstanceExtensionProperties f, std::vector<VkExtensionProperties> &out)
{
   uint32_t n = 0;
   VkResult r = f(nullptr, &n, nullptr);
   if (r != VK_SUCCESS) return r;
   out.resize(n);
   r = f(nullptr, &n, out.data());
   out.resize(n);
   return r;
}

VkResult device_extensions(PFN_vkEnumerateDeviceExtensionProperties f, VkPhysicalDevice p,
                           std::vector<VkExtensionProperties> &out)
{
   uint32_t n = 0;
   VkResult r = f(p, nullptr, &n, nullptr);
   if (r != VK_SUCCESS) return r;
   out.resize(n);
   r = f(p, nullptr, &n, out.data());
   out.resize(n);
   return r;
}

std::string joined(const std::vector<std::string> &names)
{
   std::string s;
   for (auto &n : names) s += (s.empty() ? "" : ", ") + n;
   return s.empty() ? std::string("none") : s;
}

// bridge.mm:288-307, limited_bar_memory, the same rule: true when no DEVICE_LOCAL|HOST_VISIBLE type lies on a heap as
// large as the largest DEVICE_LOCAL heap, and some system-memory type is HOST_VISIBLE|HOST_COHERENT.
bool limited_bar(const VkPhysicalDeviceMemoryProperties &m)
{
   VkDeviceSize vram = 0;
   for (uint32_t h = 0; h < m.memoryHeapCount; ++h)
      if (m.memoryHeaps[h].flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT) vram = std::max(vram, m.memoryHeaps[h].size);
   if (!vram) return false;
   bool system_uploads = false;
   for (uint32_t t = 0; t < m.memoryTypeCount; ++t) {
      const auto &type = m.memoryTypes[t];
      if (type.heapIndex >= m.memoryHeapCount) continue;
      const auto f = type.propertyFlags;
      const auto local_visible = VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT | VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT;
      const auto visible_coherent = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
      if ((f & local_visible) == local_visible && m.memoryHeaps[type.heapIndex].size >= vram) return false;
      if (!(f & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) && (f & visible_coherent) == visible_coherent)
         system_uploads = true;
   }
   return system_uploads;
}

// ---- the expected frame and the present observer ------------------------------------------------------------------

struct Colour {
   float value[4];   // the clear value (RGBA)
   int b, g, r;      // the bytes expected in the BGRA8 copy; alpha is always 255
};

int unorm_byte(float v)
{
   return (int)lround(std::min(1.0, std::max(0.0, (double)v)) * 255.0);
}

int srgb_byte(float v)
{
   const double l = std::min(1.0, std::max(0.0, (double)v));
   const double e = l <= 0.0031308 ? 12.92 * l : 1.055 * pow(l, 1.0 / 2.4) - 0.055;
   return (int)lround(e * 255.0);
}

Colour colour(float r, float g, float b, bool srgb)
{
   Colour c{{r, g, b, 1.0f}, 0, 0, 0};
   c.r = srgb ? srgb_byte(r) : unorm_byte(r);
   c.g = srgb ? srgb_byte(g) : unorm_byte(g);
   c.b = srgb ? srgb_byte(b) : unorm_byte(b);
   return c;
}

// smoke.mm's frame colour: ((f%60)/59, 0.2, 1-(f%60)/59, 1)
Colour frame_colour(unsigned f)
{
   const float x = (float)(f % 60) / 59.0f;
   return colour(x, 0.2f, 1.0f - x, false);
}

struct Expect {
   bool armed;
   uint32_t width, height;
   int b, g, r;
};

struct Observed {
   unsigned calls;
   uint64_t bytes;      // row * height of every call
   std::string problem; // the first
   bool stuck;          // a read-back copy did not complete: its objects are left as they are
};

std::mutex obs_mutex;
Expect expect_now;
Observed observed;
VkPhysicalDeviceMemoryProperties memory_properties;

// Reads the frontend's copy target back through the renderer's own device and queue, as smoke.mm:37-78 does, and
// compares every pixel. -> "" or what differs
std::string read_back(PFN_vkGetDeviceProcAddr gdpa, VkDevice dev, VkQueue q, VkBuffer source, uint32_t row,
                      uint32_t width, uint32_t height, uint32_t family, const Expect &e, bool &stuck)
{
#define OBS(name)                                                                                                     \
   auto name = (PFN_vk##name)gdpa(dev, "vk" #name);                                                                   \
   if (!name) return "the renderer answers no vk" #name
   OBS(CreateBuffer); OBS(DestroyBuffer); OBS(GetBufferMemoryRequirements); OBS(AllocateMemory); OBS(FreeMemory);
   OBS(BindBufferMemory); OBS(CreateCommandPool); OBS(DestroyCommandPool); OBS(AllocateCommandBuffers);
   OBS(BeginCommandBuffer); OBS(EndCommandBuffer); OBS(CmdPipelineBarrier); OBS(CmdCopyBuffer); OBS(QueueSubmit);
   OBS(CreateFence); OBS(DestroyFence); OBS(WaitForFences); OBS(MapMemory); OBS(UnmapMemory);
#undef OBS
   const VkDeviceSize size = (VkDeviceSize)row * height;
   VkBuffer target = VK_NULL_HANDLE;
   VkDeviceMemory memory = VK_NULL_HANDLE;
   VkCommandPool pool = VK_NULL_HANDLE;
   VkFence done = VK_NULL_HANDLE;
   auto work = [&]() -> std::string {
      VkBufferCreateInfo b{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
      b.size = size;
      b.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
      b.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
      VkResult r = CreateBuffer(dev, &b, nullptr, &target);
      if (r != VK_SUCCESS) return format("read-back vkCreateBuffer -> %d", (int)r);
      VkMemoryRequirements req{};
      GetBufferMemoryRequirements(dev, target, &req);
      const auto want = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
      uint32_t type = UINT32_MAX;
      for (uint32_t i = 0; i < memory_properties.memoryTypeCount; ++i) {
         const auto f = memory_properties.memoryTypes[i].propertyFlags;
         if (!(req.memoryTypeBits & (1u << i)) || (f & want) != want) continue;
         if (type == UINT32_MAX) type = i;
         if (f & VK_MEMORY_PROPERTY_HOST_CACHED_BIT) {
            type = i;
            break;
         }
      }
      if (type == UINT32_MAX) return "no host-visible coherent memory type for the read-back";
      VkMemoryAllocateInfo a{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
      a.allocationSize = req.size;
      a.memoryTypeIndex = type;
      r = AllocateMemory(dev, &a, nullptr, &memory);
      if (r == VK_SUCCESS) r = BindBufferMemory(dev, target, memory, 0);
      if (r != VK_SUCCESS) return format("read-back memory -> %d", (int)r);
      VkCommandPoolCreateInfo pc{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
      pc.queueFamilyIndex = family;
      r = CreateCommandPool(dev, &pc, nullptr, &pool);
      if (r != VK_SUCCESS) return format("read-back vkCreateCommandPool -> %d", (int)r);
      VkCommandBufferAllocateInfo ca{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
      ca.commandPool = pool;
      ca.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
      ca.commandBufferCount = 1;
      VkCommandBuffer cmd = VK_NULL_HANDLE;
      r = AllocateCommandBuffers(dev, &ca, &cmd);
      if (r != VK_SUCCESS) return format("read-back vkAllocateCommandBuffers -> %d", (int)r);
      VkCommandBufferBeginInfo begin{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
      begin.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
      r = BeginCommandBuffer(cmd, &begin);
      if (r != VK_SUCCESS) return format("read-back vkBeginCommandBuffer -> %d", (int)r);
      VkMemoryBarrier before{VK_STRUCTURE_TYPE_MEMORY_BARRIER};
      before.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
      before.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
      CmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 1, &before, 0,
                         nullptr, 0, nullptr);
      VkBufferCopy region{0, 0, size};
      CmdCopyBuffer(cmd, source, target, 1, &region);
      VkMemoryBarrier after{VK_STRUCTURE_TYPE_MEMORY_BARRIER};
      after.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
      after.dstAccessMask = VK_ACCESS_HOST_READ_BIT;
      CmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &after, 0, nullptr,
                         0, nullptr);
      r = EndCommandBuffer(cmd);
      if (r != VK_SUCCESS) return format("read-back vkEndCommandBuffer -> %d", (int)r);
      VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
      r = CreateFence(dev, &fi, nullptr, &done);
      if (r != VK_SUCCESS) return format("read-back vkCreateFence -> %d", (int)r);
      VkSubmitInfo s{VK_STRUCTURE_TYPE_SUBMIT_INFO};
      s.commandBufferCount = 1;
      s.pCommandBuffers = &cmd;
      r = QueueSubmit(q, 1, &s, done);
      if (r != VK_SUCCESS) return format("read-back vkQueueSubmit -> %d", (int)r);
      r = WaitForFences(dev, 1, &done, VK_TRUE, 5 * kSecond);
      if (r != VK_SUCCESS) {
         stuck = true;
         return format("the read-back copy did not complete within 5 s (vkWaitForFences -> %d)", (int)r);
      }
      void *data = nullptr;
      r = MapMemory(dev, memory, 0, VK_WHOLE_SIZE, 0, &data);
      if (r != VK_SUCCESS || !data) return format("read-back vkMapMemory -> %d", (int)r);
      const uint8_t *px = (const uint8_t *)data;
      uint64_t bad = 0;
      std::string first;
      for (uint32_t y = 0; y < height; ++y)
         for (uint32_t x = 0; x < width; ++x) {
            const uint8_t *p = px + (size_t)y * row + (size_t)x * 4;
            if (abs((int)p[0] - e.b) <= 1 && abs((int)p[1] - e.g) <= 1 && abs((int)p[2] - e.r) <= 1 && p[3] == 255)
               continue;
            if (!bad++)
               first = format("pixel x=%u y=%u got=BGRA %u,%u,%u,%u expected=%d,%d,%d,255 (each within 1)", x, y, p[0],
                              p[1], p[2], p[3], e.b, e.g, e.r);
         }
      UnmapMemory(dev, memory);
      if (bad) return first + format(", %llu of %llu pixels differ", (unsigned long long)bad,
                                     (unsigned long long)width * height);
      return "";
   };
   std::string why = work();
   if (!stuck) {
      if (pool) DestroyCommandPool(dev, pool, nullptr);
      if (done) DestroyFence(dev, done, nullptr);
      if (target) DestroyBuffer(dev, target, nullptr);
      if (memory) FreeMemory(dev, memory, nullptr);
   }
   return why;
}

// Called by the frontend after each completed copy, on the thread that presents (bridge.mm:778).
void observer(PFN_vkGetDeviceProcAddr gdpa, VkDevice dev, VkQueue q, VkBuffer source, uint32_t row, uint32_t width,
              uint32_t height, uint32_t family)
{
   std::lock_guard<std::mutex> lock(obs_mutex);
   ++observed.calls;
   observed.bytes += (uint64_t)row * height;
   const Expect e = expect_now;
   std::string why;
   if (!e.armed) why = "the present observer ran outside a present of this program";
   else if (width != e.width || height != e.height)
      why = format("observer size got=%ux%u expected=%ux%u", width, height, e.width, e.height);
   else if (row % 4 || row < 4ull * width)
      why = format("observer row pitch got=%u expected a multiple of 4 and at least %u", row, 4 * width);
   else if (observed.calls == 1 && !observed.stuck)
      why = read_back(gdpa, dev, q, source, row, width, height, family, e, observed.stuck);
   if (!why.empty() && observed.problem.empty()) observed.problem = why;
}

// ---- the drawables the layer hands out ----------------------------------------------------------------------------

struct Tag {
   uint32_t magic, seed, generation;
};

bool read_tag(IOSurfaceRef s, Tag &t)
{
   CFTypeRef v = IOSurfaceCopyValue(s, CFSTR("NVMTLVramAt:p0"));
   const bool ok = v && CFGetTypeID(v) == CFDataGetTypeID() && CFDataGetLength((CFDataRef)v) == (CFIndex)sizeof t;
   if (ok) memcpy(&t, CFDataGetBytePtr((CFDataRef)v), sizeof t);
   if (v) CFRelease(v);
   return ok;
}

struct Vend {                   // one nextDrawable of the layer, as it returned
   bool drawable;               // not nil
   uint32_t slot;               // its index among the drawables handed out
   IOSurfaceRef surface;        // retained until the frame's checks are done
   uint32_t width, height;
   MTLPixelFormat format;
   uint64_t registry;
   bool prior;                  // the surface had a tag with the magic before this present
   uint32_t prior_generation;
};

std::mutex vend_mutex;
std::vector<Vend> vends;        // since the present began
std::atomic<uint32_t> vended{0};
std::atomic<uint8_t> slot_handled[kSlots], slot_shown[kSlots];   // presented handler called; presentedTime > 0

void note_drawable(id<CAMetalDrawable> d)
{
   Vend v{};
   v.slot = UINT32_MAX;
   if (d) {
      v.drawable = true;
      const uint32_t slot = vended.fetch_add(1);
      v.slot = slot;
      id<MTLTexture> t = d.texture;
      v.width = (uint32_t)t.width;
      v.height = (uint32_t)t.height;
      v.format = t.pixelFormat;
      v.registry = t.device.registryID;
      IOSurfaceRef s = t.iosurface;
      if (s) {
         v.surface = (IOSurfaceRef)CFRetain(s);
         Tag tag{};
         if (read_tag(s, tag) && tag.magic == kTagMagic) {
            v.prior = true;
            v.prior_generation = tag.generation;
         }
      }
      // The block keeps nothing but the slot number: a drawable held past its present starves the layer's pool.
      if (slot < kSlots) [d addPresentedHandler:^(id<MTLDrawable> shown) {
         slot_shown[slot] = shown.presentedTime > 0 ? 1 : 0;
         slot_handled[slot] = 1;
      }];
   }
   std::lock_guard<std::mutex> lock(vend_mutex);
   vends.push_back(v);
}

} // namespace

@interface WCLayer : CAMetalLayer
@end
@implementation WCLayer
- (id<CAMetalDrawable>)nextDrawable
{
   id<CAMetalDrawable> drawable = [super nextDrawable];
   note_drawable(drawable);
   return drawable;
}
@end

@interface WCView : NSView
@end
@implementation WCView
- (CALayer *)makeBackingLayer
{
   return [WCLayer layer];
}
- (BOOL)wantsUpdateLayer
{
   return YES;
}
- (void)updateLayer
{
}
@end

namespace {

// ---- the window ---------------------------------------------------------------------------------------------------

NSWindow *window;
WCView *view;
WCLayer *layer;
id<MTLDevice> card;

void drain()
{
   @autoreleasepool {
      for (;;) {
         NSEvent *e = [NSApp nextEventMatchingMask:NSEventMaskAny untilDate:[NSDate distantPast]
                                            inMode:NSDefaultRunLoopMode dequeue:YES];
         if (!e) break;
         [NSApp sendEvent:e];
      }
   }
}

void pump(double seconds)
{
   const double end = now() + seconds;
   do {
      @autoreleasepool {
         NSEvent *e = [NSApp nextEventMatchingMask:NSEventMaskAny
                                         untilDate:[NSDate dateWithTimeIntervalSinceNow:0.005]
                                            inMode:NSDefaultRunLoopMode dequeue:YES];
         if (e) [NSApp sendEvent:e];
      }
   } while (now() < end);
}

template <class F> bool pump_until(F done, double seconds)
{
   const double end = now() + seconds;
   while (!done() && now() < end) pump(0.01);
   return done();
}

bool visible()
{
   return (window.occlusionState & NSWindowOcclusionStateVisible) != 0;
}

void open_window()
{
   [NSApplication sharedApplication];
   [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
   [NSApp finishLaunching];
   const NSWindowStyleMask style = (NSWindowStyleMask)(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                                                       NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable);
   window = [[NSWindow alloc] initWithContentRect:NSMakeRect(96, 96, 640, 360) styleMask:style
                                          backing:NSBackingStoreBuffered defer:NO];
   if (!window) setup_error("no NSWindow");
   window.releasedWhenClosed = NO;
   window.restorable = NO;
   window.level = NSFloatingWindowLevel;
   window.ignoresMouseEvents = YES;
   window.title = @"native Vulkan card check";
   view = [[WCView alloc] initWithFrame:NSMakeRect(0, 0, 640, 360)];
   view.wantsLayer = YES;
   window.contentView = view;
   if (![view.layer isKindOfClass:[WCLayer class]]) setup_error("the view's backing layer is not the program's layer");
   layer = (WCLayer *)view.layer;
   layer.contentsScale = window.backingScaleFactor;
   [window orderFrontRegardless];
   if (!pump_until(visible, 3.0))
      skip("the window did not become visible within 3 s (occlusionState 0x%lx)", (unsigned long)window.occlusionState);
}

void close_window()
{
   [window orderOut:nil];
   [window close];
   pump(0.05);
}

// what the frontend computes as the surface's current extent (bridge.mm:469-471)
VkExtent2D layer_extent()
{
   const CGSize size = layer.bounds.size;
   const CGFloat scale = layer.contentsScale;
   return VkExtent2D{(uint32_t)std::max(0.0, (double)(size.width * scale)),
                     (uint32_t)std::max(0.0, (double)(size.height * scale))};
}

// ---- the run ------------------------------------------------------------------------------------------------------

bool loader_mode;
std::string frontend_real, renderer_real;
void *entry_lib, *frontend_lib;
GetStats get_stats;
SetObserver set_observer;
VkInstance instance;
VkPhysicalDevice physical;
VkDevice device;
VkQueue queue;
uint32_t family = UINT32_MAX;
VkSurfaceKHR surface;
VkCommandPool pool;
VkCommandBuffer commands;
VkSemaphore acquired, rendered;
VkFence fence;
uint64_t stats_start[7];

struct Chain {
   VkSwapchainKHR handle = VK_NULL_HANDLE;
   VkExtent2D extent{};
   VkFormat format = VK_FORMAT_UNDEFINED;
   std::vector<VkImage> images;
   std::vector<bool> presented;   // the image's layout is PRESENT_SRC
};

struct Spec {
   VkPresentModeKHR mode;
   VkFormat format;
   uint32_t images;
   VkCompositeAlphaFlagBitsKHR alpha;
};

VkSwapchainCreateInfoKHR chain_info(const Spec &s, VkExtent2D extent, VkSwapchainKHR old)
{
   VkSwapchainCreateInfoKHR sc{VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR};
   sc.surface = surface;
   sc.minImageCount = s.images;
   sc.imageFormat = s.format;
   sc.imageColorSpace = VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
   sc.imageExtent = extent;
   sc.imageArrayLayers = 1;
   sc.imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
   sc.imageSharingMode = VK_SHARING_MODE_EXCLUSIVE;
   sc.preTransform = VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR;
   sc.compositeAlpha = s.alpha;
   sc.presentMode = s.mode;
   sc.clipped = VK_TRUE;
   sc.oldSwapchain = old;
   return sc;
}

// -> "" or what failed
std::string make_chain(const Spec &s, VkSwapchainKHR old, Chain &out)
{
   VkSurfaceCapabilitiesKHR caps{};
   VkResult r = vk.GetPhysicalDeviceSurfaceCapabilitiesKHR(physical, surface, &caps);
   if (r != VK_SUCCESS) return format("vkGetPhysicalDeviceSurfaceCapabilitiesKHR -> %d", (int)r);
   if (!caps.currentExtent.width || caps.currentExtent.width == UINT32_MAX) return "the surface has no current extent";
   VkSwapchainCreateInfoKHR sc = chain_info(s, caps.currentExtent, old);
   Chain c;
   c.extent = caps.currentExtent;
   c.format = s.format;
   r = vk.CreateSwapchainKHR(device, &sc, nullptr, &c.handle);
   if (r != VK_SUCCESS) return format("vkCreateSwapchainKHR -> %d", (int)r);
   uint32_t n = 0;
   r = vk.GetSwapchainImagesKHR(device, c.handle, &n, nullptr);
   if (r == VK_SUCCESS) {
      c.images.resize(n);
      r = vk.GetSwapchainImagesKHR(device, c.handle, &n, c.images.data());
      c.images.resize(n);
   }
   if (r != VK_SUCCESS) {
      vk.DestroySwapchainKHR(device, c.handle, nullptr);
      return format("vkGetSwapchainImagesKHR -> %d", (int)r);
   }
   c.presented.assign(c.images.size(), false);
   out = c;
   return "";
}

// Replaces the chain by a new one made with it as oldSwapchain, then destroys it: every frame of it has completed.
void replace_chain(Chain &c, const Spec &s, const char *why)
{
   Chain next;
   std::string e = make_chain(s, c.handle, next);
   if (!e.empty()) fatal("recreate", "%s: %s", why, e.c_str());
   vk.DestroySwapchainKHR(device, c.handle, nullptr);
   c = next;
}

void check_layer(const char *name, const Chain &c, MTLPixelFormat format_wanted, NSUInteger slots, BOOL sync,
                 BOOL opaque, size_t images)
{
   std::vector<std::string> bad;
   if (!layer.device || layer.device.registryID != card.registryID)
      bad.push_back(format("device registryID got=0x%llx expected=0x%llx",
                           (unsigned long long)(layer.device ? layer.device.registryID : 0),
                           (unsigned long long)card.registryID));
   if (layer.pixelFormat != format_wanted)
      bad.push_back(format("pixelFormat got=%lu expected=%lu", (unsigned long)layer.pixelFormat,
                           (unsigned long)format_wanted));
   if (layer.framebufferOnly) bad.push_back("framebufferOnly got=YES expected=NO");
   if (!layer.allowsNextDrawableTimeout) bad.push_back("allowsNextDrawableTimeout got=NO expected=YES");
   if (layer.maximumDrawableCount != slots)
      bad.push_back(format("maximumDrawableCount got=%lu expected=%lu", (unsigned long)layer.maximumDrawableCount,
                           (unsigned long)slots));
   if (layer.drawableSize.width != c.extent.width || layer.drawableSize.height != c.extent.height)
      bad.push_back(format("drawableSize got=%gx%g expected=%ux%u", (double)layer.drawableSize.width,
                           (double)layer.drawableSize.height, c.extent.width, c.extent.height));
   if (layer.displaySyncEnabled != sync)
      bad.push_back(format("displaySyncEnabled got=%d expected=%d", (int)layer.displaySyncEnabled, (int)sync));
   if (layer.opaque != opaque) bad.push_back(format("opaque got=%d expected=%d", (int)layer.opaque, (int)opaque));
   if (c.images.size() != images)
      bad.push_back(format("swapchain images got=%zu expected=%zu", c.images.size(), images));
   check(bad.empty(), name,
         format("%ux%u, %zu images; the layer: the card's device, pixelFormat %lu, framebufferOnly NO, "
                "allowsNextDrawableTimeout YES, %lu drawables, drawableSize as the extent, displaySyncEnabled %d, "
                "opaque %d", c.extent.width, c.extent.height, images, (unsigned long)format_wanted,
                (unsigned long)slots, (int)sync, (int)opaque),
         joined(bad));
}

// ---- frames -------------------------------------------------------------------------------------------------------

enum Phase : uint8_t { kNone, kFifo, kResized, kMinimized, kRestored, kImmediate, kEight };
uint8_t slot_phase[kSlots], slot_ok[kSlots];   // main thread only
unsigned frame_counter, success_presents;
uint64_t observed_bytes;
bool cpu_off;            // a CPU lock took longer than 1 s: no more CPU reads
double cpu_lock_max;
unsigned cpu_reads_total;                                    // CPU reads of drawables so far
unsigned first_import_failure = UINT_MAX, cpu_reads_before;  // the first frame whose present counted a refused import

struct Tally {
   const char *name;
   unsigned presents = 0, ok = 0, out_of_date = 0, cpu_reads = 0, problems = 0;
   std::string first;
   double slowest = 0;
   explicit Tally(const char *n) : name(n) {}
   void problem(unsigned frame, const std::string &what)
   {
      if (!problems++) first = format("frame %u: ", frame) + what;
   }
};

struct Presented {
   VkResult result;
   double seconds;   // vkQueuePresentKHR alone
};

// The CPU's view of the drawable after the frontend's mark: a read-only lock pages the VRAM off
// (plugin/nvmtl_vk.c:2916), so it is timed, and the corners and the centre must hold the frame's colour.
void cpu_read(IOSurfaceRef s, VkExtent2D e, const Colour &col, unsigned frame, Tally &t)
{
   const double t0 = now();
   const kern_return_t k = IOSurfaceLock(s, kIOSurfaceLockReadOnly, nullptr);
   const double took = now() - t0;
   cpu_lock_max = std::max(cpu_lock_max, took);
   if (took > 1.0) {
      cpu_off = true;
      t.problem(frame, format("a read-only IOSurfaceLock took %.3f s expected<=1 s (no further CPU reads)", took));
   }
   if (k != KERN_SUCCESS) {
      t.problem(frame, format("IOSurfaceLock(read-only) -> 0x%x", (unsigned)k));
      return;
   }
   ++t.cpu_reads;
   ++cpu_reads_total;
   const uint8_t *base = (const uint8_t *)IOSurfaceGetBaseAddress(s);
   const size_t row = IOSurfaceGetBytesPerRow(s);
   if (!base || row < 4ull * e.width || IOSurfaceGetHeight(s) < e.height) {
      t.problem(frame, format("the locked IOSurface has base %p, row %zu, height %zu for %ux%u", (const void *)base,
                              row, IOSurfaceGetHeight(s), e.width, e.height));
   } else {
      const uint32_t xs[5] = {0, e.width - 1, 0, e.width - 1, e.width / 2};
      const uint32_t ys[5] = {0, 0, e.height - 1, e.height - 1, e.height / 2};
      for (int i = 0; i < 5; ++i) {
         const uint8_t *p = base + (size_t)ys[i] * row + (size_t)xs[i] * 4;
         if (abs((int)p[0] - col.b) > 1 || abs((int)p[1] - col.g) > 1 || abs((int)p[2] - col.r) > 1 || p[3] != 255) {
            t.problem(frame, format("CPU read x=%u y=%u got=BGRA %u,%u,%u,%u expected=%d,%d,%d,255 (each within 1)",
                                    xs[i], ys[i], p[0], p[1], p[2], p[3], col.b, col.g, col.r));
            break;
         }
      }
   }
   IOSurfaceUnlock(s, kIOSurfaceLockReadOnly, nullptr);
}

// One frame: acquire (2 s), clear to the colour, submit, present, then the frame's checks. allow_out_of_date: that
// result is counted, not failed.
Presented present_frame(Chain &c, const Colour &col, Phase phase, Tally &t, bool allow_out_of_date, bool cpu)
{
   const unsigned frame = frame_counter++;
   uint32_t index = 0;
   VkResult r = vk.AcquireNextImageKHR(device, c.handle, 2 * kSecond, acquired, VK_NULL_HANDLE, &index);
   if (r != VK_SUCCESS)
      fatal("acquire", "%s frame %u: vkAcquireNextImageKHR got=%d expected=0 within 2 s", t.name, frame, (int)r);
   r = vk.ResetCommandPool(device, pool, 0);
   VkCommandBufferBeginInfo begin{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
   begin.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
   if (r == VK_SUCCESS) r = vk.BeginCommandBuffer(commands, &begin);
   if (r != VK_SUCCESS) fatal("record", "%s frame %u: -> %d", t.name, frame, (int)r);
   VkImageMemoryBarrier barrier{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
   barrier.oldLayout = c.presented[index] ? VK_IMAGE_LAYOUT_PRESENT_SRC_KHR : VK_IMAGE_LAYOUT_UNDEFINED;
   barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
   barrier.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
   barrier.srcQueueFamilyIndex = barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
   barrier.image = c.images[index];
   barrier.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
   vk.CmdPipelineBarrier(commands, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0,
                         nullptr, 1, &barrier);
   VkClearColorValue clear{};
   memcpy(clear.float32, col.value, sizeof clear.float32);
   vk.CmdClearColorImage(commands, c.images[index], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &clear, 1,
                         &barrier.subresourceRange);
   barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
   barrier.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
   barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
   barrier.dstAccessMask = 0;
   vk.CmdPipelineBarrier(commands, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0, nullptr,
                         0, nullptr, 1, &barrier);
   r = vk.EndCommandBuffer(commands);
   if (r != VK_SUCCESS) fatal("record", "%s frame %u: vkEndCommandBuffer -> %d", t.name, frame, (int)r);
   const VkPipelineStageFlags stage = VK_PIPELINE_STAGE_TRANSFER_BIT;
   VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};
   submit.waitSemaphoreCount = 1;
   submit.pWaitSemaphores = &acquired;
   submit.pWaitDstStageMask = &stage;
   submit.commandBufferCount = 1;
   submit.pCommandBuffers = &commands;
   submit.signalSemaphoreCount = 1;
   submit.pSignalSemaphores = &rendered;
   r = vk.QueueSubmit(queue, 1, &submit, fence);
   if (r != VK_SUCCESS) fatal("submit", "%s frame %u: vkQueueSubmit -> %d", t.name, frame, (int)r);

   {
      std::lock_guard<std::mutex> lock(vend_mutex);
      vends.clear();
   }
   {
      std::lock_guard<std::mutex> lock(obs_mutex);
      const bool stuck = observed.stuck;
      observed = Observed{};
      observed.stuck = stuck;
      expect_now = Expect{true, c.extent.width, c.extent.height, col.b, col.g, col.r};
   }
   VkResult result = VK_RESULT_MAX_ENUM;
   VkPresentInfoKHR p{VK_STRUCTURE_TYPE_PRESENT_INFO_KHR};
   p.waitSemaphoreCount = 1;
   p.pWaitSemaphores = &rendered;
   p.swapchainCount = 1;
   p.pSwapchains = &c.handle;
   p.pImageIndices = &index;
   p.pResults = &result;
   uint64_t stats_before[7]{}, stats_after[7]{};
   get_stats(stats_before);
   const double t0 = now();
   const VkResult overall = vk.QueuePresentKHR(queue, &p);
   const double took = now() - t0;
   get_stats(stats_after);
   if (stats_after[4] > stats_before[4] && first_import_failure == UINT_MAX) {
      first_import_failure = frame;
      cpu_reads_before = cpu_reads_total;
   }
   Observed o;
   {
      std::lock_guard<std::mutex> lock(obs_mutex);
      expect_now.armed = false;
      o = observed;
   }
   if (o.stuck) fatal("observer", "%s frame %u: %s", t.name, frame, o.problem.c_str());
   if (overall == VK_ERROR_DEVICE_LOST || overall == VK_ERROR_SURFACE_LOST_KHR)
      fatal("present", "%s frame %u: vkQueuePresentKHR got=%d expected=0", t.name, frame, (int)overall);
   r = vk.WaitForFences(device, 1, &fence, VK_TRUE, 5 * kSecond);
   if (r != VK_SUCCESS) fatal("render-fence", "%s frame %u: the clear did not complete within 5 s (%d)", t.name, frame,
                              (int)r);
   vk.ResetFences(device, 1, &fence);
   c.presented[index] = true;
   drain();
   ++t.presents;
   t.slowest = std::max(t.slowest, took);
   std::vector<Vend> seen;
   {
      std::lock_guard<std::mutex> lock(vend_mutex);
      seen.swap(vends);
   }
   auto release = [&] {
      for (auto &v : seen)
         if (v.surface) CFRelease(v.surface);
   };
   if (overall != result)
      t.problem(frame, format("vkQueuePresentKHR got=%d but pResults got=%d", (int)overall, (int)result));
   if (seen.size() != 1) t.problem(frame, format("nextDrawable calls got=%zu expected=1", seen.size()));
   if (overall == VK_ERROR_OUT_OF_DATE_KHR && allow_out_of_date) {
      ++t.out_of_date;
      if (o.calls) t.problem(frame, "the present observer ran for an out-of-date present");
      release();
      return Presented{overall, took};
   }
   if (overall != VK_SUCCESS) {
      t.problem(frame, format("vkQueuePresentKHR got=%d expected=0", (int)overall));
      release();
      return Presented{overall, took};
   }
   ++t.ok;
   ++success_presents;
   observed_bytes += o.bytes;
   if (seen.size() == 1) {
      const Vend &v = seen[0];
      const MTLPixelFormat want = c.format == VK_FORMAT_B8G8R8A8_SRGB ? MTLPixelFormatBGRA8Unorm_sRGB
                                                                      : MTLPixelFormatBGRA8Unorm;
      Tag tag{};
      const uint32_t generation = v.prior ? v.prior_generation + 1 : 0;   // bridge.mm:674-684
      if (!v.drawable) t.problem(frame, "the present succeeded but nextDrawable gave nil");
      else if (v.width != c.extent.width || v.height != c.extent.height)
         t.problem(frame, format("drawable texture got=%ux%u expected=%ux%u", v.width, v.height, c.extent.width,
                                 c.extent.height));
      else if (v.format != want)
         t.problem(frame, format("drawable pixelFormat got=%lu expected=%lu", (unsigned long)v.format,
                                 (unsigned long)want));
      else if (v.registry != card.registryID)
         t.problem(frame, format("drawable device registryID got=0x%llx expected=0x%llx",
                                 (unsigned long long)v.registry, (unsigned long long)card.registryID));
      else if (!v.surface) t.problem(frame, "the drawable's texture has no IOSurface");
      else if (!read_tag(v.surface, tag) || tag.magic != kTagMagic || tag.generation != generation)
         t.problem(frame, format("NVMTLVramAt:p0 got=magic 0x%x generation %u expected=0x%x generation %u", tag.magic,
                                 tag.generation, kTagMagic, generation));
      if (v.drawable && v.slot < kSlots) {
         slot_phase[v.slot] = phase;
         slot_ok[v.slot] = 1;
      }
      if (cpu && !cpu_off && v.surface) cpu_read(v.surface, c.extent, col, frame, t);
   }
   if (o.calls != 1) t.problem(frame, format("present observer calls got=%u expected=1", o.calls));
   else if (!o.problem.empty()) t.problem(frame, o.problem);
   release();
   return Presented{VK_SUCCESS, took};
}

void report(const Tally &t, const char *what)
{
   if (!t.problems)
      pass(t.name, "%u presents%s%s; every copy read back through the present observer with every pixel as cleared, "
           "the drawable's size, format, device and NVMTLVramAt:p0 generation as expected, %u CPU reads as cleared; "
           "slowest present %.1f ms", t.ok, *what ? ", " : "", what, t.cpu_reads, t.slowest * 1e3);
   else
      fail(t.name, "%s (%u problems; %u of %u presents succeeded, %u out of date)", t.first.c_str(), t.problems, t.ok,
           t.presents, t.out_of_date);
}

void frames(Chain &c, unsigned n, Phase phase, Tally &t, bool srgb_pair)
{
   for (unsigned i = 0; i < n; ++i) {
      @autoreleasepool {
         // sRGB: two colours in turn, so that a stale frame shows; (1, 0.5, 0) is stored as B=0, G=188, R=255
         const Colour col = !srgb_pair            ? frame_colour(frame_counter)
                            : frame_counter % 2 ? colour(0.0f, 0.5f, 1.0f, true)
                                                : colour(1.0f, 0.5f, 0.0f, true);
         present_frame(c, col, phase, t, false, i % 10 == 9);
      }
   }
}

// ---- the checks before the first frame ----------------------------------------------------------------------------

void load_instance_level(const char *lib)
{
   entry_lib = dlopen(lib, RTLD_NOW | RTLD_LOCAL | RTLD_FIRST);
   if (!entry_lib) fatal("load", "dlopen %s: %s", lib, dlerror());
   vk.GetInstanceProcAddr = (PFN_vkGetInstanceProcAddr)dlsym(entry_lib, "vkGetInstanceProcAddr");
   if (!vk.GetInstanceProcAddr) fatal("load", "%s exports no vkGetInstanceProcAddr", lib);
#define GLOBAL(name) vk.name = (PFN_vk##name)vk.GetInstanceProcAddr(VK_NULL_HANDLE, "vk" #name)
   GLOBAL(CreateInstance);
   GLOBAL(EnumerateInstanceExtensionProperties);
   GLOBAL(EnumerateInstanceLayerProperties);
   GLOBAL(EnumerateInstanceVersion);
#undef GLOBAL
   if (!vk.CreateInstance || !vk.EnumerateInstanceExtensionProperties || !vk.EnumerateInstanceLayerProperties ||
       !vk.EnumerateInstanceVersion)
      fatal("load", "vkGetInstanceProcAddr(NULL, ...) gave NULL for one of the four global functions");
   if (!loader_mode) {
      // LWJGL and SDL take vkGetInstanceProcAddr from the library and compare it with what it answers for its own name.
      void *self = (void *)vk.GetInstanceProcAddr(VK_NULL_HANDLE, "vkGetInstanceProcAddr");
      check(self == (void *)vk.GetInstanceProcAddr, "provider",
            "the exported vkGetInstanceProcAddr is the one it answers for its own name",
            format("vkGetInstanceProcAddr(NULL, \"vkGetInstanceProcAddr\") got=%p expected=%p (the export)", self,
                   (void *)vk.GetInstanceProcAddr));
      Negotiate negotiate = (Negotiate)dlsym(entry_lib, "vk_icdNegotiateLoaderICDInterfaceVersion");
      const bool exports =
         dlsym(entry_lib, "vk_icdGetInstanceProcAddr") && dlsym(entry_lib, "vk_icdGetPhysicalDeviceProcAddr");
      uint32_t high = 7, low = 2;
      const VkResult a = negotiate ? negotiate(&high) : VK_ERROR_INITIALIZATION_FAILED;
      const VkResult b = negotiate ? negotiate(&low) : VK_SUCCESS;
      check(negotiate && exports && a == VK_SUCCESS && high == 5 && b == VK_ERROR_INCOMPATIBLE_DRIVER, "icd-interface",
            "exports the loader's three ICD entries; offered 7 it negotiates 5, offered 2 it refuses",
            format("exports %s; offered 7 got=%d version %u expected=0 version 5; offered 2 got=%d expected=%d",
                   negotiate && exports ? "all three" : "not all three ICD entries", (int)a, high, (int)b,
                   (int)VK_ERROR_INCOMPATIBLE_DRIVER));
   }
   std::vector<VkExtensionProperties> ext;
   VkResult r = instance_extensions(vk.EnumerateInstanceExtensionProperties, ext);
   if (r != VK_SUCCESS) fatal("instance-extensions", "vkEnumerateInstanceExtensionProperties -> %d", (int)r);
   std::vector<std::string> bad;
   for (const char *n : kOwnedInstance) {
      const unsigned k = count_of(ext, n);
      if (loader_mode ? k < 1 : k != 1) bad.push_back(format("%s listed %u times", n, k));
   }
   for (const char *n : kHiddenInstance)
      if (count_of(ext, n)) bad.push_back(format("%s listed", n));
   check(bad.empty(), "instance-extensions",
         format("%zu offered; VK_KHR_surface, VK_EXT_metal_surface and VK_KHR_get_surface_capabilities2 %s; the 3 "
                "hidden ones absent", ext.size(), loader_mode ? "present" : "once each"),
         joined(bad));
   uint32_t layers = 0;
   r = vk.EnumerateInstanceLayerProperties(&layers, nullptr);
   check(r == VK_SUCCESS && layers == 0, "layers", "no instance layer",
         format("vkEnumerateInstanceLayerProperties -> %d, layers got=%u expected=0", (int)r, layers));
   uint32_t api = 0;
   vk.EnumerateInstanceVersion(&api);
   info("instance version %u.%u.%u", VK_API_VERSION_MAJOR(api), VK_API_VERSION_MINOR(api), VK_API_VERSION_PATCH(api));
}

void make_instance()
{
   VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
   app.pApplicationName = "native Vulkan window check";
   app.apiVersion = VK_API_VERSION_1_3;
   VkInstanceCreateInfo ci{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
   ci.pApplicationInfo = &app;
   const char *headless[] = {"VK_EXT_headless_surface"};
   ci.enabledExtensionCount = 1;
   ci.ppEnabledExtensionNames = headless;
   VkInstance refused = VK_NULL_HANDLE;
   VkResult r = vk.CreateInstance(&ci, nullptr, &refused);
   if (r == VK_SUCCESS) {
      auto destroy = (PFN_vkDestroyInstance)vk.GetInstanceProcAddr(refused, "vkDestroyInstance");
      if (destroy) destroy(refused, nullptr);
   }
   check(r == VK_ERROR_EXTENSION_NOT_PRESENT, "instance-refusal",
         "an instance asking for VK_EXT_headless_surface is refused with VK_ERROR_EXTENSION_NOT_PRESENT",
         format("vkCreateInstance with VK_EXT_headless_surface got=%d expected=%d", (int)r,
                (int)VK_ERROR_EXTENSION_NOT_PRESENT));
   ci.enabledExtensionCount = 3;
   ci.ppEnabledExtensionNames = kOwnedInstance;
   r = vk.CreateInstance(&ci, nullptr, &instance);
   if (r != VK_SUCCESS) fatal("instance", "vkCreateInstance (API 1.3, the three surface extensions) -> %d", (int)r);
   pass("instance", "API 1.3 with VK_KHR_surface, VK_EXT_metal_surface and VK_KHR_get_surface_capabilities2");
#define INST(name) vk.name = (PFN_vk##name)vk.GetInstanceProcAddr(instance, "vk" #name)
   INST(DestroyInstance); INST(EnumeratePhysicalDevices); INST(GetPhysicalDeviceProperties);
   INST(GetPhysicalDeviceMemoryProperties); INST(GetPhysicalDeviceQueueFamilyProperties);
   INST(EnumerateDeviceExtensionProperties); INST(CreateDevice); INST(GetDeviceProcAddr); INST(CreateMetalSurfaceEXT);
   INST(DestroySurfaceKHR); INST(GetPhysicalDeviceSurfaceSupportKHR); INST(GetPhysicalDeviceSurfaceCapabilitiesKHR);
   INST(GetPhysicalDeviceSurfaceCapabilities2KHR); INST(GetPhysicalDeviceSurfaceFormatsKHR);
   INST(GetPhysicalDeviceSurfaceFormats2KHR); INST(GetPhysicalDeviceSurfacePresentModesKHR);
   INST(GetPhysicalDevicePresentRectanglesKHR);
#undef INST
   const void *all[] = {(void *)vk.DestroyInstance, (void *)vk.EnumeratePhysicalDevices,
                        (void *)vk.GetPhysicalDeviceProperties, (void *)vk.GetPhysicalDeviceMemoryProperties,
                        (void *)vk.GetPhysicalDeviceQueueFamilyProperties,
                        (void *)vk.EnumerateDeviceExtensionProperties,
                        (void *)vk.CreateDevice, (void *)vk.GetDeviceProcAddr, (void *)vk.CreateMetalSurfaceEXT,
                        (void *)vk.DestroySurfaceKHR, (void *)vk.GetPhysicalDeviceSurfaceSupportKHR,
                        (void *)vk.GetPhysicalDeviceSurfaceCapabilitiesKHR,
                        (void *)vk.GetPhysicalDeviceSurfaceCapabilities2KHR,
                        (void *)vk.GetPhysicalDeviceSurfaceFormatsKHR, (void *)vk.GetPhysicalDeviceSurfaceFormats2KHR,
                        (void *)vk.GetPhysicalDeviceSurfacePresentModesKHR,
                        (void *)vk.GetPhysicalDevicePresentRectanglesKHR};
   for (const void *f : all)
      if (!f) fatal("instance-entries", "vkGetInstanceProcAddr gave NULL for one of the 17 instance functions it uses");
}

void find_frontend()
{
   if (loader_mode) {
      const std::string name = mapped_as(frontend_real);
      if (name.empty()) fatal("loader-route", "after vkCreateInstance no mapped image is %s", frontend_real.c_str());
      frontend_lib = dlopen(name.c_str(), RTLD_NOW | RTLD_LOCAL | RTLD_NOLOAD | RTLD_FIRST);
      if (!frontend_lib) fatal("loader-route", "dlopen(%s, RTLD_NOLOAD): %s", name.c_str(), dlerror());
      pass("loader-route", "the loader mapped the frontend as %s", name.c_str());
   } else {
      frontend_lib = entry_lib;
   }
   get_stats = (GetStats)dlsym(frontend_lib, "nvkbridgeGetStats");
   set_observer = (SetObserver)dlsym(frontend_lib, "nvkbridgeSetPresentObserver");
   if (!get_stats || !set_observer)
      fatal("test-hooks", "the frontend exports no nvkbridgeGetStats or no nvkbridgeSetPresentObserver");
   get_stats(stats_start);
   set_observer(&observer);
}

void pick_physical()
{
   uint32_t n = 0;
   VkResult r = vk.EnumeratePhysicalDevices(instance, &n, nullptr);
   if (r != VK_SUCCESS || !n) fatal("physical-device", "vkEnumeratePhysicalDevices -> %d, %u devices", (int)r, n);
   std::vector<VkPhysicalDevice> list(n);
   r = vk.EnumeratePhysicalDevices(instance, &n, list.data());
   if (r != VK_SUCCESS || !n) fatal("physical-device", "vkEnumeratePhysicalDevices -> %d", (int)r);
   physical = list[0];
   VkPhysicalDeviceProperties props{};
   vk.GetPhysicalDeviceProperties(physical, &props);
   if (props.vendorID != 0x10de) fatal("physical-device", "%s vendor got=0x%x expected=0x10de", props.deviceName,
                                       props.vendorID);
   pass("physical-device", "%s vendor=0x10de device=0x%04x API %u.%u.%u (%u physical devices)", props.deviceName,
        props.deviceID, VK_API_VERSION_MAJOR(props.apiVersion), VK_API_VERSION_MINOR(props.apiVersion),
        VK_API_VERSION_PATCH(props.apiVersion), n);
   vk.GetPhysicalDeviceMemoryProperties(physical, &memory_properties);

   std::vector<VkExtensionProperties> ext;
   r = device_extensions(vk.EnumerateDeviceExtensionProperties, physical, ext);
   if (r != VK_SUCCESS) fatal("device-extensions", "vkEnumerateDeviceExtensionProperties -> %d", (int)r);
   std::vector<std::string> bad;
   const unsigned swapchain = count_of(ext, VK_KHR_SWAPCHAIN_EXTENSION_NAME);
   if (swapchain != 1) bad.push_back(format("VK_KHR_swapchain listed %u times", swapchain));
   for (const char *name : kHiddenDevice)
      if (count_of(ext, name)) bad.push_back(format("%s listed", name));
   check(bad.empty(), "device-extensions",
         format("%zu offered; VK_KHR_swapchain once; the 14 hidden window-system extensions absent", ext.size()),
         joined(bad));

   vk.GetPhysicalDeviceQueueFamilyProperties(physical, &n, nullptr);
   std::vector<VkQueueFamilyProperties> families(n);
   vk.GetPhysicalDeviceQueueFamilyProperties(physical, &n, families.data());
   std::string list_text;
   for (uint32_t i = 0; i < n; ++i) {
      list_text +=
         format("%s%u: flags 0x%x, %u queues", i ? "; " : "", i, families[i].queueFlags, families[i].queueCount);
      if (family == UINT32_MAX && (families[i].queueFlags & 3) == 3 && families[i].queueCount) family = i;
   }
   info("queue families %s", list_text.c_str());
   if (family == UINT32_MAX) fatal("queue-family", "no graphics and compute family");
}

VkResult create_device(const char *extension, VkDevice *out)
{
   const float priority = 1.0f;
   VkDeviceQueueCreateInfo q{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
   q.queueFamilyIndex = family;
   q.queueCount = 1;
   q.pQueuePriorities = &priority;
   VkDeviceCreateInfo dc{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
   dc.queueCreateInfoCount = 1;
   dc.pQueueCreateInfos = &q;
   dc.enabledExtensionCount = extension ? 1 : 0;
   dc.ppEnabledExtensionNames = extension ? &extension : nullptr;
   return vk.CreateDevice(physical, &dc, nullptr, out);
}

void destroy_other_device(VkDevice d)
{
   auto destroy = (PFN_vkDestroyDevice)vk.GetDeviceProcAddr(d, "vkDestroyDevice");
   if (destroy) destroy(d, nullptr);
}

void make_devices()
{
   VkDevice other = VK_NULL_HANDLE;
   VkResult r = create_device("VK_KHR_present_wait", &other);
   if (r == VK_SUCCESS) destroy_other_device(other);
   check(r == VK_ERROR_EXTENSION_NOT_PRESENT, "device-refusal",
         "a device asking for VK_KHR_present_wait is refused with VK_ERROR_EXTENSION_NOT_PRESENT",
         format("vkCreateDevice with VK_KHR_present_wait got=%d expected=%d", (int)r,
                (int)VK_ERROR_EXTENSION_NOT_PRESENT));

   other = VK_NULL_HANDLE;
   r = create_device(nullptr, &other);
   if (r != VK_SUCCESS) {
      fail("device-plain", "vkCreateDevice without extensions -> %d", (int)r);
   } else {
      const void *create = (void *)vk.GetDeviceProcAddr(other, "vkCreateSwapchainKHR");
      const void *present = (void *)vk.GetDeviceProcAddr(other, "vkQueuePresentKHR");
      destroy_other_device(other);
      if (loader_mode)
         pass("device-plain", "a device without VK_KHR_swapchain is created and destroyed");
      else
         check(!create && !present, "device-plain",
               "a device without VK_KHR_swapchain answers NULL for vkCreateSwapchainKHR and vkQueuePresentKHR",
               format("without VK_KHR_swapchain vkCreateSwapchainKHR got=%p vkQueuePresentKHR got=%p expected=NULL",
                      create, present));
   }

   tee_sync();
   const size_t before = bridge_with("NVK Bridge: native device=").size();
   r = create_device(VK_KHR_SWAPCHAIN_EXTENSION_NAME, &device);
   if (r != VK_SUCCESS) {
      tee_sync();
      const auto why = bridge_with("NVK Bridge: a matching");
      fatal("device", "vkCreateDevice with VK_KHR_swapchain -> %d%s%s", (int)r, why.empty() ? "" : "; ",
            why.empty() ? "" : why.back().c_str());
   }
   tee_sync();
   const auto native = bridge_with("NVK Bridge: native device=");
   const auto copy = bridge_with("NVK Bridge: presentation completion=");
   if (native.size() > before) info("%s", native.back().substr(12).c_str());
   if (!copy.empty()) info("%s", copy.back().substr(12).c_str());
   check(native.size() > before, "device", "a device with VK_KHR_swapchain; the frontend reported its native device",
         "the device was created but standard error has no new \"NVK Bridge: native device=\" line");

#define DEV(name) vk.name = (PFN_vk##name)vk.GetDeviceProcAddr(device, "vk" #name)
   DEV(DestroyDevice); DEV(GetDeviceQueue); DEV(CreateCommandPool); DEV(DestroyCommandPool); DEV(ResetCommandPool);
   DEV(AllocateCommandBuffers); DEV(BeginCommandBuffer); DEV(EndCommandBuffer); DEV(CmdPipelineBarrier);
   DEV(CmdClearColorImage); DEV(QueueSubmit); DEV(CreateSemaphore); DEV(DestroySemaphore); DEV(CreateFence);
   DEV(DestroyFence); DEV(WaitForFences); DEV(ResetFences); DEV(CreateSwapchainKHR); DEV(DestroySwapchainKHR);
   DEV(GetSwapchainImagesKHR); DEV(AcquireNextImageKHR); DEV(QueuePresentKHR);
#undef DEV
   const void *all[] = {(void *)vk.DestroyDevice, (void *)vk.GetDeviceQueue, (void *)vk.CreateCommandPool,
                        (void *)vk.DestroyCommandPool, (void *)vk.ResetCommandPool, (void *)vk.AllocateCommandBuffers,
                        (void *)vk.BeginCommandBuffer, (void *)vk.EndCommandBuffer, (void *)vk.CmdPipelineBarrier,
                        (void *)vk.CmdClearColorImage, (void *)vk.QueueSubmit, (void *)vk.CreateSemaphore,
                        (void *)vk.DestroySemaphore, (void *)vk.CreateFence, (void *)vk.DestroyFence,
                        (void *)vk.WaitForFences, (void *)vk.ResetFences, (void *)vk.CreateSwapchainKHR,
                        (void *)vk.DestroySwapchainKHR, (void *)vk.GetSwapchainImagesKHR,
                        (void *)vk.AcquireNextImageKHR, (void *)vk.QueuePresentKHR};
   for (const void *f : all)
      if (!f) fatal("device-entries", "vkGetDeviceProcAddr gave NULL for one of the 22 device functions it uses");
   vk.GetDeviceQueue(device, family, 0, &queue);
   if (!queue) fatal("device-entries", "vkGetDeviceQueue gave no queue");

   if (!loader_mode) {
      std::vector<std::string> answered;
      for (const char *name : kHiddenEntries)
         if (vk.GetDeviceProcAddr(device, name)) answered.push_back(name);
      check(answered.empty(), "hidden-entries",
            "vkWaitForPresentKHR, vkSetHdrMetadataEXT, vkGetSwapchainStatusKHR, vkReleaseSwapchainImagesKHR, "
            "vkGetPastPresentationTimingGOOGLE and vkGetRefreshCycleDurationGOOGLE are NULL",
            "answered: " + joined(answered));
   }
   const std::string present_in = image_of((void *)vk.QueuePresentKHR);
   const std::string create_in = image_of((void *)vk.CreateSwapchainKHR);
   const std::string clear_in = image_of((void *)vk.CmdClearColorImage);
   if (loader_mode) {
      info("through the loader vkQueuePresentKHR is in %s, vkCreateSwapchainKHR in %s, vkCmdClearColorImage in %s",
           present_in.c_str(), create_in.c_str(), clear_in.c_str());
   } else {
      check(present_in == frontend_real && create_in == frontend_real && clear_in == renderer_real, "entry-images",
            "vkQueuePresentKHR and vkCreateSwapchainKHR are the frontend's, vkCmdClearColorImage the renderer's",
            format("vkQueuePresentKHR in %s, vkCreateSwapchainKHR in %s (expected %s); vkCmdClearColorImage in %s "
                   "(expected %s)", present_in.c_str(), create_in.c_str(), frontend_real.c_str(), clear_in.c_str(),
                   renderer_real.c_str()));
   }
}

void check_memory_and_images()
{
   const auto &m = memory_properties;
   std::string heaps, types;
   VkDeviceSize vram = 0;
   for (uint32_t h = 0; h < m.memoryHeapCount; ++h) {
      heaps += format("%s%u: %llu MiB flags 0x%x", h ? "; " : "", h,
                      (unsigned long long)(m.memoryHeaps[h].size >> 20), m.memoryHeaps[h].flags);
      if (m.memoryHeaps[h].flags & VK_MEMORY_HEAP_DEVICE_LOCAL_BIT) vram = std::max(vram, m.memoryHeaps[h].size);
   }
   for (uint32_t t = 0; t < m.memoryTypeCount; ++t)
      types += format("%s%u: heap %u flags 0x%x", t ? "; " : "", t, m.memoryTypes[t].heapIndex,
                      m.memoryTypes[t].propertyFlags);
   info("memory heaps %s", heaps.c_str());
   info("memory types %s", types.c_str());
   check(!limited_bar(m), "full-bar",
         format("a DEVICE_LOCAL|HOST_VISIBLE type lies on a heap as large as the largest DEVICE_LOCAL heap (%llu MiB): "
                "the frontend imports the drawable's VRAM and never takes the host-page path",
                (unsigned long long)(vram >> 20)),
         "the frontend's limited-BAR rule (bridge.mm:288-307) holds for this device: it would copy into host pages");

   // The Metal plugin loads its own NVK (libvulkan_nouveau.dylib, plugin/nvmtl_vk.c:140) for the Metal device; it is
   // reported, not counted as another driver.
   bool fe = false, re = false;
   std::vector<std::string> others, plugin_nvk;
   for (uint32_t i = 0; i < _dyld_image_count(); ++i) {
      const char *raw = _dyld_get_image_name(i);
      if (!raw) continue;
      const std::string name = raw, base = base_of(name);
      const bool vulkanish = base.compare(0, 7, "libnvk_") == 0 || base.compare(0, 10, "libvulkan_") == 0 ||
                             base.find("MoltenVK") != std::string::npos || base.find("_icd.") != std::string::npos;
      if (!vulkanish) continue;
      std::string r = real(name);
      if (r.empty()) r = name;
      if (r == frontend_real) fe = true;
      else if (r == renderer_real) re = true;
      else if (base == "libvulkan_nouveau.dylib") plugin_nvk.push_back(r);
      else others.push_back(r);
   }
   info("the plugin's own NVK mapped: %s", joined(plugin_nvk).c_str());
   check(fe && re && others.empty(), "images",
         format("%s and %s are mapped, and no other Vulkan driver", frontend_real.c_str(), renderer_real.c_str()),
         format("frontend %smapped, renderer %smapped; other Vulkan driver images: %s", fe ? "" : "not ",
                re ? "" : "not ", joined(others).c_str()));

   tee_sync();
   bool renderer_line = false;
   std::string seen_path;
   for (auto &l : bridge_with("NVK Bridge: renderer=")) {
      const size_t at = l.find("renderer=") + 9, end = l.find(';', at);
      seen_path = l.substr(at, end == std::string::npos ? std::string::npos : end - at);
      if (real(seen_path) == renderer_real) renderer_line = true;
   }
   check(renderer_line, "renderer-line",
         format("the frontend reported its renderer %s, presentation gpu-copy", renderer_real.c_str()),
         format("no \"NVK Bridge: renderer=\" line names %s (last seen: %s)", renderer_real.c_str(),
                seen_path.empty() ? "none" : seen_path.c_str()));
   if (loader_mode) {
      std::string found;
      {
         std::lock_guard<std::mutex> lock(tee_mutex);
         found = manifest_line;
      }
      check(!found.empty(), "loader-manifest",
            "the loader's driver log names nvk_bridge_icd.json: " + found.substr(0, 160),
            "with VK_LOADER_DEBUG=driver no line on standard error names nvk_bridge_icd.json");
   }
}

void check_surface()
{
   VkMetalSurfaceCreateInfoEXT si{VK_STRUCTURE_TYPE_METAL_SURFACE_CREATE_INFO_EXT};
   si.pLayer = layer;
   VkResult r = vk.CreateMetalSurfaceEXT(instance, &si, nullptr, &surface);
   if (r != VK_SUCCESS) fatal("surface", "vkCreateMetalSurfaceEXT on the window's CAMetalLayer subclass -> %d", (int)r);
   if (loader_mode) {
      pass("surface", "vkCreateMetalSurfaceEXT on the window's CAMetalLayer subclass");
   } else {
      const void *exported = dlsym(entry_lib, "vkCreateMetalSurfaceEXT");
      check(exported == (void *)vk.CreateMetalSurfaceEXT, "surface",
            "vkCreateMetalSurfaceEXT on the window's CAMetalLayer subclass; the exported symbol is the same function",
            format("the exported vkCreateMetalSurfaceEXT got=%p, vkGetInstanceProcAddr's got=%p", exported,
                   (void *)vk.CreateMetalSurfaceEXT));
   }

   CALayer *plain = [CALayer layer];
   VkMetalSurfaceCreateInfoEXT bad_layer{VK_STRUCTURE_TYPE_METAL_SURFACE_CREATE_INFO_EXT};
   bad_layer.pLayer = (CAMetalLayer *)plain;
   VkMetalSurfaceCreateInfoEXT bad_flags{VK_STRUCTURE_TYPE_METAL_SURFACE_CREATE_INFO_EXT};
   bad_flags.pLayer = layer;
   bad_flags.flags = 1;
   VkSurfaceKHR s1 = VK_NULL_HANDLE, s2 = VK_NULL_HANDLE;
   const VkResult r1 = vk.CreateMetalSurfaceEXT(instance, &bad_layer, nullptr, &s1);
   const VkResult r2 = vk.CreateMetalSurfaceEXT(instance, &bad_flags, nullptr, &s2);
   if (r1 == VK_SUCCESS) vk.DestroySurfaceKHR(instance, s1, nullptr);
   if (r2 == VK_SUCCESS) vk.DestroySurfaceKHR(instance, s2, nullptr);
   const bool refused = r1 == VK_ERROR_INITIALIZATION_FAILED && r2 == VK_ERROR_INITIALIZATION_FAILED;
   const std::string got = format("a plain CALayer got=%d, flags=1 got=%d expected=%d", (int)r1, (int)r2,
                                  (int)VK_ERROR_INITIALIZATION_FAILED);
   if (refused) pass("surface-refusal", "a plain CALayer and flags=1 are refused with VK_ERROR_INITIALIZATION_FAILED");
   else if (loader_mode)
      info("surface-refusal through the loader (it may make driver surfaces later): %s", got.c_str());
   else fail("surface-refusal", "%s", got.c_str());

   VkBool32 supported = VK_FALSE;
   r = vk.GetPhysicalDeviceSurfaceSupportKHR(physical, family, surface, &supported);
   check(r == VK_SUCCESS && supported, "support", format("queue family %u can present to the surface", family),
         format("vkGetPhysicalDeviceSurfaceSupportKHR -> %d, supported %u", (int)r, supported));

   const VkExtent2D want = layer_extent();
   const auto alpha = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR | VK_COMPOSITE_ALPHA_PRE_MULTIPLIED_BIT_KHR;
   const auto usage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT |
                      VK_IMAGE_USAGE_TRANSFER_DST_BIT;
   auto caps_problems = [&](const VkSurfaceCapabilitiesKHR &c) {
      std::vector<std::string> bad;
      if (c.minImageCount != 2 || c.maxImageCount != 8)
         bad.push_back(format("images got=%u..%u expected=2..8", c.minImageCount, c.maxImageCount));
      if (c.currentExtent.width != want.width || c.currentExtent.height != want.height)
         bad.push_back(format("currentExtent got=%ux%u expected=%ux%u", c.currentExtent.width, c.currentExtent.height,
                              want.width, want.height));
      if (c.supportedTransforms != VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR ||
          c.currentTransform != VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR)
         bad.push_back(format("transforms got=0x%x current 0x%x expected=identity", c.supportedTransforms,
                              (unsigned)c.currentTransform));
      if (c.supportedCompositeAlpha != (VkCompositeAlphaFlagsKHR)alpha)
         bad.push_back(format("composite alpha got=0x%x expected=0x%x", c.supportedCompositeAlpha, (unsigned)alpha));
      if (c.supportedUsageFlags != (VkImageUsageFlags)usage)
         bad.push_back(format("usage got=0x%x expected=0x%x", c.supportedUsageFlags, (unsigned)usage));
      if (c.maxImageArrayLayers != 1) bad.push_back(format("array layers got=%u expected=1", c.maxImageArrayLayers));
      return bad;
   };
   VkSurfaceCapabilitiesKHR caps{};
   r = vk.GetPhysicalDeviceSurfaceCapabilitiesKHR(physical, surface, &caps);
   auto bad = caps_problems(caps);
   if (r != VK_SUCCESS) bad.insert(bad.begin(), format("-> %d", (int)r));
   check(bad.empty(), "caps",
         format("2..8 images, currentExtent %ux%u (the layer's bounds times its scale), identity, opaque and "
                "premultiplied alpha, colour-attachment and transfer usage", want.width, want.height),
         joined(bad));

   VkPhysicalDeviceSurfaceInfo2KHR info2{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SURFACE_INFO_2_KHR};
   info2.surface = surface;
   VkSurfaceProtectedCapabilitiesKHR prot{VK_STRUCTURE_TYPE_SURFACE_PROTECTED_CAPABILITIES_KHR};
   prot.supportsProtected = VK_TRUE;
   VkSurfaceCapabilities2KHR caps2{VK_STRUCTURE_TYPE_SURFACE_CAPABILITIES_2_KHR};
   caps2.pNext = &prot;
   r = vk.GetPhysicalDeviceSurfaceCapabilities2KHR(physical, &info2, &caps2);
   bad = caps_problems(caps2.surfaceCapabilities);
   if (r != VK_SUCCESS) bad.insert(bad.begin(), format("-> %d", (int)r));
   if (prot.supportsProtected) bad.push_back("supportsProtected got=TRUE expected=FALSE");
   check(bad.empty(), "caps2", "the same through vkGetPhysicalDeviceSurfaceCapabilities2KHR, supportsProtected FALSE",
         joined(bad));

   uint32_t n = 0;
   r = vk.GetPhysicalDeviceSurfaceFormatsKHR(physical, surface, &n, nullptr);
   std::vector<VkSurfaceFormatKHR> formats(n);
   if (r == VK_SUCCESS) r = vk.GetPhysicalDeviceSurfaceFormatsKHR(physical, surface, &n, formats.data());
   formats.resize(r == VK_SUCCESS ? n : 0);
   auto formats_ok = [](const std::vector<VkSurfaceFormatKHR> &f) {
      return f.size() == 2 && f[0].format == VK_FORMAT_B8G8R8A8_SRGB && f[1].format == VK_FORMAT_B8G8R8A8_UNORM &&
             f[0].colorSpace == VK_COLOR_SPACE_SRGB_NONLINEAR_KHR &&
             f[1].colorSpace == VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
   };
   auto formats_text = [](const std::vector<VkSurfaceFormatKHR> &f) {
      std::vector<std::string> s;
      for (auto &x : f) s.push_back(format("%d/%d", (int)x.format, (int)x.colorSpace));
      return joined(s);
   };
   check(r == VK_SUCCESS && formats_ok(formats), "formats", "exactly B8G8R8A8_SRGB and B8G8R8A8_UNORM, SRGB_NONLINEAR",
         format("-> %d, got=%s expected=50/0, 44/0", (int)r, formats_text(formats).c_str()));

   n = 0;
   r = vk.GetPhysicalDeviceSurfaceFormats2KHR(physical, &info2, &n, nullptr);
   std::vector<VkSurfaceFormat2KHR> formats2(n, VkSurfaceFormat2KHR{VK_STRUCTURE_TYPE_SURFACE_FORMAT_2_KHR});
   if (r == VK_SUCCESS) r = vk.GetPhysicalDeviceSurfaceFormats2KHR(physical, &info2, &n, formats2.data());
   formats.clear();
   for (uint32_t i = 0; r == VK_SUCCESS && i < n; ++i) formats.push_back(formats2[i].surfaceFormat);
   check(r == VK_SUCCESS && formats_ok(formats), "formats2",
         "the same two through vkGetPhysicalDeviceSurfaceFormats2KHR",
         format("-> %d, got=%s expected=50/0, 44/0", (int)r, formats_text(formats).c_str()));

   n = 0;
   r = vk.GetPhysicalDeviceSurfacePresentModesKHR(physical, surface, &n, nullptr);
   std::vector<VkPresentModeKHR> modes(n);
   if (r == VK_SUCCESS) r = vk.GetPhysicalDeviceSurfacePresentModesKHR(physical, surface, &n, modes.data());
   modes.resize(r == VK_SUCCESS ? n : 0);
   check(r == VK_SUCCESS && modes.size() == 2 && modes[0] == VK_PRESENT_MODE_FIFO_KHR &&
            modes[1] == VK_PRESENT_MODE_IMMEDIATE_KHR,
         "modes", "exactly FIFO and IMMEDIATE",
         format("-> %d, %zu modes, first %d expected=FIFO (2), IMMEDIATE (0)", (int)r, modes.size(),
                modes.empty() ? -1 : (int)modes[0]));

   n = 0;
   r = vk.GetPhysicalDevicePresentRectanglesKHR(physical, surface, &n, nullptr);
   std::vector<VkRect2D> rects(n);
   if (r == VK_SUCCESS) r = vk.GetPhysicalDevicePresentRectanglesKHR(physical, surface, &n, rects.data());
   rects.resize(r == VK_SUCCESS ? n : 0);
   check(r == VK_SUCCESS && rects.size() == 1 && !rects[0].offset.x && !rects[0].offset.y &&
            rects[0].extent.width == want.width && rects[0].extent.height == want.height,
         "rects", format("one present rectangle, 0,0 %ux%u", want.width, want.height),
         format("-> %d, %zu rectangles, first %ux%u expected=1 of %ux%u", (int)r, rects.size(),
                rects.empty() ? 0 : rects[0].extent.width, rects.empty() ? 0 : rects[0].extent.height, want.width,
                want.height));
}

void make_frame_objects()
{
   VkCommandPoolCreateInfo pc{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
   pc.queueFamilyIndex = family;
   VkResult r = vk.CreateCommandPool(device, &pc, nullptr, &pool);
   VkCommandBufferAllocateInfo ca{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
   ca.commandPool = pool;
   ca.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
   ca.commandBufferCount = 1;
   if (r == VK_SUCCESS) r = vk.AllocateCommandBuffers(device, &ca, &commands);
   VkSemaphoreCreateInfo si{VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO};
   if (r == VK_SUCCESS) r = vk.CreateSemaphore(device, &si, nullptr, &acquired);
   if (r == VK_SUCCESS) r = vk.CreateSemaphore(device, &si, nullptr, &rendered);
   VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
   if (r == VK_SUCCESS) r = vk.CreateFence(device, &fi, nullptr, &fence);
   if (r != VK_SUCCESS) fatal("frame-objects", "command pool, command buffer, semaphores or fence -> %d", (int)r);
}

void check_swapchain_refusals()
{
   const Spec base{VK_PRESENT_MODE_FIFO_KHR, VK_FORMAT_B8G8R8A8_UNORM, 3, VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR};
   const VkExtent2D extent = layer_extent();
   struct Case {
      const char *what;
      VkResult want;
      void (*change)(VkSwapchainCreateInfoKHR &);
   };
   const Case cases[] = {
      {"R8G8B8A8_UNORM", VK_ERROR_FORMAT_NOT_SUPPORTED,
       [](VkSwapchainCreateInfoKHR &s) { s.imageFormat = VK_FORMAT_R8G8B8A8_UNORM; }},
      {"1 image", VK_ERROR_INITIALIZATION_FAILED, [](VkSwapchainCreateInfoKHR &s) { s.minImageCount = 1; }},
      {"9 images", VK_ERROR_INITIALIZATION_FAILED, [](VkSwapchainCreateInfoKHR &s) { s.minImageCount = 9; }},
      {"MAILBOX", VK_ERROR_INITIALIZATION_FAILED,
       [](VkSwapchainCreateInfoKHR &s) { s.presentMode = VK_PRESENT_MODE_MAILBOX_KHR; }},
      {"extent 0", VK_ERROR_INITIALIZATION_FAILED,
       [](VkSwapchainCreateInfoKHR &s) { s.imageExtent = VkExtent2D{0, 0}; }},
      {"STORAGE usage", VK_ERROR_FEATURE_NOT_PRESENT,
       [](VkSwapchainCreateInfoKHR &s) { s.imageUsage |= VK_IMAGE_USAGE_STORAGE_BIT; }},
      {"POST_MULTIPLIED alpha", VK_ERROR_FEATURE_NOT_PRESENT,
       [](VkSwapchainCreateInfoKHR &s) { s.compositeAlpha = VK_COMPOSITE_ALPHA_POST_MULTIPLIED_BIT_KHR; }},
   };
   std::vector<std::string> bad;
   for (const Case &c : cases) {
      VkSwapchainCreateInfoKHR sc = chain_info(base, extent, VK_NULL_HANDLE);
      c.change(sc);
      VkSwapchainKHR made = VK_NULL_HANDLE;
      const VkResult r = vk.CreateSwapchainKHR(device, &sc, nullptr, &made);
      if (r == VK_SUCCESS) vk.DestroySwapchainKHR(device, made, nullptr);
      if (r != c.want) bad.push_back(format("%s got=%d expected=%d", c.what, (int)r, (int)c.want));
   }
   if (layer.device) bad.push_back("the refusals touched the layer: its device is set");
   check(bad.empty(), "swapchain-refusals",
         "R8G8B8A8 (FORMAT_NOT_SUPPORTED), 1 and 9 images, MAILBOX, extent 0 (INITIALIZATION_FAILED), STORAGE usage "
         "and POST_MULTIPLIED alpha (FEATURE_NOT_PRESENT) are refused before the layer is touched",
         joined(bad));
}

void check_acquire(Chain &c)
{
   VkFence f[4]{};
   VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
   for (auto &x : f)
      if (vk.CreateFence(device, &fi, nullptr, &x) != VK_SUCCESS) fatal("acquire", "vkCreateFence failed");
   std::vector<std::string> bad;
   uint32_t index[3]{};
   for (int i = 0; i < 3; ++i) {
      const VkResult r = vk.AcquireNextImageKHR(device, c.handle, 2 * kSecond, VK_NULL_HANDLE, f[i], &index[i]);
      if (r != VK_SUCCESS) fatal("acquire", "acquire %d of 3 got=%d expected=0", i + 1, (int)r);
   }
   const VkResult waited = vk.WaitForFences(device, 3, f, VK_TRUE, 5 * kSecond);
   if (waited != VK_SUCCESS) fatal("acquire", "the three acquire fences did not signal within 5 s (%d)", (int)waited);
   if (index[0] == index[1] || index[1] == index[2] || index[0] == index[2])
      bad.push_back(format("indices got=%u,%u,%u expected three different", index[0], index[1], index[2]));
   uint32_t spare = 0;
   const VkResult zero = vk.AcquireNextImageKHR(device, c.handle, 0, VK_NULL_HANDLE, f[3], &spare);
   if (zero != VK_NOT_READY)
      bad.push_back(format("a fourth with timeout 0 got=%d expected=%d", (int)zero, (int)VK_NOT_READY));
   const double t0 = now();
   const VkResult wait = vk.AcquireNextImageKHR(device, c.handle, 1000000, VK_NULL_HANDLE, f[3], &spare);
   const double took = now() - t0;
   if (wait != VK_TIMEOUT) bad.push_back(format("a fourth with 1 ms got=%d expected=%d", (int)wait, (int)VK_TIMEOUT));
   if (took > 0.1) bad.push_back(format("the 1 ms acquire took %.3f s expected<=0.1 s", took));
   if (zero == VK_SUCCESS || wait == VK_SUCCESS) vk.WaitForFences(device, 1, &f[3], VK_TRUE, 5 * kSecond);
   check(bad.empty(), "acquire",
         format("3 acquires with fences; a fourth gives VK_NOT_READY with timeout 0 and VK_TIMEOUT after 1 ms "
                "(%.1f ms)", took * 1e3),
         joined(bad));
   for (auto x : f) vk.DestroyFence(device, x, nullptr);
}

void metal_blit()
{
   // The device the frontend gave the layer: the card's, which the Vulkan device also drives.
   id<MTLDevice> m = layer.device;
   if (!m || m.registryID != card.registryID) {
      fail("metal-blit", "the layer's device got=0x%llx expected=the card's 0x%llx",
           (unsigned long long)(m ? m.registryID : 0), (unsigned long long)card.registryID);
      return;
   }
   const NSUInteger size = 65536;
   id<MTLCommandQueue> q = [m newCommandQueue];
   id<MTLBuffer> buffer = [m newBufferWithLength:size options:MTLResourceStorageModeShared];
   if (!q || !buffer || !buffer.contents) {
      fail("metal-blit", "no command queue or no shared buffer of 64 KiB");
      return;
   }
   memset(buffer.contents, 0, size);
   id<MTLCommandBuffer> cmd = [q commandBuffer];
   id<MTLBlitCommandEncoder> blit = [cmd blitCommandEncoder];
   [blit fillBuffer:buffer range:NSMakeRange(0, size) value:0xA5];
   [blit endEncoding];
   dispatch_semaphore_t done = dispatch_semaphore_create(0);
   [cmd addCompletedHandler:^(id<MTLCommandBuffer> finished) {
      (void)finished;
      dispatch_semaphore_signal(done);
   }];
   [cmd commit];
   if (dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, (int64_t)(5 * NSEC_PER_SEC))))
      fatal("metal-blit", "the Metal blit did not complete within 5 s while the Vulkan device lives");
   const uint8_t *bytes = (const uint8_t *)buffer.contents;
   size_t bad = 0;
   for (NSUInteger i = 0; i < size; ++i)
      if (bytes[i] != 0xA5) ++bad;
   check(cmd.status == MTLCommandBufferStatusCompleted && !cmd.error && !bad, "metal-blit",
         format("a blit on %s filled 64 KiB with 0xA5 while the Vulkan device lives", m.name.UTF8String),
         format("status got=%ld expected=%ld, error %s, %zu of 65536 bytes not 0xA5", (long)cmd.status,
                (long)MTLCommandBufferStatusCompleted, cmd.error ? cmd.error.localizedDescription.UTF8String : "none",
                bad));
}

// ---- the windowed run ---------------------------------------------------------------------------------------------

[[noreturn]] void run_windowed(const char *lib, const char *frontend_path)
{
   frontend_real = real(frontend_path);
   if (frontend_real.empty()) frontend_real = frontend_path;
   renderer_real = real(folder_of(frontend_real) + "/libnvk_renderer.dylib");
   if (renderer_real.empty()) renderer_real = folder_of(frontend_real) + "/libnvk_renderer.dylib";
   require_gui();
   card = require_device();
   open_window();
   info("window 640x360 points, backing scale %g, layer %s, device %s", (double)window.backingScaleFactor,
        NSStringFromClass([layer class]).UTF8String, card.name.UTF8String);

   load_instance_level(lib);
   make_instance();
   find_frontend();
   pick_physical();
   make_devices();
   check_memory_and_images();
   check_surface();
   make_frame_objects();
   check_swapchain_refusals();

   // Phase A: FIFO, B8G8R8A8_UNORM, 3 images.
   const Spec fifo{VK_PRESENT_MODE_FIFO_KHR, VK_FORMAT_B8G8R8A8_UNORM, 3, VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR};
   Chain chain;
   std::string e = make_chain(fifo, VK_NULL_HANDLE, chain);
   if (!e.empty()) fatal("chain-fifo", "%s", e.c_str());
   check_layer("layer-fifo", chain, MTLPixelFormatBGRA8Unorm, 3, YES, YES, 3);
   check_acquire(chain);
   vk.DestroySwapchainKHR(device, chain.handle, nullptr);
   e = make_chain(fifo, VK_NULL_HANDLE, chain);
   if (!e.empty()) fatal("chain-fifo", "after the acquire check: %s", e.c_str());
   Tally ta("frames-fifo");
   frames(chain, 120, kFifo, ta, false);
   report(ta, "FIFO, B8G8R8A8_UNORM, 3 images");
   metal_blit();

   // Phase B: a resize to 800x450 points.
   const VkExtent2D small = chain.extent;
   [window setContentSize:NSMakeSize(800, 450)];
   pump(0.3);
   {
      Tally t("resize-old-chain");
      Presented p{};
      @autoreleasepool {
         p = present_frame(chain, frame_colour(frame_counter), kResized, t, true, false);
      }
      if (!t.problems)
         pass("resize-old-chain", "the first present on the %ux%u chain after the resize gave %s (both accepted)",
              small.width, small.height, p.result == VK_SUCCESS ? "VK_SUCCESS" : "VK_ERROR_OUT_OF_DATE_KHR");
      else
         fail("resize-old-chain", "%s", t.first.c_str());
   }
   const CGFloat scale = layer.contentsScale;
   const VkExtent2D big{(uint32_t)(800 * scale), (uint32_t)(450 * scale)};
   VkSurfaceCapabilitiesKHR caps{};
   VkResult r = vk.GetPhysicalDeviceSurfaceCapabilitiesKHR(physical, surface, &caps);
   check(r == VK_SUCCESS && caps.currentExtent.width == big.width && caps.currentExtent.height == big.height,
         "resize-caps", format("currentExtent %ux%u after the resize", big.width, big.height),
         format("-> %d, currentExtent got=%ux%u expected=%ux%u (layer bounds %gx%g)", (int)r, caps.currentExtent.width,
                caps.currentExtent.height, big.width, big.height, (double)layer.bounds.size.width,
                (double)layer.bounds.size.height));
   Chain next;
   e = make_chain(fifo, chain.handle, next);
   if (!e.empty()) fatal("chain-resized", "%s", e.c_str());
   {
      VkFence f = VK_NULL_HANDLE;
      VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
      vk.CreateFence(device, &fi, nullptr, &f);
      uint32_t index = 0;
      const VkResult old = vk.AcquireNextImageKHR(device, chain.handle, 0, VK_NULL_HANDLE, f, &index);
      if (old == VK_SUCCESS) vk.WaitForFences(device, 1, &f, VK_TRUE, 5 * kSecond);
      check(old == VK_ERROR_OUT_OF_DATE_KHR, "retired-chain",
            "an acquire on the chain replaced through oldSwapchain gives VK_ERROR_OUT_OF_DATE_KHR",
            format("acquire on the replaced chain got=%d expected=%d", (int)old, (int)VK_ERROR_OUT_OF_DATE_KHR));
      vk.DestroyFence(device, f, nullptr);
   }
   vk.DestroySwapchainKHR(device, chain.handle, nullptr);
   chain = next;
   Tally tb("frames-resized");
   frames(chain, 120, kResized, tb, false);
   report(tb, format("%ux%u", chain.extent.width, chain.extent.height).c_str());

   // Phase C: minimize and restore.
   [window miniaturize:nil];
   // A window that does not minimize or come back is the program's own window failing, not the driver: exit 2.
   if (!pump_until([] { return (bool)window.miniaturized; }, 5.0))
      setup_error("minimize: the window was not miniaturized within 5 s (the window server, not the driver)");
   pass("minimize", "the window is miniaturized");
   r = vk.GetPhysicalDeviceSurfaceCapabilitiesKHR(physical, surface, &caps);
   check(r == VK_SUCCESS && caps.currentExtent.width == chain.extent.width &&
            caps.currentExtent.height == chain.extent.height,
         "minimized-caps", "the capabilities answer with the extent unchanged while minimized",
         format("-> %d, currentExtent got=%ux%u expected=%ux%u", (int)r, caps.currentExtent.width,
                caps.currentExtent.height, chain.extent.width, chain.extent.height));
   {
      Tally t("minimized-present");
      Presented p{};
      @autoreleasepool {
         p = present_frame(chain, frame_colour(frame_counter), kMinimized, t, true, false);
      }
      if (!t.problems && p.seconds <= 2.5)
         pass("minimized-present", "a present while minimized gave %s in %.3f s (both accepted)",
              p.result == VK_SUCCESS ? "VK_SUCCESS" : "VK_ERROR_OUT_OF_DATE_KHR", p.seconds);
      else
         fail("minimized-present", "%s (%.3f s, expected<=2.5 s)", t.problems ? t.first.c_str() : "slow", p.seconds);
   }
   [window deminiaturize:nil];
   if (!pump_until([] { return !window.miniaturized && visible(); }, 5.0))
      setup_error("restore: the window was not restored and visible within 5 s (the window server, not the driver)");
   pass("restore", "the window is restored and visible");
   {
      Tally tc("frames-restored");
      bool recreated = false;
      for (unsigned i = 0; i < 60; ++i) {
         Presented p{};
         @autoreleasepool {
            p = present_frame(chain, frame_colour(frame_counter), kRestored, tc, !recreated, i % 10 == 9);
         }
         if (p.result == VK_ERROR_OUT_OF_DATE_KHR && !recreated) {
            recreated = true;
            info("frames-restored: frame %u gave VK_ERROR_OUT_OF_DATE_KHR; the chain is made again once", i);
            replace_chain(chain, fifo, "after an out-of-date present on the restored window");
         }
      }
      report(tc, "after the restore, on the chain that was minimized");
   }
   replace_chain(chain, fifo, "after the restore");
   Tally tr("frames-recreated");
   frames(chain, 60, kRestored, tr, false);
   report(tr, "on a chain made after the restore");

   // Phase D: IMMEDIATE, B8G8R8A8_SRGB, 2 images.
   const Spec immediate{VK_PRESENT_MODE_IMMEDIATE_KHR, VK_FORMAT_B8G8R8A8_SRGB, 2, VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR};
   replace_chain(chain, immediate, "IMMEDIATE sRGB");
   check_layer("layer-immediate", chain, MTLPixelFormatBGRA8Unorm_sRGB, 2, NO, YES, 2);
   Tally td("frames-immediate");
   frames(chain, 60, kImmediate, td, true);
   report(td, "IMMEDIATE, B8G8R8A8_SRGB, 2 images");

   // Phase E: 8 images, premultiplied alpha.
   const Spec eight{VK_PRESENT_MODE_FIFO_KHR, VK_FORMAT_B8G8R8A8_UNORM, 8, VK_COMPOSITE_ALPHA_PRE_MULTIPLIED_BIT_KHR};
   replace_chain(chain, eight, "8 images");
   check_layer("layer-eight", chain, MTLPixelFormatBGRA8Unorm, 3, YES, NO, 8);
   Tally te("frames-eight");
   frames(chain, 30, kEight, te, false);
   report(te, "FIFO, 8 images, premultiplied alpha");

   // Presented handlers: QuartzCore's word that the drawable reached the screen, for every drawable presented with
   // VK_SUCCESS (the one presented while minimized aside); presentedTime > 0 for 90% of the FIFO frames.
   auto pending = [] {
      unsigned n = 0;
      for (uint32_t s = 0; s < std::min(vended.load(), kSlots); ++s)
         if (slot_ok[s] && slot_phase[s] != kMinimized && !slot_handled[s]) ++n;
      return n;
   };
   const double t0 = now();
   pump_until([&] { return pending() == 0; }, 2.0);
   unsigned required = 0, shown_ab = 0, ok_ab = 0, minimized_handled = 0, minimized_ok = 0;
   for (uint32_t s = 0; s < std::min(vended.load(), kSlots); ++s) {
      if (!slot_ok[s]) continue;
      if (slot_phase[s] == kMinimized) {
         ++minimized_ok;
         if (slot_handled[s]) ++minimized_handled;
         continue;
      }
      ++required;
      if (slot_phase[s] == kFifo || slot_phase[s] == kResized) {
         ++ok_ab;
         if (slot_shown[s]) ++shown_ab;
      }
   }
   const unsigned missing = pending();
   check(!missing && required, "presented-handlers",
         format("all %u drawables presented got their presented handler (%.2f s after the last present)", required,
                now() - t0),
         format("%u of %u presented drawables got no presented handler within 2 s after the last present", missing,
                required));
   if (minimized_ok)
      info("the present made while minimized %s its presented handler", minimized_handled ? "got" : "did not get");
   check(ok_ab && shown_ab * 10 >= ok_ab * 9, "presented-time",
         format("presentedTime > 0 for %u of %u FIFO frames before and after the resize (90%% needed)", shown_ab,
                ok_ab),
         format("presentedTime > 0 for %u of %u FIFO frames before and after the resize expected>=90%%", shown_ab,
                ok_ab));

   uint64_t stats[7]{};
   get_stats(stats);
   const uint64_t copies = stats[0] - stats_start[0], bytes = stats[1] - stats_start[1];
   const uint64_t imports = stats[4] - stats_start[4];
   check(copies == success_presents && bytes == observed_bytes && !stats[5] && !stats[6], "stats",
         format("nvkbridgeGetStats: %llu copies of %llu bytes, as many as the successful presents and the observer "
                "saw; no CPU pixel traffic", (unsigned long long)copies, (unsigned long long)bytes),
         format("copies got=%llu expected=%u, bytes got=%llu expected=%llu, CPU words %llu %llu expected=0 0",
                (unsigned long long)copies, success_presents, (unsigned long long)bytes,
                (unsigned long long)observed_bytes, (unsigned long long)stats[5], (unsigned long long)stats[6]));
   check(imports == 0, "vram-import", "no drawable VRAM import was refused (import_failures 0)",
         format("import_failures got=%llu expected=0; the first in frame %u, after %u CPU reads of drawables",
                (unsigned long long)imports, first_import_failure, cpu_reads_before));
   if (copies)
      info("per copy: %.2f ms in nextDrawable, %.2f ms waiting for the copy; CPU locks of the drawable at most %.1f ms",
           (double)(stats[2] - stats_start[2]) / 1e6 / (double)copies,
           (double)(stats[3] - stats_start[3]) / 1e6 / (double)copies, cpu_lock_max * 1e3);

   // Teardown.
   vk.DestroySwapchainKHR(device, chain.handle, nullptr);
   tee_sync();
   const auto counts = bridge_with("NVK Bridge: gpu_copy_count=");
   unsigned long long counted = 0;
   const bool line_ok = !counts.empty() &&
                        sscanf(counts.back().c_str(), "NVK Bridge: gpu_copy_count=%llu", &counted) == 1 &&
                        counted == stats[0];
   check(line_ok, "teardown",
         format("vkDestroySwapchainKHR reported gpu_copy_count=%llu with no CPU pixel traffic", counted),
         format("the last gpu_copy_count line got=%s expected=%llu", counts.empty() ? "none" : counts.back().c_str(),
                (unsigned long long)stats[0]));
   set_observer(nullptr);
   vk.DestroyFence(device, fence, nullptr);
   vk.DestroySemaphore(device, acquired, nullptr);
   vk.DestroySemaphore(device, rendered, nullptr);
   vk.DestroyCommandPool(device, pool, nullptr);
   vk.DestroyDevice(device, nullptr);
   vk.DestroySurfaceKHR(instance, surface, nullptr);
   vk.DestroyInstance(instance, nullptr);
   close_window();

   tee_sync();
   std::vector<std::string> bad;
   for (const char *part : {"NVK Bridge: renderer load failed", "NVK Bridge: presentation completion uncertain",
                            "NVK Bridge: uncertain GPU completion"})
      for (auto &l : bridge_with(part)) bad.push_back(l);
   check(bad.empty(), "frontend-messages", "the frontend reported no failed load and no uncertain completion",
         joined(bad));
   finish();
}

// ---- the control: the renderer alone ------------------------------------------------------------------------------

[[noreturn]] void run_renderer(const char *path)
{
   card = require_device();
   void *lib = dlopen(path, RTLD_NOW | RTLD_LOCAL | RTLD_FIRST);
   if (!lib) fatal("load", "dlopen %s: %s", path, dlerror());
   auto gipa = (PFN_vkGetInstanceProcAddr)dlsym(lib, "vk_icdGetInstanceProcAddr");
   if (!gipa) fatal("load", "%s exports no vk_icdGetInstanceProcAddr", path);
   auto enumerate = (PFN_vkEnumerateInstanceExtensionProperties)gipa(VK_NULL_HANDLE,
                                                                     "vkEnumerateInstanceExtensionProperties");
   auto create = (PFN_vkCreateInstance)gipa(VK_NULL_HANDLE, "vkCreateInstance");
   if (!enumerate || !create)
      fatal("load", "the renderer answers no vkEnumerateInstanceExtensionProperties or vkCreateInstance");
   std::vector<VkExtensionProperties> ext;
   VkResult r = instance_extensions(enumerate, ext);
   if (r != VK_SUCCESS) fatal("renderer-instance", "vkEnumerateInstanceExtensionProperties -> %d", (int)r);
   std::vector<std::string> hidden_instance, hidden_device;
   for (const char *n : kHiddenInstance)
      if (count_of(ext, n)) hidden_instance.push_back(n);
   info("the renderer offers %zu instance extensions; VK_KHR_surface %s, VK_EXT_metal_surface %s", ext.size(),
        count_of(ext, VK_KHR_SURFACE_EXTENSION_NAME) ? "yes" : "no",
        count_of(ext, VK_EXT_METAL_SURFACE_EXTENSION_NAME) ? "yes" : "no");
   VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
   app.pApplicationName = "native Vulkan renderer control";
   app.apiVersion = VK_API_VERSION_1_3;
   VkInstanceCreateInfo ci{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
   ci.pApplicationInfo = &app;
   VkInstance inst = VK_NULL_HANDLE;
   r = create(&ci, nullptr, &inst);
   if (r != VK_SUCCESS) fatal("renderer-instance", "vkCreateInstance (API 1.3, no extensions) -> %d", (int)r);
   auto destroy = (PFN_vkDestroyInstance)gipa(inst, "vkDestroyInstance");
   auto physicals = (PFN_vkEnumeratePhysicalDevices)gipa(inst, "vkEnumeratePhysicalDevices");
   auto properties = (PFN_vkGetPhysicalDeviceProperties)gipa(inst, "vkGetPhysicalDeviceProperties");
   auto dev_ext = (PFN_vkEnumerateDeviceExtensionProperties)gipa(inst, "vkEnumerateDeviceExtensionProperties");
   if (!destroy || !physicals || !properties || !dev_ext) fatal("renderer-instance", "missing instance functions");
   uint32_t n = 0;
   r = physicals(inst, &n, nullptr);
   std::vector<VkPhysicalDevice> list(n);
   if (r == VK_SUCCESS && n) r = physicals(inst, &n, list.data());
   if (r != VK_SUCCESS || !n) {
      destroy(inst, nullptr);
      fatal("renderer-devices", "vkEnumeratePhysicalDevices -> %d, %u devices", (int)r, n);
   }
   VkPhysicalDeviceProperties props{};
   properties(list[0], &props);
   std::vector<VkExtensionProperties> dext;
   r = device_extensions(dev_ext, list[0], dext);
   destroy(inst, nullptr);
   if (r != VK_SUCCESS) fatal("renderer-devices", "vkEnumerateDeviceExtensionProperties -> %d", (int)r);
   for (const char *name : kHiddenDevice)
      if (count_of(dext, name)) hidden_device.push_back(name);
   info("the renderer's %s (vendor 0x%x, device 0x%04x) offers %zu device extensions; VK_KHR_swapchain %s",
        props.deviceName, props.vendorID, props.deviceID, dext.size(),
        count_of(dext, VK_KHR_SWAPCHAIN_EXTENSION_NAME) ? "yes" : "no");
   info("hidden instance extensions the renderer offers: %s", joined(hidden_instance).c_str());
   info("hidden device extensions the renderer offers: %s", joined(hidden_device).c_str());
   pass("renderer-extensions", "%zu of the 3 hidden instance and %zu of the 14 hidden device extensions are offered by "
        "the renderer itself (the windowed rows require each to be absent through the frontend)",
        hidden_instance.size(), hidden_device.size());
   finish();
}

[[noreturn]] void usage()
{
   setup_error("usage: nvmtltest direct <frontend> | loader <loader> <frontend> | renderer <renderer>");
}

} // namespace

int main(int argc, char **argv)
{
   @autoreleasepool {
      const char *mode = argc > 1 ? argv[1] : "";
      if (!strcmp(mode, "renderer") && argc == 3) {
         install_handlers(20);
         run_renderer(argv[2]);
      }
      install_handlers(100);
      tee_start();
      if (!strcmp(mode, "direct") && argc == 3) {
         run_windowed(argv[2], argv[2]);
      }
      if (!strcmp(mode, "loader") && argc == 4) {
         const char *files = getenv("VK_DRIVER_FILES");
         if (!files || !*files) setup_error("loader mode needs VK_DRIVER_FILES (the runtime's nvk_bridge_icd.json)");
         loader_mode = true;
         run_windowed(argv[2], argv[3]);
      }
      usage();
   }
}
