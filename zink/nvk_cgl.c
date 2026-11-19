/* SPDX-License-Identifier: MIT */
#include "nvk_cgl.h"
#include "nvk_zink.h"
#include "native_drawable.h"
#define GL_DO_NOT_WARN_IF_MULTI_GL_VERSION_HEADERS_INCLUDED 1
#include <OpenGL/gl3.h>
#include <OpenGL/gl.h>
#include <OpenGL/CGLContext.h>
#include <dlfcn.h>
#include <execinfo.h>
#include <mach/mach.h>
#include <mach/mach_vm.h>
#include <malloc/malloc.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define EXPORT __attribute__((visibility("default")))
/* The structural checks that guard the recorded private offsets below ask
 * Apple's allocator and this task's VM map about pointer values.
 * nvk_cgl_guard.h keeps the same checks portable to the Linux host test, which
 * maps these macros onto a shim of synthetic blocks and regions. */
struct nvk_cgl_vm;
static int nvk_cgl_vm_region(const void *p, struct nvk_cgl_vm *v);
#define NVK_CGL_BLOCK_SIZE(p) malloc_size(p)
#define NVK_CGL_IN_ZONE(p) (malloc_zone_from_ptr(p) != NULL)
#define NVK_CGL_VM_REGION(p, v) nvk_cgl_vm_region((p), (v))
#include "nvk_cgl_guard.h"
/* nvk_cgl_vm_heap names the SDK's malloc tags and protections by number. Tag 13 has no name every SDK shares (the
 * 11.3 SDK calls it VM_MEMORY_MALLOC_PGUARD), so it is compared under whichever name this SDK defines. */
_Static_assert(VM_MEMORY_MALLOC == 1 && VM_MEMORY_MALLOC_LARGE_REUSED == 9 && VM_MEMORY_ANALYSIS_TOOL == 10 &&
               VM_MEMORY_MALLOC_NANO == 11 && VM_MEMORY_MALLOC_MEDIUM == 12 && (VM_PROT_READ | VM_PROT_WRITE) == 3,
               "the SDK's malloc VM tags differ from the ones nvk_cgl_guard.h names");
#ifdef VM_MEMORY_MALLOC_PGUARD
_Static_assert(VM_MEMORY_MALLOC_PGUARD == 13, "the SDK's tag 13 is not the guarded malloc tag nvk_cgl_guard.h names");
#endif
#ifdef VM_MEMORY_MALLOC_PROB_GUARD
_Static_assert(VM_MEMORY_MALLOC_PROB_GUARD == 13,
               "the SDK's tag 13 is not the guarded malloc tag nvk_cgl_guard.h names");
#endif
/* The region of this task's VM map that holds p, at the top level (a submap
 * such as the shared cache is reported as one region). It reads no memory. */
static int nvk_cgl_vm_region(const void *p, struct nvk_cgl_vm *v)
{
   mach_vm_address_t address = (mach_vm_address_t)(uintptr_t)p;
   mach_vm_size_t size = 0;
   natural_t depth = 0;
   vm_region_submap_short_info_data_64_t info;
   mach_msg_type_number_t count = VM_REGION_SUBMAP_SHORT_INFO_COUNT_64;
   if (mach_vm_region_recurse(mach_task_self(), &address, &size, &depth,
                              (vm_region_recurse_info_t)&info, &count) != KERN_SUCCESS ||
       address > (mach_vm_address_t)(uintptr_t)p || address + size <= (mach_vm_address_t)(uintptr_t)p)
      return 0;
   v->tag = info.user_tag;
   v->prot = info.protection;
   v->avail = (size_t)(address + size - (mach_vm_address_t)(uintptr_t)p);
   return 1;
}
/* The CGL context's public layout, from the SDK: struct _CGLContextObject (CGLContext.h) is the engine pointer, the
 * GLIFunctionDispatch table (gliDispatch.h, the table CGLMacro.h calls through) and then the private pointer. The
 * adapter swaps the engine pointer and the table, PREFIX_BYTES from the start; every dispatch slot below is an
 * offsetof() of a member the header names. */
#define PREFIX_BYTES offsetof(struct _CGLContextObject, priv)
#define DISPATCH_SLOT(member) offsetof(struct _CGLContextObject, disp.member)
#define BACK_INDEX(offset) (((offset) - offsetof(struct _CGLContextObject, disp)) / sizeof(void *))
/* The GLEngine offsets below are the private data the adapter's other offsets are relative to. The source fork this
 * code is ported from (the commits' Ported-from lines) recorded them on macOS 15.8.1 (24H32) and 26.7.1 (25G241),
 * x86_64, with this same public prefix (7808 bytes, the glSwapAPPLE slot at 4728), and reports its adapter working on
 * both. They are data of that record, not read from any binary here. A different prefix stops the build, because
 * those observations were made against it. */
_Static_assert(PREFIX_BYTES == 0x1e80 && DISPATCH_SLOT(swap_APPLE) == 0x1278,
               "the SDK's CGL context prefix differs from the one GLEngine's recorded offsets go with");
enum {
   GLI_SHARE_GROUP = 0x5b00,      /* GLI engine: the share group's identity (compared, never followed) */
   GLI_CGL_DISPATCH = 0xb2c8,     /* GLI engine: points at the CGL context's dispatch table (CGL + 8) */
   GLI_SAVED_DISPATCH = 0xb2d0,   /* GLI engine: the saved public dispatch table that gliInitDispatchTable fills */
   GLI_PROFILE = 0xc6a6,          /* GLI engine: 0 legacy, 1 core, other values not adapted */
   PRIV_ENGINES = 0x78,           /* CGL private object: the per-renderer engine records */
   PRIV_ENGINE_COUNT = 0x82,      /* CGL private object: how many records, int16 */
   PRIV_ENGINE_STRIDE = 0x1e90,   /* bytes per record */
   PRIV_ENGINE_CALLBACKS = 0x10,  /* in a record: the GLI integer callback vector */
};
#define MAX_ENTRIES 1024
struct native_cleanup {
   void *share;
   GLuint textures[3];
   GLsync fences[2];
   IOSurfaceRef surfaces[2];
   struct native_cleanup *next;
};
struct nvk_cgl_bridge {
   CGLContextObj context;
   void *engine;
   struct nvk_zink_context *zink;
   unsigned width, height;
   void *original[PREFIX_BYTES / sizeof(void *)];
   nvk_zink_proc procs[MAX_ENTRIES];
   GLuint present_texture, present_fbo;
   struct {
      IOSurfaceRef surface;
      GLuint texture;
      GLsync fence;
   } gpu_present[2];
   unsigned gpu_width, gpu_height, gpu_slot;
   bool present_configured, native_present, apple_uncertain;
   void *driver;
   void **engine_back;
   void *apple_back[sizeof(GLIFunctionDispatch) / sizeof(void *)];
   bool automatic;
   bool legacy;
   bool resize_pending;
   unsigned pending_width, pending_height;
   GLint native_x, native_y;
   struct native_cleanup *cleanup_reserve;
   void **integer_original, **integer_zink;
   const GLubyte **api_extensions;
   char *api_extension_string;
   char api_version_string[128], api_glsl_string[64];
   GLint api_version, api_extension_count;
   bool api_ready;
   struct nvk_cgl_bridge *next;
};
struct dispatch_entry { const char *name; size_t offset; void *adapter; };
static pthread_mutex_t bridges_lock = PTHREAD_MUTEX_INITIALIZER;
static struct nvk_cgl_bridge *bridges;
static struct nvk_cgl_bridge *quarantined_bridges;
static struct native_cleanup *native_cleanups;
static struct native_cleanup *native_quarantine;
static _Atomic unsigned generation;
static _Thread_local unsigned thread_generation;
static _Thread_local struct nvk_cgl_bridge *thread_bridge;
/* The CGL context and engine thread_bridge was looked up for. The fast path of
 * bridge_current compares these instead of reading thread_bridge: a cached
 * bridge of a context that is not current on this thread may be freed by
 * another thread at any time. */
