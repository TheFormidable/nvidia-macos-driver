/* zink_win_check: native OpenGL in AppKit windows on the installed driver. tools/card/checks_zink.py builds it as
 * {BIN}/<id>/nvmtltest and runs one subcommand per process:
 *
 *   basic        an NSOpenGLView presents 60 frames through the adapter's CAMetalLayer
 *   resize       the drawable and the layer follow two window resizes
 *   ctl          the swap interval and the surface opacity reach the layer, and alpha is forced where it must be
 *   noncurrent   CGLFlushDrawable of a window context that is not current, with no context and with another current
 *   front        a single-buffered window presents on glFlush
 *   thread       a render thread other than main presents 120 frames
 *   clear        clearDrawable and the context's destruction remove the layer
 *   lwjgl        setView:, update and clearDrawable called off main from an image named liblwjgl.dylib
 *                (--lwjgl PATH, tools/card/zink_lwjgl_stub.m) are run on main
 *   formats      multisample, float and depth 32 window formats: what the default framebuffer gets (information);
 *                --renderer software shows what Apple's software renderer gives for the same formats
 *
 * Frames are checked twice and never by screen capture. Before each flush the program reads the back buffer (the
 * front buffer when single-buffered) with glReadPixels. And through the adapter's own present observer
 * (nvk_zink_set_present_observer, zink/nvk_zink.h:66-78, exported by libnvk_zink.dylib, zink/nvk_present.c:41-45) it
 * copies every presented frame from the event's VkBuffer into host memory on the GPU, with Zink's queue lock held by
 * the caller (zink/nvk_present.c:230-234; since 3.0.7 a window's frame is observed on the adapter's presentation
 * thread, :471-473, and a swap with an observer set waits for it), and compares every pixel. The observer is called
 * after the copy into the drawable has completed, so a frame it sees is the frame CAMetalLayer gets; whether the
 * window server composites it is not checked (that would need screen recording). One case is observed twice: a
 * frame whose mark the kernel refuses (the drawable has another block of video memory by then) is sent again through
 * the first of those two places, so its first event may name a buffer the drawable no longer has and its second
 * names the one that is shown. Both hold the same frame.
 *
 * Windows: 400x300 or smaller, borderless, floating, not restorable, ignoring the mouse, ordered front without
 * activating the application (activation policy Accessory, orderFrontRegardless), closed at the end. The program first
 * checks for a usable GUI session (exit 3 otherwise). Nothing is written to a file. It has run once, on one Mac, on
 * macOS 26.7.3 (25G342) with release 3.0.1's package. */
#define VK_NO_PROTOTYPES 1
#include <vulkan/vulkan.h>
#include "zink_common.h"
#import <Cocoa/Cocoa.h>
#import <QuartzCore/QuartzCore.h>
#include "nvk_zink.h"

/* ---- the GUI session --------------------------------------------------------------------------------------------- */

static void zw_require_gui(void)
{
   CFDictionaryRef session = CGSessionCopyCurrentDictionary();
   if (!session) zk_skip("no GUI session for this process (CGSessionCopyCurrentDictionary is NULL)");
   CFTypeRef console = CFDictionaryGetValue(session, kCGSessionOnConsoleKey);
   CFTypeRef locked = CFDictionaryGetValue(session, CFSTR("CGSSessionScreenIsLocked"));
   bool on_console = console && CFGetTypeID(console) == CFBooleanGetTypeID() && CFBooleanGetValue(console);
   bool is_locked = locked && CFGetTypeID(locked) == CFBooleanGetTypeID() && CFBooleanGetValue(locked);
   CFRelease(session);
   if (!on_console) zk_skip("the GUI session is not on the console");
   if (is_locked) zk_skip("the screen is locked");
   if (CGDisplayIsAsleep(CGMainDisplayID())) zk_skip("the main display is asleep");
}

/* ---- the present observer ---------------------------------------------------------------------------------------- */

typedef struct {
   bool armed;
   uint32_t w, h, split;            /* GL rows y >= split hold the top colour */
   unsigned char top[4], bottom[4]; /* RGBA as presented */
   int alpha_tol;
} zw_expect;

static pthread_mutex_t zw_lock = PTHREAD_MUTEX_INITIALIZER;
static zw_expect zw_armed, zw_next;
static struct {
   int calls, unarmed, matched, mismatched, failed, host_backing;
   uint32_t last_w, last_h;
   char first_mismatch[320], first_failure[200];
} zw_obs;
static void (*zw_set_observer)(nvk_zink_present_observer);

/* Every pixel of the frame against the expectation. The CAMetalLayer path does not flip: row 0 of the buffer is the
 * top row of the image (zink/nvk_zink.c:518-520). Colour within 1, alpha within alpha_tol. */
static bool zw_pixels(const unsigned char *data, const struct nvk_zink_present_event *e, const zw_expect *x, char *why,
                      size_t size)
{
   if (e->width != x->w || e->height != x->h) {
      snprintf(why, size, "frame size got=%ux%u expected=%ux%u", e->width, e->height, x->w, x->h);
      return false;
   }
   for (uint32_t r = 0; r < e->height; ++r) {
      uint32_t gl_y = e->height - 1 - r;
      const unsigned char *want = gl_y >= x->split ? x->top : x->bottom;
      const unsigned char *p = data + (size_t)r * e->row;
      for (uint32_t c = 0; c < e->width; ++c, p += 4)
         if (abs((int)p[2] - want[0]) > 1 || abs((int)p[1] - want[1]) > 1 || abs((int)p[0] - want[2]) > 1 ||
             abs((int)p[3] - want[3]) > x->alpha_tol) {
            snprintf(why, size, "drawable x %u row %u (GL y %u) got=BGRA(%u,%u,%u,%u) expected=BGRA(%u,%u,%u,%u)%s", c,
                     r, gl_y, p[0], p[1], p[2], p[3], want[2], want[1], want[0], want[3],
                     x->alpha_tol ? " alpha within 1" : "");
            return false;
         }
   }
   return true;
}

/* 1 the frame matches, 0 it does not, -1 the check's own copy failed before the GPU ran it, -2 the check's own copy
 * did not complete (its objects are then left alone, as uncertain GPU work must be). */
