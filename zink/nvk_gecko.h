/* SPDX-License-Identifier: MIT */
#ifndef NVK_GECKO_H
#define NVK_GECKO_H
#include <stddef.h>
#include <string.h>

/* Whether a process is one of Firefox's, or of another browser built on Gecko (LibreWolf, Zen, Floorp, Waterfox, Tor
 * Browser, Thunderbird), as the CGL adapter needs to know it before it adopts an OpenGL context (zink/nvk_cgl.c).
 *
 * Every process of such a browser, its GPU process included, runs Gecko's library, which macOS builds name XUL
 * (Firefox.app/Contents/MacOS/XUL; plugin-container.app loads the same file). The bundle identifiers differ from one
 * browser to the next and from the main process to its helpers; the library's name does not. A path whose last
 * component is exactly "XUL" counts, wherever it lives.
 *
 * Why it matters (3.0.20): Gecko draws everything it shows into IOSurfaces it binds with CGLTexImageIOSurface2D
 * (WebRender's CoreAnimation tiles and video surfaces, WebGL's canvases). In a Zink context each of those is a private
 * image, copied in from the surface's pages and copied back out, each copy followed by a wait for the GPU
 * (zink/nvk_iosurface.c), at every glFlush, glFinish and glFenceSync. A page that changes every frame, as the preview of
 * one's own screen share in a video call does, then crawls while the share itself and the shared tab keep their rate.
 * Apple's OpenGL over the Metal plugin draws into the surface's own video memory and copies nothing.
 *
 * This file knows no Apple type, so tools/test_zink_gecko.py runs it on any machine. */
static inline int nvk_gecko_image(const char *path)
{
   if (!path) return 0;
   const char *last = strrchr(path, '/');
   return !strcmp(last ? last + 1 : path, "XUL");
}

/* name(i) gives the path of loaded image i, or NULL (an image unloaded meanwhile), for i below count. 1 = a Gecko
 * process. */
static inline int nvk_gecko_process(unsigned count, const char *(*name)(unsigned))
{
   for (unsigned i = 0; i < count; ++i)
      if (nvk_gecko_image(name(i))) return 1;
   return 0;
}
#endif