static _Thread_local CGLContextObj thread_context;
static _Thread_local void *thread_engine;
static _Thread_local const char *last_error;
static int materialize(struct nvk_cgl_bridge *b);
static void release_gpu_present(struct nvk_cgl_bridge *b);
static void drain_native_cleanups(struct nvk_cgl_bridge *b);
static int present_drawable(CGLContextObj context);
static void install_integer_dispatch(struct nvk_cgl_bridge *b);
static bool select_integer_dispatch(struct nvk_cgl_bridge *b, bool zink);
static void release_integer_dispatch(struct nvk_cgl_bridge *b);
static int initialize_capabilities(struct nvk_cgl_bridge *b);
#include "cgl_api.inc"
void nvk_cgl_install_lwjgl_appkit(void);
int nvk_cgl_obs_compatibility_enabled(void);

__attribute__((noinline)) static int fail(const char *error) { last_error = error; return 0; }
EXPORT const char *nvk_cgl_last_error(void) { return last_error ? last_error : "none"; }

/* The adapter's lines on standard error, the only place this file prints: what
 * happened and, in parentheses, why. There are three, each printed once for the
 * process, the first time it happens, so that a user's report can say why:
 * OpenGL stays Apple's for a context nvk_cgl_prepare_gld refuses; only
 * CGLTexImageIOSurface2D stays Apple's in a Zink context where
 * install_integer_dispatch cannot place its hook; a call through a Zink
 * context is dropped because it does not come with the adopted GLI engine. */
static void say(const char *what, const char *why) { fprintf(stderr, "NVK Zink: %s (%s)\n", what, why); }
static bool first_time(atomic_flag *said) { return !atomic_flag_test_and_set(said); }
static atomic_flag context_refusal_said = ATOMIC_FLAG_INIT;
static atomic_flag iosurface_refusal_said = ATOMIC_FLAG_INIT;
static atomic_flag dropped_call_said = ATOMIC_FLAG_INIT;
#define LAYOUT_DIFFERS "GLEngine's private layout does not match the record"

static struct nvk_cgl_bridge *find_bridge(CGLContextObj context)
{
   struct nvk_cgl_bridge *b;
   for (b = bridges; b && b->context != context; b = b->next) {}
   return b;
}

/* The CGL context and the GLD context of the last GLI engine that passed the
 * layout check and was not adopted for another reason: the Zink screen is not
 * ready (it could not be made, or it is marked uncertain), or no memory for its
 * bridge. One slot: an engine left unadopted in another context replaces it.
 * bridges_lock guards it; nvk_cgl_destroy_gld clears it with that GLD context. */
static struct { void *context, *driver; } unadopted;

static int leave_unadopted(void *context, void *driver)
{
   pthread_mutex_lock(&bridges_lock);
   unadopted.context = context; unadopted.driver = driver;
   pthread_mutex_unlock(&bridges_lock);
   return 0;
}

void nvk_cgl_lwjgl_drawable_size(CGLContextObj context, unsigned width, unsigned height)
{
   if (!context || !width || !height) return;
   pthread_mutex_lock(&bridges_lock);
   struct nvk_cgl_bridge *b = find_bridge(context);
   if (b && b->automatic &&
       (width != b->width || height != b->height || b->resize_pending)) {
      b->pending_width = width; b->pending_height = height;
      b->native_x = 0; b->native_y = 0;
      b->resize_pending = true;
   }
   pthread_mutex_unlock(&bridges_lock);
}

static void *original_noncurrent(void *engine, size_t offset)
{
   void *proc = NULL;
   CGLContextObj current = CGLGetCurrentContext();
   if (current && *(void **)current == engine) return NULL;
   pthread_mutex_lock(&bridges_lock);
   for (struct nvk_cgl_bridge *b = bridges; b; b = b->next)
      if (b->automatic && b->engine == engine && b->context != current) {
         proc = b->apple_back[BACK_INDEX(offset)]; break;
      }
   pthread_mutex_unlock(&bridges_lock);
   return proc == (void *)1 ? NULL : proc;
}

/* CGL and NSOpenGLContext call the public viewport dispatch during native
 * surface updates, sometimes before the context becomes current. Restrict
 * drawable sizing to those observed callers, preserving application viewports. */
static int native_viewport(void *engine, void *caller, GLint x, GLint y,
                           GLsizei width, GLsizei height)
{
   Dl_info info;
   if (!dladdr(caller, &info) || !info.dli_sname) return 0;
   if (strcmp(info.dli_sname, "CGLSetSurface") &&
       strcmp(info.dli_sname, "-[NSOpenGLContext(NSOpenGLContextInternal) _updateOpenGLViewport]")) return 0;
   pthread_mutex_lock(&bridges_lock);
   struct nvk_cgl_bridge *b;
   for (b = bridges; b && b->engine != engine; b = b->next) {}
   if (b && b->automatic && width > 0 && height > 0) {
      /* AppKit can make the same native context current on main while the
       * application's Mesa context is rendering on another thread. Publish
       * geometry only; reallocating its attachments here races that render. */
      b->pending_width = width; b->pending_height = height;
      b->native_x = x; b->native_y = y;
      b->resize_pending = true;
   }
   void (*apple)(void *, GLint, GLint, GLsizei, GLsizei) =
      b && b->automatic ? b->apple_back[BACK_INDEX(DISPATCH_SLOT(viewport))] : NULL;
   pthread_mutex_unlock(&bridges_lock);
   if (!apple || apple == (void *)1) return 0;
   apple(engine, x, y, width, height);
   return 1;
}

static struct nvk_cgl_bridge *bridge_current(void *engine)
{
   unsigned current_generation = atomic_load(&generation);
   CGLContextObj context = CGLGetCurrentContext();
   if (thread_generation != current_generation || !thread_bridge ||
       thread_context != context || thread_engine != engine) {
      pthread_mutex_lock(&bridges_lock);
      thread_bridge = find_bridge(context);
      thread_context = context; thread_engine = engine;
      thread_generation = atomic_load(&generation);
      pthread_mutex_unlock(&bridges_lock);
   }
   /* Here thread_bridge is the bridge of the context current on this thread;
    * CGL does not allow another thread to destroy a context current here. */
   if (thread_bridge && thread_bridge->engine != engine) {
      /* The call is dropped: the adapter returns 0 or nothing, and neither Zink
       * nor Apple's renderer runs it. */
      if (first_time(&dropped_call_said)) {
         char why[768];
         nvk_cgl_renderer_reason(why, sizeof(why), engine, *(void **)context,
                                 thread_bridge->engine, malloc_size(thread_bridge->engine),
                                 context, malloc_size(context), GLI_PROFILE, GLI_CGL_DISPATCH,
                                 offsetof(struct _CGLContextObject, disp));
         say("a call through a Zink context was dropped; it did not come with the GLI engine Zink adopted for the "
             "current CGL context", why);
      }
      fail("The current CGL context's renderer is not the GLI engine its bridge was prepared for"); return NULL;
   }
   if (!thread_bridge || (!thread_bridge->zink && !materialize(thread_bridge)) ||
       !nvk_zink_make_current(thread_bridge->zink)) {
      fail("No attached Zink context current on this thread"); return NULL;
   }
   if (!initialize_capabilities(thread_bridge)) return NULL;
   unsigned width = thread_bridge->width, height = thread_bridge->height;
   GLint native_x = 0, native_y = 0;
   pthread_mutex_lock(&bridges_lock);
   bool pending = thread_bridge->resize_pending;
   if (pending) {
      width = thread_bridge->pending_width; height = thread_bridge->pending_height;
      native_x = thread_bridge->native_x; native_y = thread_bridge->native_y;
      thread_bridge->resize_pending = false;
   }
   pthread_mutex_unlock(&bridges_lock);
   /* GLFW can resize a Cocoa window through a path that does not pass the
    * CGLSetSurface/NSOpenGLContext viewport callers above.  The native CGL
    * surface still exposes its backing dimensions through this public CGL
    * parameter, so check it at the frame boundary as a fallback. */
   if (thread_bridge->automatic && !pending) {
      GLint surface[2] = {0, 0};
      if (CGLGetParameter(thread_bridge->context, kCGLCPSurfaceBackingSize, surface) == kCGLNoError &&
          surface[0] > 0 && surface[1] > 0 &&
          ((unsigned)surface[0] != thread_bridge->width ||
           (unsigned)surface[1] != thread_bridge->height)) {
         width = (unsigned)surface[0]; height = (unsigned)surface[1];
         pending = true;
      }
   }
   /* NSOpenGLContext can update a surface while another CGL context is current.
    * Mesa requires this drawable current before reallocating its buffers. */
   if (pending) {
      if (!nvk_zink_resize(thread_bridge->zink, width, height)) {
         pthread_mutex_lock(&bridges_lock);
         if (!thread_bridge->resize_pending) {
            thread_bridge->pending_width = width; thread_bridge->pending_height = height;
            thread_bridge->native_x = native_x; thread_bridge->native_y = native_y;
            thread_bridge->resize_pending = true;
         }
         pthread_mutex_unlock(&bridges_lock);
         fail("Cannot resize native Zink drawable"); return NULL;
      }
      pthread_mutex_lock(&bridges_lock);
      thread_bridge->width = width; thread_bridge->height = height;
      pthread_mutex_unlock(&bridges_lock);
      /* Resizing attachments must not overwrite the application viewport. */
   }
   return thread_bridge;
}