static int zw_compare(const struct nvk_zink_present_event *e, const zw_expect *x, char *why, size_t size)
{
   PFN_vkGetDeviceProcAddr gdpa = (PFN_vkGetDeviceProcAddr)e->get_device_proc_addr;
   VkDevice device = (VkDevice)e->device;
   VkQueue queue = (VkQueue)e->queue;
   VkBuffer source = (VkBuffer)(uintptr_t)e->buffer;
   if (!gdpa || !device || !queue || !source || !e->width || !e->height || e->row < e->width * 4) {
      snprintf(why, size, "the event has a null handle or a bad size (row %u, %ux%u)", e->row, e->width, e->height);
      return -1;
   }
#define ZW_FN(n)                                                                                                       \
   PFN_vk##n n = (PFN_vk##n)gdpa(device, "vk" #n);                                                                     \
   if (!n) {                                                                                                           \
      snprintf(why, size, "vk" #n " is not available from the event's device");                                       \
      return -1;                                                                                                       \
   }
   ZW_FN(CreateBuffer)
   ZW_FN(DestroyBuffer)
   ZW_FN(GetBufferMemoryRequirements)
   ZW_FN(AllocateMemory)
   ZW_FN(FreeMemory)
   ZW_FN(BindBufferMemory)
   ZW_FN(MapMemory)
   ZW_FN(UnmapMemory)
   ZW_FN(CreateCommandPool)
   ZW_FN(DestroyCommandPool)
   ZW_FN(AllocateCommandBuffers)
   ZW_FN(BeginCommandBuffer)
   ZW_FN(EndCommandBuffer)
   ZW_FN(CmdPipelineBarrier)
   ZW_FN(CmdCopyBuffer)
   ZW_FN(CreateFence)
   ZW_FN(DestroyFence)
   ZW_FN(QueueSubmit)
   ZW_FN(WaitForFences)
#undef ZW_FN
   VkDeviceSize bytes = (VkDeviceSize)e->row * e->height;
   VkBuffer target = VK_NULL_HANDLE;
   VkDeviceMemory memory = VK_NULL_HANDLE;
   VkCommandPool pool = VK_NULL_HANDLE;
   VkCommandBuffer command = VK_NULL_HANDLE;
   VkFence fence = VK_NULL_HANDLE;
   VkMemoryRequirements req;
   void *data = NULL;
   int verdict = -1;
   do {
      VkBufferCreateInfo bi = {.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = bytes,
                               .usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT, .sharingMode = VK_SHARING_MODE_EXCLUSIVE};
      if (CreateBuffer(device, &bi, NULL, &target) != VK_SUCCESS) {
         snprintf(why, size, "vkCreateBuffer failed");
         break;
      }
      GetBufferMemoryRequirements(device, target, &req);
      /* host_coherent_types: the memory types that are host visible and coherent (zink/nvk_present.c:223-227,
       * :463-467). */
      uint32_t types = req.memoryTypeBits & e->host_coherent_types, type = 0;
      if (!types) {
         snprintf(why, size, "no host-coherent memory type for the copy (types 0x%x, host 0x%x)", req.memoryTypeBits,
                  e->host_coherent_types);
         break;
      }
      while (!(types & (1u << type))) ++type;
      VkMemoryAllocateInfo ai = {.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = req.size,
                                 .memoryTypeIndex = type};
      if (AllocateMemory(device, &ai, NULL, &memory) != VK_SUCCESS || BindBufferMemory(device, target, memory, 0)) {
         snprintf(why, size, "host memory for the copy could not be allocated or bound");
         break;
      }
      VkCommandPoolCreateInfo pi = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
                                    .flags = VK_COMMAND_POOL_CREATE_TRANSIENT_BIT, .queueFamilyIndex = e->queue_family};
      if (CreateCommandPool(device, &pi, NULL, &pool) != VK_SUCCESS) {
         snprintf(why, size, "vkCreateCommandPool failed");
         break;
      }
      VkCommandBufferAllocateInfo ci = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, .commandPool = pool,
                                        .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
      VkCommandBufferBeginInfo begin = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
                                        .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT};
      if (AllocateCommandBuffers(device, &ci, &command) != VK_SUCCESS ||
          BeginCommandBuffer(command, &begin) != VK_SUCCESS) {
         snprintf(why, size, "the copy's command buffer could not be started");
         break;
      }
      VkMemoryBarrier before = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER,
                                .srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT,
                                .dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT};
      CmdPipelineBarrier(command, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 1, &before, 0,
                         NULL, 0, NULL);
      VkBufferCopy region = {0, 0, bytes};
      CmdCopyBuffer(command, source, target, 1, &region);
      VkMemoryBarrier after = {.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT,
                               .dstAccessMask = VK_ACCESS_HOST_READ_BIT};
      CmdPipelineBarrier(command, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &after, 0, NULL, 0,
                         NULL);
      VkFenceCreateInfo fi = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
      if (EndCommandBuffer(command) != VK_SUCCESS || CreateFence(device, &fi, NULL, &fence) != VK_SUCCESS) {
         snprintf(why, size, "the copy's command buffer could not be ended or its fence made");
         break;
      }
      VkSubmitInfo si = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &command};
      /* The caller holds Zink's queue lock (zink/nvk_present.c:230-234, :471-473), which is what vkQueueSubmit
       * needs. */
      if (QueueSubmit(queue, 1, &si, fence) != VK_SUCCESS) {
         snprintf(why, size, "vkQueueSubmit of the copy failed");
         break;
      }
      VkResult waited = WaitForFences(device, 1, &fence, VK_TRUE, 5000000000ull);
      if (waited != VK_SUCCESS) {
         snprintf(why, size, "the check's own copy did not complete within 5 s (VkResult %d)", (int)waited);
         return -2;
      }
      if (MapMemory(device, memory, 0, VK_WHOLE_SIZE, 0, &data) != VK_SUCCESS || !data) {
         snprintf(why, size, "vkMapMemory of the copy failed");
         break;
      }
      verdict = zw_pixels(data, e, x, why, size) ? 1 : 0;
      UnmapMemory(device, memory);
   } while (0);
   if (fence) DestroyFence(device, fence, NULL);
   if (pool) DestroyCommandPool(device, pool, NULL);
   if (target) DestroyBuffer(device, target, NULL);
   if (memory) FreeMemory(device, memory, NULL);
   return verdict;
}

/* The adapter's contract (zink/nvk_zink.h:66-70): 1 verified, 0 a safe failure (the present then fails), -1 uncertain
 * GPU work (the share group is quarantined). The check records what it saw and returns 1, so that only the driver's
 * own failures end a present; -1 only when its own copy did not complete. */
static int zw_observer(const struct nvk_zink_present_event *e)
{
   zw_expect x;
   pthread_mutex_lock(&zw_lock);
   x = zw_armed;
   ++zw_obs.calls;
   if (!x.armed) ++zw_obs.unarmed;
   pthread_mutex_unlock(&zw_lock);
   if (!x.armed) return 1;
   char why[320] = "";
   int verdict = zw_compare(e, &x, why, sizeof(why));
   pthread_mutex_lock(&zw_lock);
   if (verdict == 1) ++zw_obs.matched;
   else if (verdict == 0) {
      ++zw_obs.mismatched;
      if (!zw_obs.first_mismatch[0]) snprintf(zw_obs.first_mismatch, sizeof(zw_obs.first_mismatch), "%s", why);
   } else {
      ++zw_obs.failed;
      if (!zw_obs.first_failure[0]) snprintf(zw_obs.first_failure, sizeof(zw_obs.first_failure), "%s", why);
   }
   zw_obs.last_w = e->width;
   zw_obs.last_h = e->height;
   if (e->host_backing) ++zw_obs.host_backing;
   pthread_mutex_unlock(&zw_lock);
   return verdict == -2 ? -1 : 1;
}

/* After the first context is judged Zink's: libnvk_zink.dylib is loaded (libnvk_cgl.dylib links it). */
static void zw_install_observer(void)
{
   if (zk_software) return;
   zw_set_observer = (void (*)(nvk_zink_present_observer))zk_symbol(ZK_ZINK, "nvk_zink_set_present_observer");
   if (!zw_set_observer) {
      zk_fail("observer", "nvk_zink_set_present_observer is not exported by the loaded ...%s", ZK_ZINK);
      zk_finish();
   }
   zw_set_observer(zw_observer);
}

static _Noreturn void zw_end(void)
{
   if (zw_set_observer) zw_set_observer(NULL);
   pthread_mutex_lock(&zw_lock);
   int unarmed = zw_obs.unarmed, failed = zw_obs.failed, backing = zw_obs.host_backing;
   char failure[200];
   snprintf(failure, sizeof(failure), "%s", zw_obs.first_failure);
   pthread_mutex_unlock(&zw_lock);
   if (unarmed) zk_info("the observer saw %d presents that no frame of this check had armed", unarmed);
   if (backing) zk_info("%d observed frames went through the host-backing copy (after a VRAM import refusal)", backing);
   if (failed) zk_fail("observer-copy", "the check's own GPU copy failed %d times, first: %s", failed, failure);
   zk_finish();
}

