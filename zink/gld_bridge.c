/* SPDX-License-Identifier: MIT */
/* NVKZinkGLDriver: the GL driver bundle the accelerator names in IOGLBundleName. Every GLD entry point jumps to
 * Apple's AppleMetalOpenGLRenderer (gld_forward.S, generated from its export names), except context creation and
 * destruction, which also hand the new context to the CGL adapter (libnvk_cgl.dylib) so that its public GL calls
 * reach Zink. The adapter is opened at run time: when it or the Zink runtime is missing, the bundle only forwards,
 * and OpenGL is Apple's. */
#include "nvk_cgl_gld.h"
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define EXPORT __attribute__((visibility("default")))
#include "gld_forward.inc"
/* GLEngine passes gldCreateContext its GLD state, which sits this far into the GLI context. The offset is the
 * recorded observation of the source fork this code is ported from, on macOS 15.8.1 (24H32) and 26.7.1
 * (25G241), x86_64, data of that record. */
#define GLD_STATE_IN_GLI 0x8e0
static int available;
static int (*cgl_prepare)(void *engine, void *driver);
static void (*cgl_destroy)(void *driver);

/* The forwarder list is the export names of the build Mac's renderer. When the
 * installed renderer lacks one of them, GLEngine on that same renderer will not
 * call it either, but its jump stub would otherwise reach a null target. Point
 * such a target here, so a stray call returns a GLD error instead of jumping to
 * zero. The return convention of GLD entries that return a pointer is unknown;
 * this target exists only to keep an unexpected call from crashing. */
static int gld_absent_delegate(void) { return 10000; }

__attribute__((constructor)) static void initialize(void)
{
   void *apple = dlopen("/System/Library/Extensions/AppleMetalOpenGLRenderer.bundle/Contents/MacOS/AppleMetalOpenGLRenderer",
                        RTLD_NOW | RTLD_LOCAL);
   if (!apple) { fprintf(stderr, "NVKZinkGLDriver: %s\n", dlerror()); return; }
   /* The three lifecycle entries the overrides below forward into must resolve;
    * every other delegate may be absent from a renderer that is not the build
    * Mac's. Forward the ones that resolved and stub the ones that did not, so
    * the card keeps Apple's OpenGL through this bundle rather than losing it. */
   int missing_required = 0, missing_absent = 0;
   const char *first_absent = NULL;
   for (size_t i = 0; i < sizeof(gld_names) / sizeof(gld_names[0]); ++i) {
      gld_targets[i] = dlsym(apple, gld_names[i]);
      if (gld_targets[i]) continue;
      if (i == (size_t)GLD_INDEX_gldGetVersion || i == (size_t)GLD_INDEX_gldCreateContext ||
          i == (size_t)GLD_INDEX_gldDestroyContext) {
         fprintf(stderr, "NVKZinkGLDriver: missing required delegate %s; OpenGL is unavailable for this card"
                 " (boot argument nvzink=0 restores Apple's renderer)\n", gld_names[i]);
         missing_required = 1;
      } else {
         gld_targets[i] = (void *)gld_absent_delegate;
         if (!first_absent) first_absent = gld_names[i];
         ++missing_absent;
      }
   }
   if (missing_required) return;
   available = 1;
   if (missing_absent)
      fprintf(stderr, "NVKZinkGLDriver: %d delegate(s) absent from this renderer (first %s);"
              " forwarding the rest to Apple's renderer\n", missing_absent, first_absent);
   /* The adapter beside the Zink runtime (rpath @loader_path/../../../nvmtl/zink). Both functions or neither. */
   void *cgl = dlopen("@rpath/libnvk_cgl.dylib", RTLD_NOW | RTLD_LOCAL);
   if (cgl) {
      cgl_prepare = (int (*)(void *, void *))dlsym(cgl, "nvk_cgl_prepare_gld");
      cgl_destroy = (void (*)(void *))dlsym(cgl, "nvk_cgl_destroy_gld");
   }
   if (!cgl_prepare || !cgl_destroy) {
      cgl_prepare = NULL; cgl_destroy = NULL;
      fprintf(stderr, "NVKZinkGLDriver: no CGL adapter (%s); OpenGL stays Apple's\n", cgl ? "missing entry" : dlerror());
   }
   /* Zink loads its private Vulkan frontend; do not change process ICD selection. */
}

EXPORT int gldGetVersion(uint32_t *a, uint32_t *b, uint32_t *c, uint32_t *d)
{
   if (!available) return 0;
   return ((int (*)(uint32_t *, uint32_t *, uint32_t *, uint32_t *))gld_targets[GLD_INDEX_gldGetVersion])(a,b,c,d);
}

/* gldCreateContext takes six pointer arguments with the GLD context as output in the second and the GLD state in
 * the fifth (the source fork's records). Pixel format, device, share group and plugin state stay Apple's. */
EXPORT int gldCreateContext(void *share, void **output, void *pixel_format,
                           void *config, void *state, void *plugin_state)
{
   if (!available) return 10006;
   int result = ((int (*)(void *, void **, void *, void *, void *, void *))
                 gld_targets[GLD_INDEX_gldCreateContext])(share, output, pixel_format, config, state, plugin_state);
   if (!result && output && *output && state && cgl_prepare)
      cgl_prepare((char *)state - GLD_STATE_IN_GLI, *output);
   return result;
}

EXPORT int gldDestroyContext(void *context)
{
   if (cgl_destroy) cgl_destroy(context);
   return ((int (*)(void *))gld_targets[GLD_INDEX_gldDestroyContext])(context);
}