static nvk_zink_proc mesa_entrypoint(const char *name)
{
   nvk_zink_proc proc = nvk_zink_get_proc_address(name);
   if (proc) return proc;
   /* Apple exposes its VAO aliases at separate slots. Mesa exposes the same
    * object semantics through the core names in compatibility contexts. */
   static const struct { const char *apple, *mesa; } aliases[] = {
      {"glBindVertexArrayAPPLE", "glBindVertexArray"},
      {"glDeleteVertexArraysAPPLE", "glDeleteVertexArrays"},
      {"glGenVertexArraysAPPLE", "glGenVertexArrays"},
      {"glIsVertexArrayAPPLE", "glIsVertexArray"},
      {"glFlushRenderAPPLE", "glFlush"},
      {"glFinishRenderAPPLE", "glFinish"},
   };
   for (size_t i = 0; i < sizeof(aliases) / sizeof(aliases[0]); ++i)
      if (!strcmp(name, aliases[i].apple)) return nvk_zink_get_proc_address(aliases[i].mesa);
   return NULL;
}

static void missing_entrypoint(const char *name)
{
   fail(name);
   /* Leave unsupported extensions in Mesa's error stream. Calling an Apple
    * handler here would split object and fixed-function state between drivers. */
   void (*enable)(GLenum) = (void *)nvk_zink_get_proc_address("glEnable");
   enable(0);
}

static void flush_front_buffer(struct nvk_cgl_bridge *b)
{
   if (!nvk_zink_begin_front_flush(b->zink)) return;
   int presented = present_drawable(b->context) == 1;
   nvk_zink_end_front_flush(b->zink, presented);
}

static int prepare_iosurface_flush(struct nvk_cgl_bridge *b)
{
   return nvk_zink_flush_iosurfaces(b->zink) || fail("IOSurface GPU publication failed");
}

static void refresh_iosurface_bindings(struct nvk_cgl_bridge *b, GLenum target, int framebuffer)
{
   if (!nvk_zink_refresh_iosurfaces(b->zink,target,framebuffer))
      fail("IOSurface GPU refresh failed");
}

static bool api_has_extension(struct nvk_cgl_bridge *b, const char *name)
{
   const char *const *list=b->legacy?api_compatibility_extensions:api_core_extensions;
   size_t count=b->legacy?sizeof(api_compatibility_extensions)/sizeof(*list):sizeof(api_core_extensions)/sizeof(*list);
   size_t low=0, high=count;
   while (low<high) {
      size_t middle=low+(high-low)/2; int order=strcmp(name,list[middle]);
      if (!order) return true;
      if (order<0) high=middle; else low=middle+1;
   }
   return false;
}

/* The framework's symbol loader cannot expose every API supported by Mesa.
 * Advertise the intersection of real public adapters and Mesa capabilities,
 * independently for each CGL context. The standalone Mesa API stays intact. */
static int initialize_capabilities(struct nvk_cgl_bridge *b)
{
   if (b->api_ready) return 1;
   void (*get)(GLenum,GLint *)=(void *)nvk_zink_get_proc_address("glGetIntegerv");
   const GLubyte *(*string)(GLenum)=(void *)nvk_zink_get_proc_address("glGetString");
   const GLubyte *(*indexed)(GLenum,GLuint)=(void *)nvk_zink_get_proc_address("glGetStringi");
   GLint major=0,minor=0,count=0;get(GL_MAJOR_VERSION,&major);get(GL_MINOR_VERSION,&minor);get(GL_NUM_EXTENSIONS,&count);
   if (count<0 || count>16384 || major<3) return fail("Unsupported Mesa capability table");
   const GLubyte **extensions=calloc((size_t)count+1,sizeof(*extensions));
   if (!extensions) return fail("Cannot allocate CGL capability table");
   size_t bytes=1;GLint kept=0;
   for (GLint i=0;i<count;++i) {
      const GLubyte *name=indexed(GL_EXTENSIONS,(GLuint)i);
      if (name && api_has_extension(b,(const char *)name)) { extensions[kept++]=name;bytes+=strlen((const char *)name)+1; }
   }
   char *all=malloc(bytes);
   if (!all) { free(extensions);return fail("Cannot allocate CGL extension string"); }
   char *next=all;
   for (GLint i=0;i<kept;++i) {
      if (i) *next++=' ';
      size_t length=strlen((const char *)extensions[i]);memcpy(next,extensions[i],length);next+=length;
   }
   *next=0;
   GLint version=major*10+minor,limit=b->legacy?api_compatibility_version:api_core_version;
   b->api_version=version<limit?version:limit;
   const char *mesa=(const char *)string(GL_VERSION),*suffix=mesa?strchr(mesa,' '):NULL;
   snprintf(b->api_version_string,sizeof(b->api_version_string),"%d.%d%s",b->api_version/10,b->api_version%10,suffix?suffix:"");
   unsigned glsl=b->api_version>=33?(unsigned)b->api_version*10: b->api_version==32?150:b->api_version==31?140:130;
   snprintf(b->api_glsl_string,sizeof(b->api_glsl_string),"%u.%02u Mesa CGL",glsl/100,glsl%100);
   b->api_extensions=extensions;b->api_extension_count=kept;b->api_extension_string=all;b->api_ready=true;
   return 1;
}

static const GLubyte *capability_string(struct nvk_cgl_bridge *b, GLenum name)
{
   if (name==GL_VERSION) return (const GLubyte *)b->api_version_string;
   if (name==GL_SHADING_LANGUAGE_VERSION) return (const GLubyte *)b->api_glsl_string;
   if (name==GL_EXTENSIONS && b->legacy) return (const GLubyte *)b->api_extension_string;
   const GLubyte *(*get)(GLenum)=(void *)nvk_zink_get_proc_address("glGetString");return get(name);
}

static const GLubyte *capability_string_indexed(struct nvk_cgl_bridge *b, GLenum name, GLuint index)
{
   const GLubyte *(*get)(GLenum,GLuint)=(void *)nvk_zink_get_proc_address("glGetStringi");
   if (name!=GL_EXTENSIONS) return get(name,index);
   if (index<(GLuint)b->api_extension_count) return b->api_extensions[index];
   /* Ask Mesa for a definitely invalid index, preserving its GL error path. */
   return get(name,UINT32_MAX);
}