/* ---- windows ----------------------------------------------------------------------------------------------------- */

@interface ZKWin : NSObject
@property(strong) NSWindow *window;
@property(strong) NSView *view;
@property(strong) NSOpenGLContext *context;
@property int w, h;
@property BOOL frontOnly;
@end
@implementation ZKWin
@end

/* AppKit's own redraws draw nothing; the check draws. */
@interface ZKGLView : NSOpenGLView
@end
@implementation ZKGLView
- (void)drawRect:(NSRect)dirtyRect
{
   (void)dirtyRect;
}
@end

/* A bare NSView as LWJGL uses; it records on which threads AppKit and the adapter ask it for its surface resolution
 * (the adapter's publish_view_size reads it, zink/appkit.m:66-77). zw_in_stub is set while the render thread is inside
 * one of the stub's calls: a read on main then is the call run on main (zink/appkit.m:88-127), a read off main then is
 * the call run on the render thread, and a read at any other time is AppKit's own (counted, not judged). */
static atomic_int zw_in_stub, zw_bestres_main, zw_bestres_other, zw_bestres_outside;
@interface ZKPlainView : NSView
@end
@implementation ZKPlainView
- (BOOL)wantsBestResolutionOpenGLSurface
{
   if (!atomic_load(&zw_in_stub)) atomic_fetch_add(&zw_bestres_outside, 1);
   else if ([NSThread isMainThread]) atomic_fetch_add(&zw_bestres_main, 1);
   else atomic_fetch_add(&zw_bestres_other, 1);
   return NO;
}
@end

static void zw_pump(double seconds)
{
   double end = zk_now() + seconds;
   do {
      @autoreleasepool {
         NSEvent *event = [NSApp nextEventMatchingMask:NSEventMaskAny
                                             untilDate:[NSDate dateWithTimeIntervalSinceNow:0.005]
                                                inMode:NSDefaultRunLoopMode
                                               dequeue:YES];
         if (event) [NSApp sendEvent:event];
      }
   } while (zk_now() < end);
}

/* Keeps main's run loop (and so the main dispatch queue) running until *state reaches value. */
static bool zw_pump_until(atomic_int *state, int value, double seconds)
{
   double end = zk_now() + seconds;
   while (atomic_load(state) < value && zk_now() < end) zw_pump(0.01);
   return atomic_load(state) >= value;
}

typedef struct {
   bool db, alpha, software, color_float;
   int samples, depth, stencil;
} zw_format;

static const zw_format zw_default = {.db = true, .alpha = true, .depth = 24, .stencil = 8};

static NSOpenGLPixelFormat *zw_pixel_format(zw_format f, bool core41)
{
   NSOpenGLPixelFormatAttribute a[40];
   int n = 0;
   a[n++] = NSOpenGLPFAOpenGLProfile;
   a[n++] = core41 ? NSOpenGLProfileVersion4_1Core : NSOpenGLProfileVersion3_2Core;
   if (f.software) {
      a[n++] = NSOpenGLPFARendererID;
      a[n++] = kCGLRendererGenericFloatID;
   } else {
      a[n++] = NSOpenGLPFAAccelerated;
      a[n++] = NSOpenGLPFANoRecovery;
   }
   if (f.db) a[n++] = NSOpenGLPFADoubleBuffer;
   if (f.color_float) {
      a[n++] = NSOpenGLPFAColorFloat;
      a[n++] = NSOpenGLPFAColorSize;
      a[n++] = 64;
      a[n++] = NSOpenGLPFAAlphaSize;
      a[n++] = 16;
   } else {
      a[n++] = NSOpenGLPFAColorSize;
      a[n++] = 24;
      a[n++] = NSOpenGLPFAAlphaSize;
      a[n++] = f.alpha ? 8 : 0;
   }
   a[n++] = NSOpenGLPFADepthSize;
   a[n++] = (NSOpenGLPixelFormatAttribute)f.depth;
   a[n++] = NSOpenGLPFAStencilSize;
   a[n++] = (NSOpenGLPixelFormatAttribute)f.stencil;
   if (f.samples) {
      a[n++] = NSOpenGLPFAMultisample;
      a[n++] = NSOpenGLPFASampleBuffers;
      a[n++] = 1;
      a[n++] = NSOpenGLPFASamples;
      a[n++] = (NSOpenGLPixelFormatAttribute)f.samples;
   }
   a[n] = 0;
   NSOpenGLPixelFormat *pf = [[NSOpenGLPixelFormat alloc] initWithAttributes:a];
   if (!pf && f.software && core41) {
      pf = zw_pixel_format(f, false);
      if (pf) zk_info("the software renderer has no 4.1 core window format here; the control uses 3.2 core");
   }
   return pf;
}

/* A borderless window at (64,64) holding an NSOpenGLView, or a bare view with its own context. attach: the context is
 * given the view on main, as an application does. nil when the pixel format does not exist. */
static ZKWin *zw_open(int w, int h, zw_format f, bool plain, bool attach)
{
   NSOpenGLPixelFormat *pf = zw_pixel_format(f, true);
   if (!pf) return nil;
   ZKWin *z = [ZKWin new];
   z.w = w;
   z.h = h;
   z.frontOnly = !f.db;
   NSRect frame = NSMakeRect(0, 0, w, h);
   NSView *view;
   if (plain) {
      view = [[ZKPlainView alloc] initWithFrame:frame];
      z.context = [[NSOpenGLContext alloc] initWithFormat:pf shareContext:nil];
   } else {
      ZKGLView *glview = [[ZKGLView alloc] initWithFrame:frame pixelFormat:pf];
      glview.wantsBestResolutionOpenGLSurface = NO;
      view = glview;
      z.context = glview.openGLContext;
   }
   if (!z.context) zk_setup_error("no NSOpenGLContext for an existing pixel format");
   NSWindow *window = [[NSWindow alloc] initWithContentRect:NSMakeRect(64, 64, w, h)
                                                  styleMask:NSWindowStyleMaskBorderless
                                                    backing:NSBackingStoreBuffered
                                                      defer:NO];
   window.releasedWhenClosed = NO;
   window.restorable = NO;
   window.level = NSFloatingWindowLevel;
   window.ignoresMouseEvents = YES;
   window.title = @"native OpenGL card check";
   window.contentView = view;
   [window orderFrontRegardless];
   z.window = window;
   z.view = view;
   zw_pump(0.1);
   if (attach) [z.context setView:view];
   return z;
}

static void zw_close(ZKWin *z)
{
   [NSOpenGLContext clearCurrentContext];
   [z.window orderOut:nil];
   [z.window close];
   zw_pump(0.05);
}

static CAMetalLayer *zw_layer(NSView *view, int *count)
{
   CAMetalLayer *found = nil;
   *count = 0;
   for (CALayer *layer in view.layer.sublayers)
      if ([layer isKindOfClass:[CAMetalLayer class]]) {
         found = (CAMetalLayer *)layer;
         ++*count;
      }
   return found;
}

/* The adapter's sublayer (zink/native_drawable.m:113-150): one CAMetalLayer, BGRA8, sized as the drawable, on the
 * card's Metal device (matched by the NVAccel registry entry, :56-78). */