static bool capability_parameter(struct nvk_cgl_bridge *b, GLenum name, GLint *value)
{
   switch (name) {
   case GL_MAJOR_VERSION: *value=b->api_version/10;return true;
   case GL_MINOR_VERSION: *value=b->api_version%10;return true;
   case GL_NUM_EXTENSIONS: *value=b->api_extension_count;return true;
   default: return false;
   }
}

#include "cgl_dispatch.inc"
_Static_assert(sizeof(entries) / sizeof(entries[0]) <= MAX_ENTRIES, "adapter inventory too large");

__attribute__((constructor)) static void initialize_appkit_compatibility(void)
{
   nvk_cgl_install_lwjgl_appkit();
}

static bool native_surface_copy_context(void)
{
   /* AppKit's NSIOAccelSurface uses a private legacy context to copy the
    * Apple drawable into an IOSurface for CAImageQueue. Its CGL texture
    * imports and GL objects must remain in the native driver together. */
   void *frames[32];
   int count = backtrace(frames, 32);
   const char prefix[] = "-[NSIOAccelSurface ";
   for (int i = 0; i < count; ++i) {
      Dl_info info;
      if (dladdr(frames[i], &info) && info.dli_sname &&
          !strncmp(info.dli_sname, prefix, sizeof(prefix) - 1)) return true;
   }
   return false;
}

static void use_original(struct nvk_cgl_bridge *b)
{
   select_integer_dispatch(b,false);
   if (b->automatic) {
      b->original[0] = b->engine;
      memcpy(b->original + 1, b->apple_back, sizeof(b->apple_back));
   }
   memcpy(b->context, b->original, PREFIX_BYTES);
}

/* CGLFlushDrawable calls a distinct dispatch slot, not public glFlush. */
static void swap_drawable(void *engine)
{
   CGLContextObj current = CGLGetCurrentContext();
   if (current && *(void **)current == engine) {
      /* Keep ordinary game swaps on the cached current-context path. */
      struct nvk_cgl_bridge *b = bridge_current(engine);
      if (b) nvk_cgl_present(b->context);
      return;
   }
   /* CGLFlushDrawable accepts a context that is not current. For example,
    * AnyDesk releases its render context before flushing the Cocoa window. */
   pthread_mutex_lock(&bridges_lock);
   struct nvk_cgl_bridge *b = bridges;
   while (b && b->engine != engine) b = b->next;
   CGLContextObj context = b ? b->context : NULL;
   pthread_mutex_unlock(&bridges_lock);
   if (!context) { fail("Swap requires an attached Zink context"); return; }
   /* The surrounding CGL flush owns/locks this carrier. Do not acquire a
    * CGL reference while holding bridges_lock (attachment locks CGL first). */
   CGLRetainContext(context);
   nvk_cgl_present(context);
   CGLReleaseContext(context);
}

static void use_zink(struct nvk_cgl_bridge *b)
{
   for (size_t i = 0; i < sizeof(entries) / sizeof(entries[0]); ++i)
      *(void **)((char *)b->context + entries[i].offset) = entries[i].adapter;
   *(void **)((char *)b->context + DISPATCH_SLOT(swap_APPLE)) = (void *)swap_drawable;
   select_integer_dispatch(b,true);
}

/* CGLTexImageIOSurface2D sends an eight-word IOSurface packet through the
 * GLI integer callback. Clone the context's callback vector: the original
 * vector is shared by Apple's engines and must never be edited globally.
 * This finds the place of that vector's pointer in the CGL private object's
 * record of the bridge's engine. Where it finds none it returns NULL and why,
 * when not null, receives what the memory held; it prints nothing. */
static void ***integer_dispatch_slot(struct nvk_cgl_bridge *b, char *why, size_t why_size)
{
   char unused[1];
   if (!why) { why = unused; why_size = sizeof(unused); }
   char *meta=*(char **)((char *)b->context+PREFIX_BYTES);
   /* The CGL private object is memory nvk_cgl_prepare_gld did not check. It is
    * read only as a heap block of its own, or as a pointer-aligned span of the
    * CGL context's block past the public struct, that holds the record's engine
    * count and list pointer; the engine list only as a heap block of its own,
    * or as a pointer-aligned span past those two members of the block that
    * holds the private object, that holds the records it is said to hold. */
   const size_t head=PRIV_ENGINE_COUNT+sizeof(int16_t);
   const void *block; size_t block_size;
   if (nvk_cgl_room(meta, b->context, malloc_size(b->context), sizeof(struct _CGLContextObject),
                    &block, &block_size) < head) {
      snprintf(why, why_size, "the CGL private object is neither a heap block nor a span of the CGL context's block "
               "that holds the recorded engine count and list");
      return NULL;
   }
   char *items=*(char **)(meta+PRIV_ENGINES);
   int count=*(int16_t *)(meta+PRIV_ENGINE_COUNT);
   if (!items || count<1 || count>16) {
      snprintf(why, why_size, "the CGL private object's engine list pointer is %s and its engine count is %d, where "
               "the record has a list of 1 to 16 engines", items ? "not null" : "null", count);
      return NULL;
   }
   const void *list_block; size_t list_block_size;
   if (nvk_cgl_room(items, block, block_size, (size_t)(meta-(const char *)block)+head,
                    &list_block, &list_block_size) < (size_t)count*PRIV_ENGINE_STRIDE) {
      snprintf(why, why_size, "the CGL engine list is neither a heap block nor a span of the CGL private object's "
               "block that holds its %d records", count);
      return NULL;
   }
   for (int i=0;i<count;++i) {
      char *entry=items+(size_t)i*PRIV_ENGINE_STRIDE;
      if (*(void **)entry==b->engine) return (void ***)(entry+PRIV_ENGINE_CALLBACKS);
   }
   snprintf(why, why_size, "none of the %d records of the CGL engine list names this GLI engine", count);
   return NULL;
}

static int iosurface_integer(void *engine, unsigned parameter, const int *values)
{
   pthread_mutex_lock(&bridges_lock);
   struct nvk_cgl_bridge *b=bridges;
   while (b && b->engine!=engine) b=b->next;
   int (*original)(void *,unsigned,const int *)=b && b->integer_original ? b->integer_original[9] : NULL;
   pthread_mutex_unlock(&bridges_lock);
   if (!b || !original) return kCGLBadContext;
   if (parameter!=910) return original(engine,parameter,values);
   if (!values) return kCGLBadAddress;
   CGLContextObj previous=CGLGetCurrentContext();
   struct nvk_zink_context *previous_zink=nvk_zink_get_current();
   bool borrowed=previous!=b->context;
   if (borrowed) {
      if (CGLLockContext(b->context)) return kCGLBadContext;
      if (previous) CGLRetainContext(previous);
      if (CGLSetCurrentContext(b->context)) {
         if (previous) CGLReleaseContext(previous);
         CGLUnlockContext(b->context); return kCGLBadContext;
      }
      /* Keep the Mesa context restored below alive across the borrowed bind. */
      nvk_zink_ref(previous_zink);
   }
   int result=kCGLBadState;
   if (bridge_current(engine)) {
      IOSurfaceRef surface=IOSurfaceLookup((uint32_t)values[0]);
      if (surface) {
         result=nvk_zink_bind_iosurface(b->zink,(unsigned)values[1],(unsigned)values[2],
            values[3],values[4],(unsigned)values[5],(unsigned)values[6],surface,(unsigned)values[7]);
         CFRelease(surface);
      } else result=kCGLBadValue;
   }
   if (borrowed) {
      CGLError restored=CGLSetCurrentContext(previous);
      int mesa_restored=nvk_zink_make_current(previous_zink);
      if (!mesa_restored) nvk_zink_make_current(NULL);
      nvk_zink_unref(previous_zink);
      if (previous) CGLReleaseContext(previous);
      CGLUnlockContext(b->context);
      if (restored || !mesa_restored) result=kCGLBadContext;
   }
   return result;
}

static bool named_callback(void *pointer, const char *name)
{
   Dl_info info={0};
   return pointer && dladdr(pointer,&info) && info.dli_sname && !strcmp(info.dli_sname,name);
}

/* Runs once for a context, when Zink first serves it. The hook is placed only
 * where the engine's record holds a callback vector that is a heap block of 16
 * to 512 pointers whose entries 4, 6, 9 and 15 dladdr names gliCreateContext,
 * gliDestroyContext, gliSetInteger and gliGetVersion, and the clone can be
 * allocated. In every other case the context stays Zink's and
 * CGLTexImageIOSurface2D keeps Apple's handler; the line says what was found,
 * once for the process, except where it was the allocation that failed. */
static void install_integer_dispatch(struct nvk_cgl_bridge *b)
{
   char why[256];
   void ***slot=integer_dispatch_slot(b,why,sizeof(why));
   if (slot) {
      void **vector=*slot;
      size_t bytes=vector && malloc_zone_from_ptr(vector) ? malloc_size(vector) : 0;
      if (!vector)
         snprintf(why,sizeof(why),"the callback vector in this GLI engine's record is null");
      else if (!bytes)
         snprintf(why,sizeof(why),"the callback vector in this GLI engine's record is 0x%jx, which starts no heap "
                  "block",(uintmax_t)(uintptr_t)vector);
      else if (bytes<16*sizeof(void *) || bytes>4096)
         snprintf(why,sizeof(why),"the callback vector in this GLI engine's record is a heap block of 0x%zx bytes, "
                  "where the record has one of 0x80 to 0x1000 bytes",bytes);
      else if (!named_callback(vector[4],"gliCreateContext") ||
               !named_callback(vector[6],"gliDestroyContext") ||
               !named_callback(vector[9],"gliSetInteger") ||
               !named_callback(vector[15],"gliGetVersion"))
         snprintf(why,sizeof(why),"dladdr does not name entries 4, 6, 9 and 15 of the callback vector in this GLI "
                  "engine's record gliCreateContext, gliDestroyContext, gliSetInteger and gliGetVersion");
      else {
         void **copy=malloc(bytes); if (!copy) return;
         memcpy(copy,vector,bytes); copy[9]=(void *)iosurface_integer;
         b->integer_original=vector; b->integer_zink=copy;
         *slot=copy;
         return;
      }
   }
   if (first_time(&iosurface_refusal_said))
      say("CGLTexImageIOSurface2D in a Zink context stays Apple's; " LAYOUT_DIFFERS, why);
}

static bool select_integer_dispatch(struct nvk_cgl_bridge *b, bool zink)
{
   if (!b->integer_zink) return true;
   void ***slot=integer_dispatch_slot(b,NULL,0);
   if (!slot || (*slot!=b->integer_zink && *slot!=b->integer_original)) return false;
   *slot=zink?b->integer_zink:b->integer_original;
   return true;
}

static void release_integer_dispatch(struct nvk_cgl_bridge *b)
{
   /* Keep the clone alive if an unexpected carrier layout prevents proving
    * that Apple's callback vector has been restored. */
   if (select_integer_dispatch(b,false)) free(b->integer_zink);
   b->integer_zink=NULL;
}

/* GLD context creation runs before gliInitDispatchTable. In the source fork's
 * records of 24H32 and 25G241, GLI_CGL_DISPATCH points at the CGL context's
 * table (CGL + 8) and GLI_SAVED_DISPATCH at the saved public dispatch table.
 * Give GLEngine a private table to populate, and leave adapters in the
 * original saved table for CGL's final restore. This is distinct from the HAL
 * table. The GLD proxy owns this registration and calls destroy during
 * teardown. The Zink screen is made only once a context's layout passes the check;
 * a process whose screen cannot be made (no private NVK runtime, no NVIDIA
 * device) keeps Apple's renderer for every context. */
EXPORT int nvk_cgl_prepare_gld(void *engine, void *driver)
{
   if (!engine || !driver) return 0;
   /* OBS keeps all of its GL objects in Apple's driver, together with its
    * CGL/IOSurface interop. Never switch a live context. */
   if (nvk_cgl_obs_compatibility_enabled()) return 0;
   if (native_surface_copy_context()) return 0;
   /* Check that the recorded private offsets land in memory shaped as the record
    * says before any read past a pointer or any write through one, and before the
    * Zink screen (a Vulkan instance and an NVK device) exists. On a build whose
    * layout differs, leave this GLI engine unadapted: return 0, having written
    * nothing and opened no device. */
   unsigned char profile = 0;
   void *context_ptr = NULL;
   void **back = NULL;
   struct nvk_cgl_layout_report report;
   enum nvk_cgl_layout layout =
      nvk_cgl_check_gld_layout(engine, GLI_PROFILE, GLI_CGL_DISPATCH, GLI_SAVED_DISPATCH,
                               offsetof(struct _CGLContextObject, disp),
                               sizeof(struct _CGLContextObject), sizeof(GLIFunctionDispatch),
                               &profile, &context_ptr, &back, &report);
   /* One bridge per CGL context: find_bridge and the restore on destroy key on
    * the context. A further GLI engine (another renderer of the pixel format) of
    * a context the adapter adopted, or of the last one it left unadopted, is left
    * unadapted, whatever its own check says: nothing is written, and no line says
    * that the layout differs. In a context Zink serves, a call that comes with
    * such an engine is dropped (bridge_current). */
   enum nvk_cgl_further further = NVK_CGL_FURTHER_NO;
   if (nvk_cgl_layout_has_context(layout)) {
      pthread_mutex_lock(&bridges_lock);
      struct nvk_cgl_bridge *other = find_bridge((CGLContextObj)(void *)report.context.value);
      further = nvk_cgl_further_engine(layout, other != NULL,
                                       unadopted.context == (void *)report.context.value);
      pthread_mutex_unlock(&bridges_lock);
   }
   if (further == NVK_CGL_FURTHER_ADOPTED) return fail("The CGL context already has a GLI engine served by Zink");
   if (further == NVK_CGL_FURTHER_UNADOPTED) return 0;
   if (layout != NVK_CGL_LAYOUT_OK) {
      if (first_time(&context_refusal_said)) {
         char why[768];
         nvk_cgl_layout_reason(why, sizeof(why), &report);
         say("OpenGL stays Apple's; " LAYOUT_DIFFERS, why);
      }
      return 0;
   }
   if (!nvk_zink_screen_ready()) return leave_unadopted(context_ptr, driver);
   CGLContextObj context = (CGLContextObj)context_ptr;
   struct nvk_cgl_bridge *b = calloc(1, sizeof(*b));
   if (!b) return leave_unadopted(context_ptr, driver);
   b->context = context; b->engine = engine; b->driver = driver;
   b->legacy = profile == 0;
   b->automatic = true; b->engine_back = back;
   memcpy(b->original, context, PREFIX_BYTES);
   /* Every nonzero private slot directs gliInitDispatchTable to write here. */
   for (size_t i = 0; i < sizeof(b->apple_back) / sizeof(b->apple_back[0]); ++i)
      b->apple_back[i] = (void *)1;
   for (size_t i = 0; i < sizeof(entries) / sizeof(entries[0]); ++i) {
      b->procs[i] = mesa_entrypoint(entries[i].name);
   }
   for (size_t i = 0; i < sizeof(entries) / sizeof(entries[0]); ++i) {
      back[BACK_INDEX(entries[i].offset)] = entries[i].adapter;
   }
   back[BACK_INDEX(DISPATCH_SLOT(swap_APPLE))] = (void *)swap_drawable;
   *(void ***)((char *)engine + GLI_SAVED_DISPATCH) = b->apple_back;
   pthread_mutex_lock(&bridges_lock);
   b->next = bridges; bridges = b;
   atomic_fetch_add(&generation, 1);
   pthread_mutex_unlock(&bridges_lock);
   return 1;
}