static void zw_check_layer(const char *name, ZKWin *z)
{
   int count = 0;
   CAMetalLayer *layer = zw_layer(z.view, &count);
   if (count != 1) {
      zk_fail(name, "CAMetalLayer sublayers of the view got=%d expected=1", count);
      return;
   }
   CGSize size = layer.drawableSize;
   uint64_t registry = layer.device.registryID;
   NSString *cls = layer.device ? NSStringFromClass([(NSObject *)layer.device class]) : @"none";
   if ((int)size.width == z.w && (int)size.height == z.h && layer.pixelFormat == MTLPixelFormatBGRA8Unorm &&
       registry == zk_device_registry)
      zk_pass(name, "one CAMetalLayer, drawableSize %dx%d, BGRA8Unorm, on the card's device (%s)", z.w, z.h,
              cls.UTF8String);
   else
      zk_fail(name, "CAMetalLayer got=%gx%g format %lu device 0x%llx (%s) expected=%dx%d format %lu device 0x%llx",
              size.width, size.height, (unsigned long)layer.pixelFormat, (unsigned long long)registry, cls.UTF8String,
              z.w, z.h, (unsigned long)MTLPixelFormatBGRA8Unorm, (unsigned long long)zk_device_registry);
}

/* ---- frames ------------------------------------------------------------------------------------------------------ */

typedef struct {
   int frames, seen, matched, bad, readback_bad, last_error_bad;
   char first_bad[320], first_readback[200], first_last_error[200];
} zw_tally;

typedef struct {
   int clear, read, seen, tol; /* the alpha cleared, read back, and expected in the drawable */
} zw_alpha;
static const zw_alpha zw_opaque = {255, 255, 255, 0};

enum { ZW_FLUSH_BUFFER, ZW_GL_FLUSH, ZW_CGL_FLUSH };

static void zw_colours(int index, int alpha, unsigned char *top, unsigned char *bottom)
{
   bottom[0] = (unsigned char)(index * 4 % 256);
   bottom[1] = 80;
   bottom[2] = (unsigned char)(200 - index % 100);
   bottom[3] = (unsigned char)alpha;
   top[0] = 30;
   top[1] = (unsigned char)(255 - index * 3 % 256);
   top[2] = (unsigned char)(index * 5 % 256);
   top[3] = (unsigned char)alpha;
}

static void zw_clear(const unsigned char *rgba)
{
   glClearColor(rgba[0] / 255.0f, rgba[1] / 255.0f, rgba[2] / 255.0f, rgba[3] / 255.0f);
   glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
}

/* Draws frame index (bottom half one colour, top half another, by scissored clears), makes the context current first,
 * and checks four pixels of the drawn buffer exactly, the top-right corner among them, which shows that the drawable
 * has the window's size. The expectation for the drawable is left in zw_next. */
static void zw_draw(ZKWin *z, int index, zw_alpha a, zw_tally *t)
{
   [z.context makeCurrentContext];
   int w = z.w, h = z.h, split = h / 2;
   unsigned char top[4], bottom[4];
   zw_colours(index, a.clear, top, bottom);
   glBindFramebuffer(GL_FRAMEBUFFER, 0);
   glViewport(0, 0, w, h);
   if (z.frontOnly) {
      glDrawBuffer(GL_FRONT);
      glReadBuffer(GL_FRONT);
   }
   glEnable(GL_SCISSOR_TEST);
   glScissor(0, 0, w, split);
   zw_clear(bottom);
   glScissor(0, split, w, h - split);
   zw_clear(top);
   glDisable(GL_SCISSOR_TEST);
   const int at[4][3] = {{w / 4, split / 2, 0}, {w / 4, split + (h - split) / 2, 1}, {w - 1, h - 1, 1}, {0, 0, 0}};
   for (int i = 0; i < 4; ++i) {
      unsigned char got[4], want[4];
      memcpy(want, at[i][2] ? top : bottom, 4);
      want[3] = (unsigned char)a.read;
      memset(got, 0xee, 4);
      glReadPixels(at[i][0], at[i][1], 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, got);
      if (memcmp(got, want, 4)) {
         if (!t->readback_bad)
            snprintf(t->first_readback, sizeof(t->first_readback),
                     "frame %d pixel (%d,%d) of %dx%d got=(%u,%u,%u,%u) expected=(%u,%u,%u,%u)", index, at[i][0],
                     at[i][1], w, h, got[0], got[1], got[2], got[3], want[0], want[1], want[2], want[3]);
         ++t->readback_bad;
         break;
      }
   }
   GLenum e = zk_gl_error();
   if (e) {
      if (!t->readback_bad)
         snprintf(t->first_readback, sizeof(t->first_readback), "frame %d: glGetError 0x%x", index, e);
      ++t->readback_bad;
   }
   zw_expect x = {.armed = true, .w = (uint32_t)w, .h = (uint32_t)h, .split = (uint32_t)split, .alpha_tol = a.tol};
   memcpy(x.top, top, 4);
   memcpy(x.bottom, bottom, 4);
   x.top[3] = x.bottom[3] = (unsigned char)a.seen;
   zw_next = x;
}

/* Presents what zw_draw drew and counts what the observer saw of it. */
static void zw_present(ZKWin *z, int how, zw_tally *t)
{
   pthread_mutex_lock(&zw_lock);
   int matched = zw_obs.matched, mismatched = zw_obs.mismatched, failed = zw_obs.failed;
   zw_armed = zw_next;
   pthread_mutex_unlock(&zw_lock);
   if (how == ZW_FLUSH_BUFFER) [z.context flushBuffer];
   else if (how == ZW_GL_FLUSH) glFlush();
   else CGLFlushDrawable(z.context.CGLContextObj);
   pthread_mutex_lock(&zw_lock);
   zw_armed.armed = false;
   int dm = zw_obs.matched - matched, dx = zw_obs.mismatched - mismatched, df = zw_obs.failed - failed;
   if (dx && !t->bad) snprintf(t->first_bad, sizeof(t->first_bad), "frame %d: %s", t->frames, zw_obs.first_mismatch);
   pthread_mutex_unlock(&zw_lock);
   ++t->frames;
   if (dm + dx + df) ++t->seen;
   if (dm && !dx) ++t->matched;
   if (dx) ++t->bad;
   if (!zk_software) {
      /* nvk_cgl_last_error() is per thread; a present that only defers (returns 2) clears it
       * (zink/nvk_cgl.c:1127-1162). */
      const char *last = zk_last_error();
      if (strcmp(last, "none")) {
         if (!t->last_error_bad)
            snprintf(t->first_last_error, sizeof(t->first_last_error), "frame %d: %s", t->frames - 1, last);
         ++t->last_error_bad;
      }
   }
}

static void zw_frame(ZKWin *z, int index, zw_alpha a, int how, zw_tally *t)
{
   zw_draw(z, index, a, t);
   zw_present(z, how, t);
}

/* The readbacks, the observed frames (at least min_seen of them, every one matching), and the adapter's last error. */
static void zw_report(const char *name, const zw_tally *t, int min_seen)
{
   char n[64];
   snprintf(n, sizeof(n), "%s-readback", name);
   if (!t->readback_bad) zk_pass(n, "%d frames read back exactly before their flush", t->frames);
   else zk_fail(n, "%d of %d frames wrong, first %s", t->readback_bad, t->frames, t->first_readback);
   if (zk_software) return;
   snprintf(n, sizeof(n), "%s-drawable", name);
   if (t->seen >= min_seen && !t->bad && t->matched == t->seen)
      zk_pass(n, "the observer saw %d of %d frames, every pixel as drawn", t->seen, t->frames);
   else if (t->bad) zk_fail(n, "%d of %d observed frames differ, first %s", t->bad, t->seen, t->first_bad);
   else zk_fail(n, "the observer verified %d of %d frames (%d seen) expected=at least %d", t->matched, t->frames,
                t->seen, min_seen);
   snprintf(n, sizeof(n), "%s-last-error", name);
   if (!t->last_error_bad) zk_pass(n, "nvk_cgl_last_error() is none after every flush");
   else zk_fail(n, "nvk_cgl_last_error() got=%s expected=none (%d flushes)", t->first_last_error, t->last_error_bad);
}