static int materialize(struct nvk_cgl_bridge *b)
{
   GLint double_buffer = 0, alpha_bits = 0;
   if (!b->automatic || b->context != CGLGetCurrentContext()) return 0;
   CGLDescribePixelFormat(CGLGetPixelFormat(b->context), 0, kCGLPFADoubleBuffer, &double_buffer);
   CGLDescribePixelFormat(CGLGetPixelFormat(b->context), 0, kCGLPFAAlphaSize, &alpha_bits);
   GLint viewport[4] = {0};
   use_original(b);
   glGetIntegerv(GL_VIEWPORT, viewport);
   use_zink(b);
   b->width = viewport[2] > 0 ? (unsigned)viewport[2] : 1;
   b->height = viewport[3] > 0 ? (unsigned)viewport[3] : 1;
   pthread_mutex_lock(&bridges_lock);
   struct nvk_zink_context *share = NULL;
   for (struct nvk_cgl_bridge *other = bridges; other; other = other->next)
      if (other != b && other->zink &&
          *(void **)((char *)other->engine + GLI_SHARE_GROUP) ==
          *(void **)((char *)b->engine + GLI_SHARE_GROUP)) { share = other->zink; break; }
   b->zink = nvk_zink_create_drawable_profile(b->width, b->height, share, double_buffer, b->legacy,
                                            alpha_bits > 0);
   pthread_mutex_unlock(&bridges_lock);
   if (!b->zink) return fail("Cannot create automatic Zink context");
   install_integer_dispatch(b);
   return 1;
}

EXPORT void nvk_cgl_destroy_gld(void *driver)
{
   pthread_mutex_lock(&bridges_lock);
   if (unadopted.driver == driver) unadopted.context = unadopted.driver = NULL;
   struct nvk_cgl_bridge **link = &bridges;
   while (*link && (*link)->driver != driver) link = &(*link)->next;
   struct nvk_cgl_bridge *b = *link;
   if (b) { *link = b->next; atomic_fetch_add(&generation, 1); }
   pthread_mutex_unlock(&bridges_lock);
   if (!b) return;
   nvk_native_remove(b->context);
   use_original(b);
   *(void ***)((char *)b->engine + GLI_SAVED_DISPATCH) = b->engine_back;
   release_gpu_present(b);
   use_original(b);
   if (b->apple_uncertain) {
      pthread_mutex_lock(&bridges_lock);
      b->next = quarantined_bridges; quarantined_bridges = b;
      pthread_mutex_unlock(&bridges_lock);
      return;
   }
   if (b->context == CGLGetCurrentContext()) {
      if (b->present_texture) glDeleteTextures(1, &b->present_texture);
      if (b->present_fbo) glDeleteFramebuffers(1, &b->present_fbo);
   }
   nvk_zink_destroy(b->zink);
   release_integer_dispatch(b);
   free(b->api_extensions);free(b->api_extension_string);
   free(b->cleanup_reserve);
   free(b);
}

EXPORT int nvk_cgl_attach(CGLContextObj context, unsigned width, unsigned height)
{
   last_error = NULL;
   if (!context || context != CGLGetCurrentContext() || !width || !height)
      return fail("Attach requires a current CGL context and nonzero dimensions");
   GLint profile = 0;
   if (CGLDescribePixelFormat(CGLGetPixelFormat(context), 0, kCGLPFAOpenGLProfile, &profile) ||
       (profile != kCGLOGLPVersion_GL4_Core && profile != kCGLOGLPVersion_GL3_Core && profile != kCGLOGLPVersion_Legacy))
      return fail("Only legacy and GL 3.2/4.1 core CGL contexts are supported");
   if (CGLLockContext(context)) return fail("Cannot lock CGL context");
   pthread_mutex_lock(&bridges_lock);
   bool exists = find_bridge(context) != NULL;
   struct nvk_zink_context *share = NULL;
   for (struct nvk_cgl_bridge *other = bridges; other; other = other->next)
      if (other->context != context &&
          *(void **)((char *)other->engine + GLI_SHARE_GROUP) ==
          *(void **)((char *)*(void **)context + GLI_SHARE_GROUP)) share = other->zink;
   pthread_mutex_unlock(&bridges_lock);
   if (exists) { CGLUnlockContext(context); return fail("Context is already attached"); }
   struct nvk_cgl_bridge *b = calloc(1, sizeof(*b));
   if (!b) { CGLUnlockContext(context); return fail("Out of memory"); }
   b->context = context; b->engine = *(void **)context;
   b->legacy = profile == kCGLOGLPVersion_Legacy;
   b->width = width; b->height = height;
   b->resize_pending = false;
   memcpy(b->original, context, PREFIX_BYTES);
   for (size_t i = 0; i < sizeof(entries) / sizeof(entries[0]); ++i) {
      b->procs[i] = mesa_entrypoint(entries[i].name);
   }
   GLint double_buffer = 0, alpha_bits = 0;
   CGLDescribePixelFormat(CGLGetPixelFormat(context), 0, kCGLPFADoubleBuffer, &double_buffer);
   CGLDescribePixelFormat(CGLGetPixelFormat(context), 0, kCGLPFAAlphaSize, &alpha_bits);
   pthread_mutex_lock(&bridges_lock);
   share = NULL;
   for (struct nvk_cgl_bridge *other = bridges; other; other = other->next)
      if (other->zink && *(void **)((char *)other->engine + GLI_SHARE_GROUP) ==
          *(void **)((char *)b->engine + GLI_SHARE_GROUP)) { share = other->zink; break; }
   b->zink = nvk_zink_create_drawable_profile(width, height, share, double_buffer, b->legacy,
                                             alpha_bits > 0);
   pthread_mutex_unlock(&bridges_lock);
   if (!b->zink || !nvk_zink_make_current(b->zink)) {
      nvk_zink_destroy(b->zink); free(b); CGLUnlockContext(context);
      return fail("Cannot create Zink/NVK context");
   }
   CGLRetainContext(context);
   pthread_mutex_lock(&bridges_lock);
   b->next = bridges; bridges = b;
   atomic_fetch_add(&generation, 1);
   pthread_mutex_unlock(&bridges_lock);
   use_zink(b);
   install_integer_dispatch(b);
   CGLUnlockContext(context);
   return 1;
}

EXPORT int nvk_cgl_resize(CGLContextObj context, unsigned width, unsigned height)
{
   if (!context || context != CGLGetCurrentContext()) return fail("Resize requires the context current");
   struct nvk_cgl_bridge *b = bridge_current(*(void **)context);
   if (!b || !nvk_zink_resize(b->zink, width, height)) return fail("Cannot resize Zink drawable");
   pthread_mutex_lock(&bridges_lock);
   b->width = width; b->height = height;
   pthread_mutex_unlock(&bridges_lock);
   return 1;
}

static int reserve_native_cleanup(struct nvk_cgl_bridge *b)
{
   if (!b->cleanup_reserve) b->cleanup_reserve = calloc(1, sizeof(*b->cleanup_reserve));
   return b->cleanup_reserve != NULL;
}

static void free_native_cleanup(struct native_cleanup *cleanup)
{
   for (unsigned i = 0; i < 2; ++i)
      if (cleanup->surfaces[i]) CFRelease(cleanup->surfaces[i]);
   free(cleanup);
}

static void defer_native_cleanup(struct nvk_cgl_bridge *b)
{
   void *share = *(void **)((char *)b->engine + GLI_SHARE_GROUP);
   struct native_cleanup *cleanup = b->cleanup_reserve;
   if (cleanup) {
      cleanup->share = share;
      cleanup->textures[0] = b->present_texture;
      b->present_texture = 0;
      for (unsigned i = 0; i < 2; ++i) {
         cleanup->textures[i + 1] = b->gpu_present[i].texture;
         cleanup->fences[i] = b->gpu_present[i].fence;
         cleanup->surfaces[i] = b->gpu_present[i].surface;
         memset(&b->gpu_present[i], 0, sizeof(b->gpu_present[i]));
      }
      b->cleanup_reserve = NULL;
   }
   pthread_mutex_lock(&bridges_lock);
   bool peer = false;
   for (struct nvk_cgl_bridge *other = bridges; other; other = other->next)
      if (other != b && *(void **)((char *)other->engine + GLI_SHARE_GROUP) == share) {
         peer = true; break;
      }
   if (peer && cleanup) {
      cleanup->next = native_cleanups; native_cleanups = cleanup;
      cleanup = NULL;
   }
   struct native_cleanup *discard = NULL;
   if (!peer) {
      /* Final native share-group destruction owns these remaining GL objects. */
      struct native_cleanup **link = &native_cleanups;
      while (*link) {
         struct native_cleanup *item = *link;
         if (item->share == share) {
            *link = item->next; item->next = discard; discard = item;
         } else link = &item->next;
      }
   }
   pthread_mutex_unlock(&bridges_lock);
   if (cleanup) free_native_cleanup(cleanup);
   while (discard) {
      struct native_cleanup *next = discard->next;
      free_native_cleanup(discard); discard = next;
   }
}

static void drain_native_cleanups(struct nvk_cgl_bridge *b)
{
   if (b->context != CGLGetCurrentContext()) return;
   void *share = *(void **)((char *)b->engine + GLI_SHARE_GROUP);
   struct native_cleanup *pending = NULL;
   pthread_mutex_lock(&bridges_lock);
   struct native_cleanup **link = &native_cleanups;
   while (*link) {
      struct native_cleanup *item = *link;
      if (item->share == share) {
         *link = item->next; item->next = pending; pending = item;
      } else link = &item->next;
   }
   pthread_mutex_unlock(&bridges_lock);
   if (!pending) return;
   use_original(b);
   while (pending) {
      struct native_cleanup *next = pending->next;
      for (unsigned i = 0; i < 2; ++i) {
         if (pending->fences[i]) {
            GLenum result;
            result = glClientWaitSync(pending->fences[i], GL_SYNC_FLUSH_COMMANDS_BIT, 5000000000ULL);
            if (result != GL_ALREADY_SIGNALED && result != GL_CONDITION_SATISFIED) {
               b->apple_uncertain = true;
               pthread_mutex_lock(&bridges_lock);
               struct native_cleanup **tail = &native_quarantine;
               while (*tail) tail = &(*tail)->next;
               *tail = pending;
               pthread_mutex_unlock(&bridges_lock);
               use_zink(b);
               return;
            }
            glDeleteSync(pending->fences[i]);
         }
      }
      glDeleteTextures(3, pending->textures);
      free_native_cleanup(pending); pending = next;
   }
   use_zink(b);
}

static void release_gpu_present(struct nvk_cgl_bridge *b)
{
   if (b->apple_uncertain) return;
   bool current = b->context == CGLGetCurrentContext();
   if (current) {
      drain_native_cleanups(b);
      if (b->apple_uncertain) return;
      use_original(b);
      for (unsigned i = 0; i < 2; ++i) if (b->gpu_present[i].fence) {
         GLenum result = glClientWaitSync(b->gpu_present[i].fence, GL_SYNC_FLUSH_COMMANDS_BIT, 5000000000ULL);
         if (result != GL_ALREADY_SIGNALED && result != GL_CONDITION_SATISFIED) {
            b->apple_uncertain = true;
            use_zink(b);
            return;
         }
      }
      for (unsigned i = 0; i < 2; ++i) {
         if (b->gpu_present[i].fence) glDeleteSync(b->gpu_present[i].fence);
         if (b->gpu_present[i].texture) glDeleteTextures(1, &b->gpu_present[i].texture);
      }
   } else defer_native_cleanup(b);
   if (b->zink) nvk_zink_present_release(b->zink);
   for (unsigned i = 0; i < 2; ++i) {
      if (b->gpu_present[i].surface) CFRelease(b->gpu_present[i].surface);
      memset(&b->gpu_present[i], 0, sizeof(b->gpu_present[i]));
   }
   b->gpu_width = b->gpu_height = b->gpu_slot = 0;
   if (current) use_zink(b);
}