/* The first context of the check, current on main: Zink's (or the software control), then the observer. */
static void zw_gate_main(ZKWin *z)
{
   [z.context makeCurrentContext];
   zk_gate(zk_string(GL_RENDERER));
   zw_install_observer();
}

/* ---- basic, resize, ctl, noncurrent, front ----------------------------------------------------------------------- */

static ZKWin *zw_open_or_fail(int w, int h, zw_format f, bool plain, bool attach)
{
   ZKWin *z = zw_open(w, h, f, plain, attach);
   if (!z) zk_setup_error("no pixel format for a %dx%d %s window", w, h, f.db ? "double-buffered" : "single-buffered");
   return z;
}

static void sub_basic(void)
{
   ZKWin *z = zw_open_or_fail(320, 240, zw_default, false, true);
   zw_gate_main(z);
   GLint interval = -1;
   CGLGetParameter(z.context.CGLContextObj, kCGLCPSwapInterval, &interval);
   zk_info("swap interval %d", (int)interval);
   zw_tally t = {0};
   for (int i = 0; i < 60; ++i) {
      zw_frame(z, i, zw_opaque, ZW_FLUSH_BUFFER, &t);
      zw_pump(0.004);
   }
   /* The first frame after the layer's setup may be deferred (returns 2, zink/nvk_cgl.c:1152). */
   zw_report("frames", &t, 57);
   zw_check_layer("layer", z);
   zw_close(z);
   zw_end();
}

static void zw_resize(ZKWin *z, int w, int h)
{
   [z.window setContentSize:NSMakeSize(w, h)];
   [z.context update]; /* as NSOpenGLView does on a frame change; the adapter's hook publishes the size (appkit.m:98) */
   z.w = w;
   z.h = h;
   zw_pump(0.2);
}

static void sub_resize(void)
{
   ZKWin *z = zw_open_or_fail(320, 240, zw_default, false, true);
   zw_gate_main(z);
   zw_tally t0 = {0}, t1 = {0}, t2 = {0};
   for (int i = 0; i < 20; ++i) zw_frame(z, i, zw_opaque, ZW_FLUSH_BUFFER, &t0);
   zw_report("320x240", &t0, 17);
   /* The drawable follows at the next GL call (zink/nvk_cgl.c:195-207, :299-326); at most 3 frames per size may
    * miss the observer while the layer is reconfigured (zink/native_drawable.m:155-170). */
   zw_resize(z, 400, 300);
   for (int i = 0; i < 20; ++i) zw_frame(z, 20 + i, zw_opaque, ZW_FLUSH_BUFFER, &t1);
   zw_report("400x300", &t1, 17);
   zw_check_layer("400x300-layer", z);
   zw_resize(z, 200, 150);
   for (int i = 0; i < 20; ++i) zw_frame(z, 40 + i, zw_opaque, ZW_FLUSH_BUFFER, &t2);
   zw_report("200x150", &t2, 17);
   zw_check_layer("200x150-layer", z);
   zw_close(z);
   zw_end();
}

static CAMetalLayer *zw_only_layer(const char *name, ZKWin *z)
{
   int count = 0;
   CAMetalLayer *layer = zw_layer(z.view, &count);
   if (count != 1) {
      zk_fail(name, "CAMetalLayer sublayers got=%d expected=1", count);
      return nil;
   }
   return layer;
}

static void sub_ctl(void)
{
   ZKWin *z = zw_open_or_fail(320, 240, zw_default, false, true);
   zw_gate_main(z);
   CGLContextObj cgl = z.context.CGLContextObj;
   const GLint zero = 0, one = 1;
   zw_tally warm = {0}, i0 = {0}, i1 = {0}, o0 = {0}, o1 = {0};
   for (int i = 0; i < 3; ++i) zw_frame(z, i, zw_opaque, ZW_FLUSH_BUFFER, &warm);
   /* (a) displaySyncEnabled = (kCGLCPSwapInterval != 0) (zink/native_drawable.m:136, :120). */
   CGLSetParameter(cgl, kCGLCPSwapInterval, &zero);
   for (int i = 0; i < 3; ++i) zw_frame(z, 10 + i, zw_opaque, ZW_FLUSH_BUFFER, &i0);
   zw_pump(0.05);
   CAMetalLayer *layer = zw_only_layer("interval-0", z);
   if (layer && !layer.displaySyncEnabled) zk_pass("interval-0", "swap interval 0: displaySyncEnabled NO");
   else if (layer) zk_fail("interval-0", "swap interval 0: displaySyncEnabled got=YES expected=NO");
   zw_report("interval-0", &i0, 2);
   CGLSetParameter(cgl, kCGLCPSwapInterval, &one);
   for (int i = 0; i < 3; ++i) zw_frame(z, 20 + i, zw_opaque, ZW_FLUSH_BUFFER, &i1);
   zw_pump(0.05);
   layer = zw_only_layer("interval-1", z);
   if (layer && layer.displaySyncEnabled) zk_pass("interval-1", "swap interval 1: displaySyncEnabled YES");
   else if (layer) zk_fail("interval-1", "swap interval 1: displaySyncEnabled got=NO expected=YES");
   zw_report("interval-1", &i1, 2);
   /* (b) layer.opaque = kCGLCPSurfaceOpacity (zink/native_drawable.m:115, zink/nvk_cgl.c:1058-1063); an opaque surface
    * gets alpha 1 in the copy (zink/nvk_zink.c:441-446). 0.5 is 127.5 in 8 bits, hence alpha within 1. */
   CGLSetParameter(cgl, kCGLCPSurfaceOpacity, &zero);
   const zw_alpha half = {128, 128, 128, 1}, forced = {128, 128, 255, 0};
   for (int i = 0; i < 3; ++i) zw_frame(z, 30 + i, half, ZW_FLUSH_BUFFER, &o0);
   zw_pump(0.05);
   layer = zw_only_layer("opacity-0", z);
   if (layer && !layer.opaque) zk_pass("opacity-0", "surface opacity 0: layer.opaque NO");
   else if (layer) zk_fail("opacity-0", "surface opacity 0: layer.opaque got=YES expected=NO");
   zw_report("opacity-0", &o0, 2);
   CGLSetParameter(cgl, kCGLCPSurfaceOpacity, &one);
   for (int i = 0; i < 3; ++i) zw_frame(z, 40 + i, forced, ZW_FLUSH_BUFFER, &o1);
   zw_pump(0.05);
   layer = zw_only_layer("opacity-1", z);
   if (layer && layer.opaque) zk_pass("opacity-1", "surface opacity 1: layer.opaque YES, alpha forced to 255");
   else if (layer) zk_fail("opacity-1", "surface opacity 1: layer.opaque got=NO expected=YES");
   zw_report("opacity-1", &o1, 2);
   zw_close(z);

   /* (c) a format without alpha gives an RGBX drawable, whose copy shows alpha 1 even when not opaque. Judged only
    * when the pixel format really has no alpha (the adapter mirrors the described alpha size,
    * zink/nvk_cgl.c:755-758). */
   zw_format noalpha = zw_default;
   noalpha.alpha = false;
   ZKWin *y = zw_open(320, 240, noalpha, false, true);
   if (!y) {
      zk_info("no window pixel format with alpha size 0; (c) not run");
   } else {
      GLint alpha_size = -1;
      CGLDescribePixelFormat(CGLGetPixelFormat(y.context.CGLContextObj), 0, kCGLPFAAlphaSize, &alpha_size);
      [y.context makeCurrentContext];
      if (zk_same_renderer("rgbx")) {
         CGLSetParameter(y.context.CGLContextObj, kCGLCPSurfaceOpacity, &zero);
         zw_tally r = {0};
         if (alpha_size == 0) {
            const zw_alpha rgbx = {128, 255, 255, 0};
            for (int i = 0; i < 3; ++i) zw_frame(y, 50 + i, rgbx, ZW_FLUSH_BUFFER, &r);
            zw_report("rgbx", &r, 2);
         } else {
            zk_info("the alpha-size-0 window format has alpha size %d; (c) not judged", (int)alpha_size);
         }
      }
      zw_close(y);
   }
   zw_end();
}