static IOSurfaceRef create_present_surface(unsigned width, unsigned height)
{
   size_t stride = IOSurfaceAlignProperty(kIOSurfaceBytesPerRow, (size_t)width * 4);
   if (stride > SIZE_MAX / height) return NULL;
   size_t size = IOSurfaceAlignProperty(kIOSurfaceAllocSize, stride * height);
   if (size > INT32_MAX) return NULL;
   CFMutableDictionaryRef properties = CFDictionaryCreateMutable(NULL, 0,
      &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
   if (!properties) return NULL;
   const CFStringRef keys[] = {kIOSurfaceWidth, kIOSurfaceHeight, kIOSurfaceBytesPerElement,
                              kIOSurfaceBytesPerRow, kIOSurfaceAllocSize, kIOSurfacePixelFormat};
   int64_t values[] = {width, height, 4, (int64_t)stride, (int64_t)size, 0x42475241};
   for (unsigned i = 0; i < 6; ++i) {
      CFNumberRef n = CFNumberCreate(NULL, kCFNumberSInt64Type, &values[i]);
      if (!n) { CFRelease(properties); return NULL; }
      CFDictionarySetValue(properties, keys[i], n);
      CFRelease(n);
   }
   IOSurfaceRef surface = IOSurfaceCreate(properties);
   CFRelease(properties);
   return surface;
}

static int prepare_gpu_present(struct nvk_cgl_bridge *b)
{
   if (b->gpu_width == b->width && b->gpu_height == b->height) return 1;
   if (!reserve_native_cleanup(b)) return 0;
   release_gpu_present(b);
   if (b->apple_uncertain || !nvk_zink_make_current(b->zink)) return 0;
   for (unsigned i = 0; i < 2; ++i) {
      b->gpu_present[i].surface = create_present_surface(b->width, b->height);
      if (!b->gpu_present[i].surface ||
          !nvk_zink_present_import(b->zink, i, b->gpu_present[i].surface)) goto failure;
      use_original(b);
      glGenTextures(1, &b->gpu_present[i].texture);
      glBindTexture(GL_TEXTURE_RECTANGLE, b->gpu_present[i].texture);
      glTexParameteri(GL_TEXTURE_RECTANGLE, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
      glTexParameteri(GL_TEXTURE_RECTANGLE, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
      CGLError error = CGLTexImageIOSurface2D(b->context, GL_TEXTURE_RECTANGLE, GL_RGBA,
         b->width, b->height, GL_BGRA, GL_UNSIGNED_INT_8_8_8_8_REV, b->gpu_present[i].surface, 0);
      GLenum gl_error = glGetError();
      use_zink(b);
      if (error != kCGLNoError || gl_error != GL_NO_ERROR) goto failure;
   }
   b->gpu_width = b->width;
   b->gpu_height = b->height;
   return 1;
failure:
   release_gpu_present(b);
   return 0;
}

static bool opaque_drawable(CGLContextObj context)
{
   GLint opacity = 1;
   CGLGetParameter(context, kCGLCPSurfaceOpacity, &opacity);
   return opacity != 0;
}

static int gpu_present(struct nvk_cgl_bridge *b)
{
   unsigned slot = b->gpu_slot;
   use_original(b);
   if (b->gpu_present[slot].fence) {
      GLenum result;
      result = glClientWaitSync(b->gpu_present[slot].fence,
                                GL_SYNC_FLUSH_COMMANDS_BIT, 5000000000ULL);
      glDeleteSync(b->gpu_present[slot].fence);
      b->gpu_present[slot].fence = NULL;
      if (result != GL_ALREADY_SIGNALED && result != GL_CONDITION_SATISFIED) { b->apple_uncertain = true; use_zink(b); return fail("Apple IOSurface fence wait failed"); }
   }
   use_zink(b);
   IOSurfaceRef surface = b->gpu_present[slot].surface;
   if (IOSurfaceLock(surface, 0, NULL) != kIOReturnSuccess) return fail("Cannot lock GPU presentation surface");
   int ok = nvk_zink_present_begin(b->zink, slot, 1, opaque_drawable(b->context));
   if (!nvk_zink_present_wait(b->zink, slot)) {
      nvk_zink_present_release(b->zink);
      ok = 0;
   }
   ok &= IOSurfaceUnlock(surface, 0, NULL) == kIOReturnSuccess;
   if (!ok) return fail("Zink IOSurface GPU copy failed");
   use_original(b);
   if (!b->present_fbo) glGenFramebuffers(1, &b->present_fbo);
   glBindFramebuffer(GL_READ_FRAMEBUFFER, b->present_fbo);
   glFramebufferTexture2D(GL_READ_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                          GL_TEXTURE_RECTANGLE, b->gpu_present[slot].texture, 0);
   glReadBuffer(GL_COLOR_ATTACHMENT0);
   glBindFramebuffer(GL_DRAW_FRAMEBUFFER, 0);
   GLint double_buffer = 0;
   glGetIntegerv(GL_DOUBLEBUFFER, &double_buffer);
   glDrawBuffer(double_buffer ? GL_BACK : GL_FRONT);
   glBlitFramebuffer(0, 0, b->width, b->height, 0, 0, b->width, b->height,
                      GL_COLOR_BUFFER_BIT, GL_NEAREST);
   /* Native presentation must select the window framebuffer on both sides,
    * rather than leaving our offscreen IOSurface selected for reads. */
   glBindFramebuffer(GL_READ_FRAMEBUFFER, 0);
   GLenum error = glGetError();
   CGLError swap_error = CGLFlushDrawable(b->context);
   b->gpu_present[slot].fence = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
   glFlush();
   bool fenced = b->gpu_present[slot].fence != NULL;
   use_zink(b);
   if (error != GL_NO_ERROR || swap_error != kCGLNoError || !fenced) {
      b->apple_uncertain = true;
      return fail("Apple IOSurface blit or swap failed; resources quarantined");
   }
   b->gpu_slot ^= 1;
   return nvk_zink_swap(b->zink);
}

/* 2 is a deferred native acquire, rather than a completed publication. */
static int present_drawable(CGLContextObj context)
{
   last_error = NULL;
   if (!context || context != CGLGetCurrentContext()) return fail("Present requires the context current");
   struct nvk_cgl_bridge *b = bridge_current(*(void **)context);
   if (!b) return 0;
   if (!prepare_iosurface_flush(b)) return 0;
   drain_native_cleanups(b);
   if (b->apple_uncertain) return fail("Apple presentation completion uncertain; resources quarantined");
   /* A context with an AppKit view presents through a CAMetalLayer; one
    * without a view, or whose layer cannot be used, through Apple's IOSurface. */
   if (!b->present_configured) {
      b->native_present = true;
      b->present_configured = true;
   }
   if (b->native_present) {
      uint32_t vendor = 0, device = 0;
      if (!nvk_zink_device_info(b->zink, &vendor, &device)) return fail("Native presentation requires an NVK device");
      void *frame = NULL; IOSurfaceRef surface = NULL;
      int acquired = nvk_native_acquire(context, vendor, device, b->width, b->height, opaque_drawable(context), &frame, &surface);
      if (acquired == 2) return 2; /* Main-thread setup/resize or minimized window. */
      if (acquired != 1) {
         if (acquired == NVK_NATIVE_NO_VIEW) {
            GLint has_drawable = 0;
            if (CGLGetParameter(context, kCGLCPHasDrawable, &has_drawable) == kCGLNoError &&
                !has_drawable)
               return 2; /* No output yet: do not blit into an unattached Apple FBO. */
         }
         /* CGL clients without an NSView retain the GPU IOSurface path.
          * Never fall back after an uncertain submission or to CPU pixels. */
         if (acquired != NVK_NATIVE_NO_VIEW) {
            nvk_native_remove(context);
            b->native_present = false;
         }
         /* Early Cocoa swaps may precede setView/update. Keep the context
          * eligible for native presentation once that view is attached. */
      } else {
      int copied = nvk_zink_present_native(b->zink, surface, opaque_drawable(context));
      if (copied < 0) {
         /* The retained frame pins the drawable while uncertain GPU work can
          * still reference its pages. The Zink share group is quarantined. */
         return fail("Native NVK completion uncertain; drawable quarantined");
      }
      int ok = copied && nvk_native_publish(frame);
      nvk_native_release(frame);
      if (!ok) return fail("Native NVK drawable handoff failed");
      return nvk_zink_swap(b->zink);
      }
   }
   if (!prepare_gpu_present(b)) return fail("GPU presentation needs NVK host import and Apple IOSurface textures");
   return gpu_present(b);
}

EXPORT int nvk_cgl_present(CGLContextObj context)
{
   if (!context) return fail("Present requires an attached Zink context");
   CGLContextObj previous = CGLGetCurrentContext();
   if (context == previous) return present_drawable(context) != 0;
   if (CGLLockContext(context)) return fail("Cannot lock presentation context");
   pthread_mutex_lock(&bridges_lock);
   bool attached = find_bridge(context) != NULL;
   pthread_mutex_unlock(&bridges_lock);
   if (!attached) { CGLUnlockContext(context); return fail("Present requires an attached Zink context"); }
   struct nvk_zink_context *previous_zink = nvk_zink_get_current();
   if (previous) CGLRetainContext(previous);
   if (CGLSetCurrentContext(context)) {
      if (previous) CGLReleaseContext(previous);
      CGLUnlockContext(context);
      return fail("Cannot bind presentation context");
   }
   /* Keep the Mesa context restored below alive across the borrowed bind. */
   nvk_zink_ref(previous_zink);
   int presented = present_drawable(context) != 0;
   const char *presentation_error = last_error;
   CGLError restored = CGLSetCurrentContext(previous);
   int mesa_restored = nvk_zink_make_current(previous_zink);
   if (!mesa_restored) nvk_zink_make_current(NULL);
   nvk_zink_unref(previous_zink);
   if (previous) CGLReleaseContext(previous);
   CGLUnlockContext(context);
   if (restored || !mesa_restored) return fail("Cannot restore context after presentation");
   last_error = presentation_error;
   return presented;
}

EXPORT int nvk_cgl_detach(CGLContextObj context)
{
   if (!context || context != CGLGetCurrentContext()) return fail("Detach requires the context current");
   if (CGLLockContext(context)) return fail("Cannot lock CGL context");
   pthread_mutex_lock(&bridges_lock);
   struct nvk_cgl_bridge **link = &bridges;
   while (*link && (*link)->context != context) link = &(*link)->next;
   struct nvk_cgl_bridge *b = *link;
   if (b) { *link = b->next; atomic_fetch_add(&generation, 1); }
   pthread_mutex_unlock(&bridges_lock);
   if (!b) { CGLUnlockContext(context); return fail("Context is not attached"); }
   nvk_native_remove(b->context);
   release_gpu_present(b);
   use_original(b);
   bool automatic = b->automatic;
   if (automatic) *(void ***)((char *)b->engine + GLI_SAVED_DISPATCH) = b->engine_back;
   if (b->apple_uncertain) {
      pthread_mutex_lock(&bridges_lock);
      b->next = quarantined_bridges; quarantined_bridges = b;
      pthread_mutex_unlock(&bridges_lock);
      CGLUnlockContext(context);
      if (!automatic) CGLReleaseContext(context);
      return 1;
   }
   if (b->present_texture) glDeleteTextures(1, &b->present_texture);
   if (b->present_fbo) glDeleteFramebuffers(1, &b->present_fbo);
   nvk_zink_destroy(b->zink);
   release_integer_dispatch(b);
   free(b->api_extensions);free(b->api_extension_string);
   free(b->cleanup_reserve);
   free(b);
   CGLUnlockContext(context);
   if (!automatic) CGLReleaseContext(context);
   return 1;
}