static void sub_noncurrent(void)
{
   ZKWin *z = zw_open_or_fail(320, 240, zw_default, false, true);
   zw_gate_main(z);
   CGLPixelFormatAttribute xa[] = {kCGLPFAOpenGLProfile, (CGLPixelFormatAttribute)kCGLOGLPVersion_GL4_Core,
                                   kCGLPFAAccelerated, kCGLPFANoRecovery, kCGLPFAColorSize, (CGLPixelFormatAttribute)24,
                                   kCGLPFAAlphaSize, (CGLPixelFormatAttribute)8, (CGLPixelFormatAttribute)0};
   CGLPixelFormatObj xpf = NULL;
   GLint npf = 0;
   CGLContextObj x = NULL;
   if (CGLChoosePixelFormat(xa, &xpf, &npf) || !xpf || CGLCreateContext(xpf, NULL, &x) || !x)
      zk_setup_error("no offscreen context X");
   zw_tally warm = {0}, none = {0}, other = {0};
   for (int i = 0; i < 5; ++i) zw_frame(z, i, zw_opaque, ZW_FLUSH_BUFFER, &warm);
   zw_report("warm-up", &warm, 3);

   /* No context current: swap_drawable finds the bridge by engine and presents by a borrowed bind (zink/nvk_cgl.c:
    * 496-508, :1170-1200). */
   zw_draw(z, 10, zw_opaque, &none);
   CGLSetCurrentContext(NULL);
   zw_present(z, ZW_CGL_FLUSH, &none);
   CGLContextObj after = CGLGetCurrentContext();
   if (none.seen == 1 && none.matched == 1 && !after)
      zk_pass("no-current", "CGLFlushDrawable with no current context presented the frame; still no current context");
   else zk_fail("no-current",
                "observer saw %d (matched %d) expected=1; current context afterwards %s expected=NULL%s%s",
                none.seen, none.matched, after ? "not NULL" : "NULL", none.bad ? ": " : "", none.first_bad);

   /* Another context current: the caller's binding is restored, and X keeps working. */
   zw_draw(z, 11, zw_opaque, &other);
   if (CGLSetCurrentContext(x)) zk_setup_error("CGLSetCurrentContext(X) failed");
   bool x_ok = zk_same_renderer("other-current");
   GLuint fbo = 0, rb = 0;
   glGenFramebuffers(1, &fbo);
   glGenRenderbuffers(1, &rb);
   glBindRenderbuffer(GL_RENDERBUFFER, rb);
   glRenderbufferStorage(GL_RENDERBUFFER, GL_RGBA8, 16, 16);
   glBindFramebuffer(GL_FRAMEBUFFER, fbo);
   glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, rb);
   glViewport(0, 0, 16, 16);
   const unsigned char xc[4] = {12, 200, 99, 255};
   zw_clear(xc);
   zw_present(z, ZW_CGL_FLUSH, &other);
   after = CGLGetCurrentContext();
   unsigned char got[4] = {0xee, 0xee, 0xee, 0xee};
   if (after == x) {
      glBindFramebuffer(GL_FRAMEBUFFER, fbo);
      glReadPixels(8, 8, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, got);
   }
   GLenum e = after == x ? zk_gl_error() : 0;
   if (other.seen == 1 && other.matched == 1 && after == x)
      zk_pass("other-current", "CGLFlushDrawable with X current presented the window's frame; X is current again");
   else zk_fail("other-current", "observer saw %d (matched %d) expected=1; current context afterwards is %s%s%s",
                other.seen, other.matched, after == x ? "X" : after ? "another context" : "NULL",
                other.bad ? ": " : "", other.first_bad);
   if (x_ok && after == x && !memcmp(got, xc, 4) && !e)
      zk_pass("other-intact", "X's framebuffer still holds its colour and X's GL calls work");
   else if (x_ok) zk_fail("other-intact", "X's pixel got=(%u,%u,%u,%u) expected=(%u,%u,%u,%u), glGetError 0x%x", got[0],
                         got[1], got[2], got[3], xc[0], xc[1], xc[2], xc[3], e);
   if (after == x) {
      glDeleteFramebuffers(1, &fbo);
      glDeleteRenderbuffers(1, &rb);
   }
   CGLSetCurrentContext(NULL);
   CGLDestroyContext(x);
   CGLDestroyPixelFormat(xpf);
   zw_close(z);
   zw_end();
}

static void sub_front(void)
{
   zw_format single = zw_default;
   single.db = false;
   ZKWin *z = zw_open_or_fail(320, 240, single, false, true);
   zw_gate_main(z);
   /* In a single-buffered context the adapter's glFlush presents the front buffer (zink/nvk_cgl.c:358-363,
    * zink/nvk_zink.c:53-83). */
   zw_tally t = {0};
   for (int i = 0; i < 20; ++i) {
      zw_frame(z, i, zw_opaque, ZW_GL_FLUSH, &t);
      zw_pump(0.004);
   }
   zw_report("front", &t, 18);
   zw_close(z);
   zw_end();
}

/* ---- thread, clear, lwjgl ---------------------------------------------------------------------------------------- */

/* 0 started, 1 the render thread read its renderer, 2 main judged it (go), 3 the thread is done. */
static atomic_int zw_state;
static char zw_thread_renderer[512];
/* nvk_cgl_last_error on the render thread, read there beside the renderer: main judges both. */
static const char *zw_thread_error;
static ZKWin *zw_thread_win;
static zw_tally zw_thread_tally;
static double zw_thread_seconds;
static int (*zw_lwjgl_set_view)(void *, void *);
static int (*zw_lwjgl_update)(void *);
static int (*zw_lwjgl_clear)(void *);
static double zw_lwjgl_seconds[3];
static bool zw_lwjgl_view_ok;

static bool zw_wait_go(void)
{
   double end = zk_now() + 15.0;
   while (atomic_load(&zw_state) < 2 && zk_now() < end) usleep(2000);
   return atomic_load(&zw_state) >= 2;
}

static void *zw_render_main(void *unused)
{
   (void)unused;
   @autoreleasepool {
      ZKWin *z = zw_thread_win;
      [z.context makeCurrentContext];
      snprintf(zw_thread_renderer, sizeof(zw_thread_renderer), "%s", zk_string(GL_RENDERER));
      zw_thread_error = zk_last_error();
      atomic_store(&zw_state, 1);
      if (!zw_wait_go()) return NULL;
      double start = zk_now();
      /* The first present from this thread dispatches the layer's setup to main and is deferred
       * (zink/native_drawable.m:171-180); main keeps its run loop going meanwhile. */
      for (int i = 0; i < 120; ++i) zw_frame(z, i, zw_opaque, ZW_FLUSH_BUFFER, &zw_thread_tally);
      zw_thread_seconds = zk_now() - start;
      [NSOpenGLContext clearCurrentContext];
      atomic_store(&zw_state, 3);
   }
   return NULL;
}

static void *zw_lwjgl_main(void *unused)
{
   (void)unused;
   @autoreleasepool {
      ZKWin *z = zw_thread_win;
      void *context = (__bridge void *)z.context, *view = (__bridge void *)z.view;
      double t0 = zk_now();
      atomic_store(&zw_in_stub, 1);
      zw_lwjgl_set_view(context, view);
      atomic_store(&zw_in_stub, 0);
      zw_lwjgl_seconds[0] = zk_now() - t0;
      zw_lwjgl_view_ok = z.context.view == z.view;
      [z.context makeCurrentContext];
      snprintf(zw_thread_renderer, sizeof(zw_thread_renderer), "%s", zk_string(GL_RENDERER));
      zw_thread_error = zk_last_error();
      atomic_store(&zw_state, 1);
      if (!zw_wait_go()) return NULL;
      t0 = zk_now();
      atomic_store(&zw_in_stub, 1);
      zw_lwjgl_update(context);
      atomic_store(&zw_in_stub, 0);
      zw_lwjgl_seconds[1] = zk_now() - t0;
      double start = zk_now();
      for (int i = 0; i < 30; ++i) zw_frame(z, i, zw_opaque, ZW_FLUSH_BUFFER, &zw_thread_tally);
      zw_thread_seconds = zk_now() - start;
      t0 = zk_now();
      atomic_store(&zw_in_stub, 1);
      zw_lwjgl_clear(context);
      atomic_store(&zw_in_stub, 0);
      zw_lwjgl_seconds[2] = zk_now() - t0;
      [NSOpenGLContext clearCurrentContext];
      atomic_store(&zw_state, 3);
   }
   return NULL;
}

/* Runs body on a render thread while main keeps its run loop, judges the thread's renderer on main and installs the
 * observer before the thread draws. */
static void zw_run_render_thread(void *(*body)(void *), double seconds)
{
   pthread_t thread;
   atomic_store(&zw_state, 0);
   if (pthread_create(&thread, NULL, body, NULL)) zk_setup_error("pthread_create failed");
   pthread_detach(thread);
   if (!zw_pump_until(&zw_state, 1, 10.0)) {
      zk_fail("render-thread", "the render thread did not make its context current within 10 s");
      zw_end();
   }
   zk_gate_error = zw_thread_error;
   zk_gate(zw_thread_renderer);
   zk_gate_error = NULL;
   zw_install_observer();
   atomic_store(&zw_state, 2);
   if (!zw_pump_until(&zw_state, 3, seconds)) {
      zk_fail("render-thread", "the render thread did not finish within %.0f s", seconds);
      zw_end();
   }
}

static void sub_thread(void)
{
   ZKWin *z = zw_open_or_fail(320, 240, zw_default, true, true);
   zw_thread_win = z;
   zw_run_render_thread(zw_render_main, 15.0);
   zw_report("thread", &zw_thread_tally, 110);
   if (zw_thread_seconds <= 10.0)
      zk_pass("thread-time", "120 frames from the render thread in %.1f s", zw_thread_seconds);
   else zk_fail("thread-time", "120 frames took %.1f s expected=at most 10", zw_thread_seconds);
   zw_thread_win = nil;
   zw_close(z);
   zw_end();
}

static void sub_clear(void)
{
   ZKWin *z = nil;
   __weak NSOpenGLContext *weak = nil;
   int count = 0;
   /* Its own pool, so that no autoreleased reference of the steps before keeps the context alive at the end. */
   @autoreleasepool {
      z = zw_open_or_fail(320, 240, zw_default, true, true);
      zw_gate_main(z);
      zw_tally before = {0}, cleared = {0}, again = {0};
      for (int i = 0; i < 10; ++i) zw_frame(z, i, zw_opaque, ZW_FLUSH_BUFFER, &before);
      zw_report("before", &before, 8);
      /* The adapter's clearDrawable hook removes the layer (zink/appkit.m:117-127, zink/native_drawable.m:80-95). */
      [z.context clearDrawable];
      zw_pump(0.2);
      zw_layer(z.view, &count);
      if (!count) zk_pass("clear-layer", "after clearDrawable the view has no CAMetalLayer");
      else zk_fail("clear-layer", "after clearDrawable CAMetalLayer sublayers got=%d expected=0", count);
      zw_draw(z, 20, zw_opaque, &cleared);
      zw_present(z, ZW_FLUSH_BUFFER, &cleared);
      if (!cleared.seen) zk_pass("clear-flush", "flushBuffer without a drawable returned and presented nothing");
      else zk_fail("clear-flush", "flushBuffer without a drawable: the observer saw %d frame(s) expected=0",
                   cleared.seen);
      [z.context setView:z.view];
      zw_pump(0.1);
      for (int i = 0; i < 10; ++i) zw_frame(z, 30 + i, zw_opaque, ZW_FLUSH_BUFFER, &again);
      zw_report("again", &again, 8);
      /* Destroying the context removes the layer too (gldDestroyContext reaches nvk_cgl_destroy_gld, zink/gld_bridge.c:
       * 94-98, which removes the native target, zink/nvk_cgl.c:789). */
      [NSOpenGLContext clearCurrentContext];
      weak = z.context;
      z.context = nil;
   }
   zw_pump(0.2);
   if (weak) {
      zk_info("the NSOpenGLContext is still alive after the check dropped its reference; destruction not judged");
   } else {
      zw_layer(z.view, &count);
      if (!count) zk_pass("destroy-layer", "after the context was destroyed the view has no CAMetalLayer");
      else zk_fail("destroy-layer", "after the context was destroyed CAMetalLayer sublayers got=%d expected=0", count);
   }
   [z.window orderOut:nil];
   [z.window close];
   zw_pump(0.05);
   zw_end();
}

static void sub_lwjgl(const char *stub)
{
   if (!stub) zk_setup_error("lwjgl needs --lwjgl PATH of the stub library");
   const char *base = strrchr(stub, '/');
   if (strcmp(base ? base + 1 : stub, "liblwjgl.dylib"))
      zk_setup_error("the stub must be named liblwjgl.dylib (zink/appkit.m:84-85 checks the basename): %s", stub);
   void *lib = dlopen(stub, RTLD_NOW | RTLD_LOCAL);
   if (!lib) zk_setup_error("dlopen %s: %s", stub, dlerror());
   zw_lwjgl_set_view = (int (*)(void *, void *))dlsym(lib, "zink_lwjgl_set_view");
   zw_lwjgl_update = (int (*)(void *))dlsym(lib, "zink_lwjgl_update");
   zw_lwjgl_clear = (int (*)(void *))dlsym(lib, "zink_lwjgl_clear_drawable");
   if (!zw_lwjgl_set_view || !zw_lwjgl_update || !zw_lwjgl_clear) zk_setup_error("the stub lacks its three functions");
   ZKWin *z = zw_open_or_fail(320, 240, zw_default, true, false);
   zw_thread_win = z;
   atomic_store(&zw_in_stub, 0);
   atomic_store(&zw_bestres_main, 0);
   atomic_store(&zw_bestres_other, 0);
   atomic_store(&zw_bestres_outside, 0);
   zw_run_render_thread(zw_lwjgl_main, 15.0);
   const char *what[3] = {"setView:", "update", "clearDrawable"};
   bool quick = true;
   for (int i = 0; i < 3; ++i) {
      zk_info("%s from the render thread through liblwjgl.dylib returned in %.3f s", what[i], zw_lwjgl_seconds[i]);
      if (zw_lwjgl_seconds[i] > 2.0) quick = false;
   }
   if (quick) zk_pass("calls", "setView:, update and clearDrawable returned within 2 s each");
   else zk_fail("calls", "a call took more than 2 s (see the INFO lines)");
   if (zw_lwjgl_view_ok) zk_pass("set-view", "the context's view is the view after setView: off main");
   else zk_fail("set-view", "the context's view after setView: off main is not the view");
   int on_main = atomic_load(&zw_bestres_main), off_main = atomic_load(&zw_bestres_other);
   zk_info("wantsBestResolutionOpenGLSurface read %d time(s) outside the stub's calls (AppKit's own reads)",
           atomic_load(&zw_bestres_outside));
   if (on_main >= 1 && !off_main)
      zk_pass("main-thread", "during the stub's calls wantsBestResolutionOpenGLSurface was read %d time(s), all on "
              "main", on_main);
   else zk_fail("main-thread", "during the stub's calls wantsBestResolutionOpenGLSurface read on main got=%d off main "
                "got=%d expected=at least 1 and 0 (zink/appkit.m:88-127 runs these calls on main)", on_main, off_main);
   zw_report("frames", &zw_thread_tally, 25);
   zw_pump(0.2);
   int count = 0;
   zw_layer(z.view, &count);
   if (!count) zk_pass("clear-layer", "after clearDrawable off main the view has no CAMetalLayer");
   else zk_fail("clear-layer", "after clearDrawable off main CAMetalLayer sublayers got=%d expected=0", count);
   zw_thread_win = nil;
   zw_close(z);
   zw_end();
}

/* ---- formats (information) --------------------------------------------------------------------------------------- */

/* The adapter mirrors only double buffering and alpha; its default framebuffer is RGBA8 or RGBX8 with Z24S8 and one
 * sample (zink/nvk_zink.c:186-190). Printed next to what the pixel format describes; not judged. */
static void sub_formats(void)
{
   const struct {
      const char *name;
      zw_format f;
   } cases[3] = {
      {"msaa4", {.db = true, .alpha = true, .samples = 4, .depth = 24, .stencil = 8}},
      {"float64", {.db = true, .alpha = true, .color_float = true, .depth = 24, .stencil = 8}},
      {"depth32", {.db = true, .alpha = true, .depth = 32, .stencil = 0}},
   };
   bool gated = false;
   for (int i = 0; i < 3; ++i) {
      zw_format f = cases[i].f;
      f.software = zk_software;
      ZKWin *z = zw_open(160, 120, f, false, true);
      if (!z) {
         zk_info("%s: no window pixel format", cases[i].name);
         continue;
      }
      [z.context makeCurrentContext];
      if (!gated) {
         zk_gate(zk_string(GL_RENDERER));
         gated = true;
      } else if (!zk_same_renderer(cases[i].name)) {
         zw_close(z);
         continue;
      }
      CGLPixelFormatObj pf = CGLGetPixelFormat(z.context.CGLContextObj);
      GLint d_samples = 0, d_color = 0, d_alpha = 0, d_depth = 0, d_stencil = 0, d_float = 0;
      CGLDescribePixelFormat(pf, 0, kCGLPFASamples, &d_samples);
      CGLDescribePixelFormat(pf, 0, kCGLPFAColorSize, &d_color);
      CGLDescribePixelFormat(pf, 0, kCGLPFAAlphaSize, &d_alpha);
      CGLDescribePixelFormat(pf, 0, kCGLPFADepthSize, &d_depth);
      CGLDescribePixelFormat(pf, 0, kCGLPFAStencilSize, &d_stencil);
      CGLDescribePixelFormat(pf, 0, kCGLPFAColorFloat, &d_float);
      zk_info("%s pixel format: samples %d, colour %d, alpha %d, float %d, depth %d, stencil %d", cases[i].name,
              (int)d_samples, (int)d_color, (int)d_alpha, (int)d_float, (int)d_depth, (int)d_stencil);
      glBindFramebuffer(GL_FRAMEBUFFER, 0);
      GLint samples = -1, buffers = -1, type = -1, red = -1, alpha = -1, depth = -1, stencil = -1;
      zk_gl_error();
      glGetIntegerv(GL_SAMPLES, &samples);
      glGetIntegerv(GL_SAMPLE_BUFFERS, &buffers);
      glGetFramebufferAttachmentParameteriv(GL_FRAMEBUFFER, GL_BACK_LEFT, GL_FRAMEBUFFER_ATTACHMENT_COMPONENT_TYPE,
                                            &type);
      glGetFramebufferAttachmentParameteriv(GL_FRAMEBUFFER, GL_BACK_LEFT, GL_FRAMEBUFFER_ATTACHMENT_RED_SIZE, &red);
      glGetFramebufferAttachmentParameteriv(GL_FRAMEBUFFER, GL_BACK_LEFT, GL_FRAMEBUFFER_ATTACHMENT_ALPHA_SIZE, &alpha);
      glGetFramebufferAttachmentParameteriv(GL_FRAMEBUFFER, GL_DEPTH, GL_FRAMEBUFFER_ATTACHMENT_DEPTH_SIZE, &depth);
      glGetFramebufferAttachmentParameteriv(GL_FRAMEBUFFER, GL_STENCIL, GL_FRAMEBUFFER_ATTACHMENT_STENCIL_SIZE,
                                            &stencil);
      GLenum qe = zk_gl_error();
      zk_info("%s default framebuffer: GL_SAMPLES %d, GL_SAMPLE_BUFFERS %d, component type 0x%x, red %d, alpha %d, "
              "depth %d, stencil %d (glGetError 0x%x)", cases[i].name, (int)samples, (int)buffers, (unsigned)type,
              (int)red, (int)alpha, (int)depth, (int)stencil, qe);
      zw_tally t = {0};
      zw_draw(z, i, zw_opaque, &t);
      [z.context flushBuffer];
      GLenum e = zk_gl_error();
      if (!t.readback_bad && !e) zk_pass(cases[i].name, "the window renders and flushes without a GL error");
      else zk_fail(cases[i].name, "%s glGetError 0x%x after the flush", t.readback_bad ? t.first_readback : "", e);
      zw_close(z);
   }
   zw_end();
}

/* ---- main -------------------------------------------------------------------------------------------------------- */

static _Noreturn void zw_usage(void)
{
   zk_setup_error("usage: nvmtltest basic|resize|ctl|noncurrent|front|thread|clear|formats [--renderer software] | "
                  "lwjgl --lwjgl PATH");
}

int main(int argc, char **argv)
{
   zk_tee_start();
   zk_install_handlers(30);
   const char *sub = argc > 1 ? argv[1] : "", *stub = NULL;
   for (int i = 2; i < argc; ++i) {
      if (!strcmp(argv[i], "--renderer") && i + 1 < argc && !strcmp(argv[i + 1], "software")) {
         zk_software = true;
         ++i;
      } else if (!strcmp(argv[i], "--lwjgl") && i + 1 < argc) {
         stub = argv[++i];
      } else {
         zw_usage();
      }
   }
   if (zk_software && strcmp(sub, "formats")) zw_usage();
   @autoreleasepool {
      zw_require_gui();
      zk_require_device();
      [NSApplication sharedApplication];
      [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
      if (!strcmp(sub, "basic")) sub_basic();
      if (!strcmp(sub, "resize")) sub_resize();
      if (!strcmp(sub, "ctl")) sub_ctl();
      if (!strcmp(sub, "noncurrent")) sub_noncurrent();
      if (!strcmp(sub, "front")) sub_front();
      if (!strcmp(sub, "thread")) sub_thread();
      if (!strcmp(sub, "clear")) sub_clear();
      if (!strcmp(sub, "lwjgl")) sub_lwjgl(stub);
      if (!strcmp(sub, "formats")) sub_formats();
   }
   zw_usage();
}
