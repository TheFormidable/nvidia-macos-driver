/* zink_gl_check: offscreen OpenGL contexts on the installed native OpenGL driver (NVKZinkGLDriver.bundle, which
 * forwards to Apple's renderer and hands each hardware context to the Zink adapter in /Library/GPUBundles/nvmtl/zink).
 * tools/card/checks_zink.py builds it as {BIN}/<id>/nvmtltest and runs one subcommand per process:
 *
 *   path                   which renderer serves a hardware context, its strings, and the images that served it
 *   rinfo                  renderer information through the forwarding bundle
 *   profile P B A          P legacy|gl3|gl4, B db|sb, A alpha|noalpha: the strings, the default framebuffer, an FBO
 *                          clear and triangle, and the extensions against the installed cgl-dispatch-map.json
 *   offscreen              depth-tested FBO rendering, readback, PBO, fence, texture upload, CGLFlushDrawable
 *                          without a drawable
 *   legacy                 fixed function, a display list and GLSL 1.20 in a legacy context
 *   iosurface              CGLTexImageIOSurface2D: read, write-back, refresh on rebind, formats, errors
 *   share a|b|c|d          shared and unshared contexts, and the order in which they first make a GL call
 *   threads a|b            one context per thread; a fence handoff between shared contexts on two threads
 *   destroy a|b|c          destroying a context that another thread released, the current one, or one whose
 *                          thread exited with it current
 *   migrate                a context that moves to another thread while its old thread binds another context
 *
 * Options: --renderer software asks for Apple's software renderer (kCGLRendererGenericFloatID), which never reaches
 * the bundle (zink/gld_bridge.c only sees the hardware renderer's gldCreateContext): the same-process control.
 * --map PATH names the installed cgl-dispatch-map.json, whose "capabilities" give the version caps and extension
 * lists the adapter advertises (build/generate_cgl_dispatch.py:236-243).
 *
 * A hardware context that the adapter's layout guard keeps Apple's makes the check inactive (exit 4), with the
 * guard's reason; any other hardware context that stays Apple's is a failure. See zk_not_zink in zink_common.h.
 * Nothing here draws on screen or writes a file. It has run on one Mac, on macOS 26.7.3 (25G342), where the adapter's
 * layout guard kept every hardware context Apple's: only rinfo and the software-renderer controls gave results. */
#include "zink_common.h"
#include <IOSurface/IOSurface.h>
#include <OpenGL/CGLIOSurface.h>

/* ---- arguments and the installed capability table ---------------------------------------------------------------- */

static const char *zk_map_path;
static int zk_core_cap = 41, zk_compat_cap = 32; /* the 11.3 SDK's caps, used until --map is read */
static NSSet<NSString *> *zk_core_ext, *zk_compat_ext;

static void zk_read_map(void)
{
   if (!zk_map_path) return;
   @autoreleasepool {
      NSData *data = [NSData dataWithContentsOfFile:[NSString stringWithUTF8String:zk_map_path]];
      NSError *error = nil;
      id root = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:&error] : nil;
      NSDictionary *caps = [root isKindOfClass:[NSDictionary class]] ? root[@"capabilities"] : nil;
      NSDictionary *core = [caps isKindOfClass:[NSDictionary class]] ? caps[@"core"] : nil;
      NSDictionary *compat = [caps isKindOfClass:[NSDictionary class]] ? caps[@"compatibility"] : nil;
      if (![core isKindOfClass:[NSDictionary class]] || ![compat isKindOfClass:[NSDictionary class]] ||
          ![core[@"version"] isKindOfClass:[NSNumber class]] || ![compat[@"version"] isKindOfClass:[NSNumber class]] ||
          ![core[@"extensions"] isKindOfClass:[NSArray class]] ||
          ![compat[@"extensions"] isKindOfClass:[NSArray class]])
         zk_setup_error("%s has no capabilities.core and capabilities.compatibility with version and extensions",
                        zk_map_path);
      zk_core_cap = [core[@"version"] intValue];
      zk_compat_cap = [compat[@"version"] intValue];
      zk_core_ext = [NSSet setWithArray:core[@"extensions"]];
      zk_compat_ext = [NSSet setWithArray:compat[@"extensions"]];
      zk_info("cgl-dispatch-map.json: core %d with %lu extensions, compatibility %d with %lu extensions", zk_core_cap,
              (unsigned long)zk_core_ext.count, zk_compat_cap, (unsigned long)zk_compat_ext.count);
   }
}

/* ---- pixel formats and contexts ---------------------------------------------------------------------------------- */

enum { ZK_LEGACY, ZK_GL3, ZK_GL4 };
static const char *const zk_profile_names[] = {"legacy", "gl3", "gl4"};

/* Hardware: accelerated, no recovery, the main display's mask, which leaves CGL no renderer but the card's.
 * Software: Apple's software renderer; when it has no 4.1 core profile, 3.2 core. */
static CGLPixelFormatObj zk_pixel_format(int profile, bool double_buffer, bool alpha)
{
   for (int attempt = 0; attempt < 2; ++attempt) {
      int p = profile;
      if (attempt) {
         if (!zk_software || profile != ZK_GL4) break;
         p = ZK_GL3;
      }
      CGLPixelFormatAttribute a[24];
      int n = 0;
      a[n++] = kCGLPFAOpenGLProfile;
      a[n++] = (CGLPixelFormatAttribute)(p == ZK_LEGACY ? kCGLOGLPVersion_Legacy
                                         : p == ZK_GL3  ? kCGLOGLPVersion_GL3_Core
                                                        : kCGLOGLPVersion_GL4_Core);
      if (zk_software) {
         a[n++] = kCGLPFARendererID;
         a[n++] = (CGLPixelFormatAttribute)kCGLRendererGenericFloatID;
      } else {
         a[n++] = kCGLPFAAccelerated;
         a[n++] = kCGLPFANoRecovery;
         a[n++] = kCGLPFADisplayMask;
         a[n++] = (CGLPixelFormatAttribute)CGDisplayIDToOpenGLDisplayMask(CGMainDisplayID());
      }
      a[n++] = kCGLPFAColorSize;
      a[n++] = (CGLPixelFormatAttribute)24;
      if (alpha) {
         a[n++] = kCGLPFAAlphaSize;
         a[n++] = (CGLPixelFormatAttribute)8;
      }
      a[n++] = kCGLPFADepthSize;
      a[n++] = (CGLPixelFormatAttribute)24;
      a[n++] = kCGLPFAStencilSize;
      a[n++] = (CGLPixelFormatAttribute)8;
      if (double_buffer) a[n++] = kCGLPFADoubleBuffer;
      a[n] = (CGLPixelFormatAttribute)0;
      CGLPixelFormatObj pf = NULL;
      GLint count = 0;
      CGLError e = CGLChoosePixelFormat(a, &pf, &count);
      if (!e && pf) {
         if (attempt) zk_info("the software renderer has no 4.1 core profile here; the control uses 3.2 core");
         return pf;
      }
      zk_info("CGLChoosePixelFormat(%s) gave %d (%s)", zk_profile_names[p], (int)e, CGLErrorString(e));
   }
   zk_setup_error("no %s pixel format for %s", zk_software ? "software" : "hardware", zk_profile_names[profile]);
}

static CGLContextObj zk_context(CGLPixelFormatObj pf, CGLContextObj share)
{
   CGLContextObj ctx = NULL;
   CGLError e = CGLCreateContext(pf, share, &ctx);
   if (e || !ctx) zk_setup_error("CGLCreateContext gave %d (%s)", (int)e, CGLErrorString(e));
   return ctx;
}

static void zk_make_current(CGLContextObj ctx)
{
   CGLError e = CGLSetCurrentContext(ctx);
   if (e) zk_setup_error("CGLSetCurrentContext gave %d (%s)", (int)e, CGLErrorString(e));
}

/* ---- GL objects of one context ----------------------------------------------------------------------------------- */

typedef struct {
   int glsl; /* 120 for legacy contexts, else 150 or 410 */
   GLuint vao, vbo;
   GLuint solid, fetch;
   GLint solid_color, solid_z, fetch_tex;
   GLuint fbo, color, depth;
   int w, h;
} zk_gl;

static GLuint zk_shader(GLenum type, const char *source, const char *name)
{
   GLuint s = glCreateShader(type);
   glShaderSource(s, 1, &source, NULL);
   glCompileShader(s);
   GLint ok = 0;
   glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
   if (!ok) {
      char log[512] = {0};
      glGetShaderInfoLog(s, sizeof(log) - 1, NULL, log);
      zk_fail(name, "the %s shader did not compile: %s", type == GL_VERTEX_SHADER ? "vertex" : "fragment", log);
      glDeleteShader(s);
      return 0;
   }
   return s;
}

static GLuint zk_program(int glsl, const char *vs_body, const char *fs_body, const char *name)
{
   char vs[1024], fs[1024];
   snprintf(vs, sizeof(vs), "#version %d\n%s", glsl, vs_body);
   snprintf(fs, sizeof(fs), "#version %d\n%s", glsl, fs_body);
   GLuint v = zk_shader(GL_VERTEX_SHADER, vs, name), f = v ? zk_shader(GL_FRAGMENT_SHADER, fs, name) : 0;
   if (!v || !f) return 0;
   GLuint p = glCreateProgram();
   glAttachShader(p, v);
   glAttachShader(p, f);
   glBindAttribLocation(p, 0, "pos");
   if (glsl >= 150) glBindFragDataLocation(p, 0, "frag");
   glLinkProgram(p);
   glDeleteShader(v);
   glDeleteShader(f);
   GLint ok = 0;
   glGetProgramiv(p, GL_LINK_STATUS, &ok);
   if (!ok) {
      char log[512] = {0};
      glGetProgramInfoLog(p, sizeof(log) - 1, NULL, log);
      zk_fail(name, "the program did not link: %s", log);
      glDeleteProgram(p);
      return 0;
   }
   return p;
}

/* A solid-colour program with a depth uniform, and (core only) a program that copies texel (x, y) of a 2D texture to
 * pixel (x, y). Needs the context current. */
static bool zk_gl_setup(zk_gl *g, int glsl, const char *name)
{
   memset(g, 0, sizeof(*g));
   g->glsl = glsl;
   if (glsl >= 150) {
      g->solid = zk_program(glsl, "in vec2 pos;\nuniform float z;\nvoid main() { gl_Position = vec4(pos, z, 1.0); }\n",
                            "uniform vec4 color;\nout vec4 frag;\nvoid main() { frag = color; }\n", name);
      g->fetch = zk_program(glsl, "in vec2 pos;\nvoid main() { gl_Position = vec4(pos, 0.0, 1.0); }\n",
                            "uniform sampler2D tex;\nout vec4 frag;\n"
                            "void main() { frag = texelFetch(tex, ivec2(gl_FragCoord.xy), 0); }\n",
                            name);
      if (!g->solid || !g->fetch) return false;
      glGenVertexArrays(1, &g->vao);
      glBindVertexArray(g->vao);
   } else {
      g->solid = zk_program(glsl, "attribute vec2 pos;\nuniform float z;\n"
                                  "void main() { gl_Position = vec4(pos, z, 1.0); }\n",
                            "uniform vec4 color;\nvoid main() { gl_FragColor = color; }\n", name);
      if (!g->solid) return false;
   }
   g->solid_color = glGetUniformLocation(g->solid, "color");
   g->solid_z = glGetUniformLocation(g->solid, "z");
   if (g->fetch) g->fetch_tex = glGetUniformLocation(g->fetch, "tex");
   glGenBuffers(1, &g->vbo);
   glBindBuffer(GL_ARRAY_BUFFER, g->vbo);
   glEnableVertexAttribArray(0);
   glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, (const void *)0);
   GLenum e = zk_gl_error();
   if (e) {
      zk_fail(name, "glGetError 0x%x after building the programs and buffers", e);
      return false;
   }
   return true;
}

/* An FBO with RGBA8 colour and D24S8 depth-stencil renderbuffers, bound for drawing and reading, with its viewport. */
static bool zk_gl_fbo(zk_gl *g, int w, int h, const char *name)
{
   g->w = w;
   g->h = h;
   glGenFramebuffers(1, &g->fbo);
   glGenRenderbuffers(1, &g->color);
   glGenRenderbuffers(1, &g->depth);
   glBindRenderbuffer(GL_RENDERBUFFER, g->color);
   glRenderbufferStorage(GL_RENDERBUFFER, GL_RGBA8, w, h);
   glBindRenderbuffer(GL_RENDERBUFFER, g->depth);
   glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH24_STENCIL8, w, h);
   glBindFramebuffer(GL_FRAMEBUFFER, g->fbo);
   glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_RENDERBUFFER, g->color);
   glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_ATTACHMENT, GL_RENDERBUFFER, g->depth);
   glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_STENCIL_ATTACHMENT, GL_RENDERBUFFER, g->depth);
   GLenum status = glCheckFramebufferStatus(GL_FRAMEBUFFER);
   glViewport(0, 0, w, h);
   GLenum e = zk_gl_error();
   if (status != GL_FRAMEBUFFER_COMPLETE || e) {
      zk_fail(name, "a %dx%d RGBA8 + D24S8 framebuffer: status 0x%x, glGetError 0x%x", w, h, status, e);
      return false;
   }
   return true;
}

static void zk_gl_free(zk_gl *g)
{
   if (g->fbo) glDeleteFramebuffers(1, &g->fbo);
   if (g->color) glDeleteRenderbuffers(1, &g->color);
   if (g->depth) glDeleteRenderbuffers(1, &g->depth);
   if (g->vbo) glDeleteBuffers(1, &g->vbo);
   if (g->vao) glDeleteVertexArrays(1, &g->vao);
   if (g->solid) glDeleteProgram(g->solid);
   if (g->fetch) glDeleteProgram(g->fetch);
   memset(g, 0, sizeof(*g));
}

static void zk_gl_draw(zk_gl *g, GLuint program, const float *xy, int vertices)
{
   if (g->vao) glBindVertexArray(g->vao);
   glBindBuffer(GL_ARRAY_BUFFER, g->vbo);
   glBufferData(GL_ARRAY_BUFFER, (GLsizeiptr)(sizeof(float) * 2 * (size_t)vertices), xy, GL_STREAM_DRAW);
   glEnableVertexAttribArray(0);
   glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, (const void *)0);
   glUseProgram(program);
   glDrawArrays(GL_TRIANGLES, 0, vertices);
}

static void zk_gl_solid(zk_gl *g, float z, const unsigned char *rgba)
{
   glUseProgram(g->solid);
   glUniform4f(g->solid_color, rgba[0] / 255.0f, rgba[1] / 255.0f, rgba[2] / 255.0f, rgba[3] / 255.0f);
   glUniform1f(g->solid_z, z);
}

/* A rectangle in pixels [x0, x1) x [y0, y1) of the bound framebuffer. Its edges lie on pixel boundaries, so no pixel
 * centre is on an edge and coverage is exact on every rasterizer. */
static void zk_gl_rect(zk_gl *g, int x0, int y0, int x1, int y1, float z, const unsigned char *rgba)
{
   float a = 2.0f * x0 / g->w - 1.0f, b = 2.0f * y0 / g->h - 1.0f;
   float c = 2.0f * x1 / g->w - 1.0f, d = 2.0f * y1 / g->h - 1.0f;
   const float xy[12] = {a, b, c, b, c, d, a, b, c, d, a, d};
   zk_gl_solid(g, z, rgba);
   zk_gl_draw(g, g->solid, xy, 6);
}

/* The triangle (-1,-1), (1,-1), (-1,1): the lower-left half below the diagonal. */
static void zk_gl_lower_left(zk_gl *g, const unsigned char *rgba)
{
   const float xy[6] = {-1.0f, -1.0f, 1.0f, -1.0f, -1.0f, 1.0f};
   zk_gl_solid(g, 0.0f, rgba);
   zk_gl_draw(g, g->solid, xy, 3);
}

/* Copies texel (x, y) of the 2D texture to pixel (x, y) of the bound framebuffer. */
static void zk_gl_fetch(zk_gl *g, GLuint texture)
{
   const float xy[12] = {-1, -1, 1, -1, 1, 1, -1, -1, 1, 1, -1, 1};
   glUseProgram(g->fetch);
   glActiveTexture(GL_TEXTURE0);
   glBindTexture(GL_TEXTURE_2D, texture);
   glUniform1i(g->fetch_tex, 0);
   zk_gl_draw(g, g->fetch, xy, 6);
}

static void zk_clear(const unsigned char *rgba)
{
   glClearColor(rgba[0] / 255.0f, rgba[1] / 255.0f, rgba[2] / 255.0f, rgba[3] / 255.0f);
   glClearDepth(1.0);
   glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT | GL_STENCIL_BUFFER_BIT);
}

static void zk_read(int x, int y, unsigned char *rgba)
{
   memset(rgba, 0xee, 4);
   glReadPixels(x, y, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, rgba);
}

/* One pixel against a colour; on a mismatch the text of a FAIL line is written to why. */
static bool zk_pixel_ok(int x, int y, const unsigned char *want, int tolerance, char *why, size_t size)
{
   unsigned char got[4];
   zk_read(x, y, got);
   if (zk_near(got, want, tolerance)) return true;
   snprintf(why, size, "pixel (%d,%d) got=(%u,%u,%u,%u) expected=(%u,%u,%u,%u)%s", x, y, got[0], got[1], got[2], got[3],
            want[0], want[1], want[2], want[3], tolerance ? " within 1" : "");
   return false;
}

static bool zk_no_error(const char *name, const char *after)
{
   GLenum e = zk_gl_error();
   if (!e) return true;
   zk_fail(name, "glGetError got=0x%x expected=0 after %s", e, after);
   return false;
}

/* A texture with a known RGBA8 pattern. */
static void zk_pattern(unsigned char *rgba, int w, int h, unsigned seed)
{
   for (int y = 0; y < h; ++y)
      for (int x = 0; x < w; ++x) {
         unsigned char *p = rgba + 4 * (y * w + x);
         p[0] = (unsigned char)(seed * 37u + (unsigned)x * 50u + 7u);
         p[1] = (unsigned char)(seed * 11u + (unsigned)y * 60u + 3u);
         p[2] = (unsigned char)(200u - seed * 5u - (unsigned)(x + y) * 9u);
         p[3] = 255;
      }
}

static GLuint zk_texture(int w, int h, unsigned seed)
{
   unsigned char pixels[16 * 16 * 4];
   zk_pattern(pixels, w, h, seed);
   GLuint t = 0;
   glGenTextures(1, &t);
   glBindTexture(GL_TEXTURE_2D, t);
   glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
   glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
   glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
   return t;
}

/* Reads texture t through an FBO of the current context; 0 when every texel equals pattern seed. */
static int zk_texture_differs(GLuint t, int w, int h, unsigned seed, char *why, size_t size)
{
   unsigned char want[16 * 16 * 4], got[16 * 16 * 4];
   zk_pattern(want, w, h, seed);
   memset(got, 0xee, sizeof(got));
   GLuint fbo = 0;
   glGenFramebuffers(1, &fbo);
   glBindFramebuffer(GL_FRAMEBUFFER, fbo);
   glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, t, 0);
   GLenum status = glCheckFramebufferStatus(GL_FRAMEBUFFER);
   glPixelStorei(GL_PACK_ALIGNMENT, 1);
   if (status == GL_FRAMEBUFFER_COMPLETE) glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, got);
   glBindFramebuffer(GL_FRAMEBUFFER, 0);
   glDeleteFramebuffers(1, &fbo);
   if (status != GL_FRAMEBUFFER_COMPLETE) {
      snprintf(why, size, "the texture cannot be attached (status 0x%x)", status);
      return -1;
   }
   for (int i = 0; i < w * h; ++i)
      if (memcmp(got + 4 * i, want + 4 * i, 4)) {
         snprintf(why, size, "texel (%d,%d) got=(%u,%u,%u,%u) expected=(%u,%u,%u,%u)", i % w, i / w, got[4 * i],
                  got[4 * i + 1], got[4 * i + 2], got[4 * i + 3], want[4 * i], want[4 * i + 1], want[4 * i + 2],
                  want[4 * i + 3]);
         return 1;
      }
   return 0;
}

static void zk_buffer_pattern(unsigned char *bytes, size_t n, unsigned seed)
{
   for (size_t i = 0; i < n; ++i) bytes[i] = (unsigned char)(seed * 29u + i * 13u + 1u);
}

/* ---- bounded handoffs between threads ---------------------------------------------------------------------------- */

typedef struct {
   pthread_mutex_t m;
   pthread_cond_t c;
   int value;
} zk_event;
#define ZK_EVENT_INIT {PTHREAD_MUTEX_INITIALIZER, PTHREAD_COND_INITIALIZER, 0}

static void zk_event_set(zk_event *e, int value)
{
   pthread_mutex_lock(&e->m);
   e->value = value;
   pthread_cond_broadcast(&e->c);
   pthread_mutex_unlock(&e->m);
}

static bool zk_event_wait(zk_event *e, int value, double seconds)
{
   struct timespec until;
   clock_gettime(CLOCK_REALTIME, &until);
   long long ns = (long long)until.tv_nsec + (long long)(seconds * 1e9);
   until.tv_sec += (time_t)(ns / 1000000000LL);
   until.tv_nsec = (long)(ns % 1000000000LL);
   pthread_mutex_lock(&e->m);
   while (e->value < value)
      if (pthread_cond_timedwait(&e->c, &e->m, &until) == ETIMEDOUT) break;
   bool ok = e->value >= value;
   pthread_mutex_unlock(&e->m);
   return ok;
}

/* The first context of a threaded check is made and judged on the main thread, so that an inactive or failed gate
 * ends the process from main; it stays current there, with no further GL call, until the process ends. */
static void zk_gate_on_main(CGLPixelFormatObj pf)
{
   CGLContextObj gate = zk_context(pf, NULL);
   zk_make_current(gate);
   zk_gate(zk_string(GL_RENDERER));
   glFinish();
}

/* ---- the strings and images of a context ------------------------------------------------------------------------ */

/* The adapter's strings (zink/nvk_cgl.c:392-450): GL_VERSION is "<v/10>.<v%10>" plus Mesa's text after its first
 * space, with v = min(Mesa's version, the profile's cap) (:415-418); GL_SHADING_LANGUAGE_VERSION is "%u.%02u Mesa CGL"
 * of v*10 for v >= 33, else 150, 140 or 130 (:419-420); GL_MAJOR_VERSION and GL_MINOR_VERSION come from v (:442-450).
 * GL_VENDOR and GL_RENDERER are Mesa's: "Mesa" and "zink Vulkan <maj>.<min>(<device> (MESA_NVK))" (Mesa 26.3.0
 * src/gallium/drivers/zink/zink_screen.c:162-165 and :186-197). Mesa's own text says "(Core Profile)" for core and
 * "(Compatibility Profile)" for a compatibility context of 3.2 or more (src/mesa/main/version.c:129-143). */
static void zk_check_strings(bool legacy)
{
   const char *vendor = zk_string(GL_VENDOR), *renderer = zk_string(GL_RENDERER);
   const char *version = zk_string(GL_VERSION), *glsl = zk_string(GL_SHADING_LANGUAGE_VERSION);
   zk_info("GL_VENDOR %s", vendor);
   zk_info("GL_VERSION %s", version);
   zk_info("GL_SHADING_LANGUAGE_VERSION %s", glsl);
   if (zk_software) return;
   if (!strcmp(vendor, "Mesa")) zk_pass("vendor", "GL_VENDOR is Mesa");
   else zk_fail("vendor", "GL_VENDOR got=%s expected=Mesa", vendor);
   if (strstr(renderer, "(MESA_NVK)")) zk_pass("renderer", "GL_RENDERER names the NVK device: %s", renderer);
   else zk_fail("renderer", "GL_RENDERER got=%s expected=zink Vulkan ...(MESA_NVK))", renderer);
   int cap = legacy ? zk_compat_cap : zk_core_cap;
   char want[64];
   snprintf(want, sizeof(want), "%d.%d ", cap / 10, cap % 10);
   const char *profile = legacy ? (cap >= 32 ? "(Compatibility Profile) Mesa " : " Mesa ") : "(Core Profile) Mesa ";
   if (!strncmp(version, want, strlen(want)) && strstr(version, profile))
      zk_pass("version", "GL_VERSION %s (the %s cap %d)", version, legacy ? "compatibility" : "core", cap);
   else
      zk_fail("version", "GL_VERSION got=%s expected=%s...%s... (the %s cap %d of cgl-dispatch-map.json; a lower "
              "number means Mesa's own version is below it)", version, want, profile,
              legacy ? "compatibility" : "core", cap);
   unsigned g = cap >= 33 ? (unsigned)cap * 10 : cap == 32 ? 150 : cap == 31 ? 140 : 130;
   char want_glsl[32];
   snprintf(want_glsl, sizeof(want_glsl), "%u.%02u Mesa CGL", g / 100, g % 100);
   if (!strcmp(glsl, want_glsl)) zk_pass("glsl", "GL_SHADING_LANGUAGE_VERSION %s", glsl);
   else zk_fail("glsl", "GL_SHADING_LANGUAGE_VERSION got=%s expected=%s", glsl, want_glsl);
   GLint major = -1, minor = -1;
   zk_gl_error();
   glGetIntegerv(GL_MAJOR_VERSION, &major);
   glGetIntegerv(GL_MINOR_VERSION, &minor);
   GLenum e = zk_gl_error();
   if (!e && major == cap / 10 && minor == cap % 10)
      zk_pass("major-minor", "GL_MAJOR_VERSION.GL_MINOR_VERSION %d.%d", major, minor);
   else
      zk_fail("major-minor", "GL_MAJOR_VERSION.GL_MINOR_VERSION got=%d.%d (glGetError 0x%x) expected=%d.%d", major,
              minor, e, cap / 10, cap % 10);
}

/* ctx->disp is the public dispatch of struct _CGLContextObject (<OpenGL/CGLContext.h>); on the Zink path the adapter
 * puts its own entries there (zink/nvk_cgl.c:511-517, use_zink). */
static void zk_check_dispatch(CGLContextObj ctx)
{
   char owner[PATH_MAX], swap[PATH_MAX];
   zk_owner((const void *)ctx->disp.get_string, owner, sizeof(owner));
   zk_owner((const void *)ctx->disp.swap_APPLE, swap, sizeof(swap));
   zk_info("after the first GL call: get_string in %s, swap_APPLE in %s", owner, swap);
   bool adapter = zk_ends_with(owner, ZK_CGL);
   if (zk_software) {
      if (!adapter) zk_pass("dispatch", "the software context's get_string is not the adapter's (%s)", owner);
      else zk_fail("dispatch", "the software context's get_string is in %s, the adapter", owner);
   } else {
      if (adapter) zk_pass("dispatch", "get_string is the adapter's: %s", owner);
      else zk_fail("dispatch", "get_string got=%s expected=...%s", owner, ZK_CGL);
   }
}

static void zk_check_images(void)
{
   const char *const suffixes[] = {ZK_BUNDLE, ZK_CGL, ZK_ZINK, ZK_VULKAN};
   const char *const names[] = {"bundle", "adapter", "zink", "vulkan"};
   int missing = 0;
   char first[64] = "";
   for (int i = 0; i < 4; ++i) {
      char real[PATH_MAX];
      if (zk_image(suffixes[i], real, sizeof(real))) zk_info("image %s %s", names[i], real);
      else {
         zk_info("image %s not loaded (...%s)", names[i], suffixes[i]);
         if (!missing++) snprintf(first, sizeof(first), "%s", suffixes[i]);
      }
   }
   if (zk_software) return;
   if (!missing) zk_pass("images", "the bundle, libnvk_cgl, libnvk_zink and libnvk_zink_vulkan are loaded");
   else zk_fail("images", "%d of the four images are not loaded, first ...%s", missing, first);
}

/* ---- path, rinfo ------------------------------------------------------------------------------------------------- */

static void sub_path(void)
{
   CGLPixelFormatObj pf = zk_pixel_format(ZK_GL4, false, true);
   CGLContextObj ctx = zk_context(pf, NULL);
   char owner[PATH_MAX], swap[PATH_MAX];
   zk_owner((const void *)ctx->disp.get_string, owner, sizeof(owner));
   zk_owner((const void *)ctx->disp.swap_APPLE, swap, sizeof(swap));
   zk_info("before the first GL call: get_string in %s, swap_APPLE in %s", owner, swap);
   zk_make_current(ctx);
   zk_gate(zk_string(GL_RENDERER)); /* the first GL call: the Zink context is made now (zink/nvk_cgl.c:753-777) */
   zk_check_strings(false);
   zk_check_dispatch(ctx);
   zk_check_images();
   zk_info("PATH %s", zk_software ? "software" : "zink");
   CGLSetCurrentContext(NULL);
   CGLDestroyContext(ctx);
   CGLDestroyPixelFormat(pf);
   zk_finish();
}

static void sub_rinfo(void)
{
   CGLRendererInfoObj info = NULL;
   GLint count = 0;
   CGLError e = CGLQueryRendererInfo(CGDisplayIDToOpenGLDisplayMask(CGMainDisplayID()), &info, &count);
   if (e || !info) {
      zk_fail("query", "CGLQueryRendererInfo gave %d (%s)", (int)e, CGLErrorString(e));
      zk_finish();
   }
   int online = 0;
   GLint vram_of_online = 0;
   for (GLint i = 0; i < count; ++i) {
      GLint id = 0, accelerated = 0, on = 0, vram = 0, major = 0;
      CGLDescribeRenderer(info, i, kCGLRPRendererID, &id);
      CGLDescribeRenderer(info, i, kCGLRPAccelerated, &accelerated);
      CGLDescribeRenderer(info, i, kCGLRPOnline, &on);
      CGLDescribeRenderer(info, i, kCGLRPVideoMemoryMegabytes, &vram);
      CGLDescribeRenderer(info, i, kCGLRPMajorGLVersion, &major);
      zk_info("renderer %d: id 0x%08x accelerated %d online %d video memory %d MB major GL version %d", (int)i,
              (unsigned)id, (int)accelerated, (int)on, (int)vram, (int)major);
      if (accelerated && on) {
         ++online;
         vram_of_online = vram;
      }
   }
   CGLDestroyRendererInfo(info);
   if (count >= 1) zk_pass("count", "%d renderer(s) for the main display", (int)count);
   else zk_fail("count", "renderers got=0 expected=at least 1");
   if (online == 1 && vram_of_online > 0)
      zk_pass("accelerated", "exactly one accelerated online renderer, with %d MB of video memory",
              (int)vram_of_online);
   else
      zk_fail("accelerated", "accelerated online renderers got=%d (video memory %d MB) expected=1 with more than 0 MB",
              online, (int)vram_of_online);
   char real[PATH_MAX];
   zk_info("bundle %s", zk_image(ZK_BUNDLE, real, sizeof(real)) ? real : "not loaded by the renderer query");
   zk_finish();
}

/* ---- profile ----------------------------------------------------------------------------------------------------- */

static void zk_check_extensions(bool legacy)
{
   NSSet<NSString *> *allowed = legacy ? zk_compat_ext : zk_core_ext;
   if (!allowed) {
      zk_fail("extensions", "no --map: the extension list of cgl-dispatch-map.json is needed");
      return;
   }
   int count = 0, outside = 0;
   char first[160] = "";
   zk_gl_error();
   if (legacy) {
      const char *all = (const char *)glGetString(GL_EXTENSIONS);
      if (!all) {
         zk_fail("extensions", "glGetString(GL_EXTENSIONS) is NULL");
         return;
      }
      const char *p = all;
      while (*p) {
         while (*p == ' ') ++p;
         const char *end = p;
         while (*end && *end != ' ') ++end;
         if (end > p) {
            ++count;
            NSString *name = [[NSString alloc] initWithBytes:p length:(NSUInteger)(end - p)
                                                    encoding:NSUTF8StringEncoding];
            if (![allowed containsObject:name] && !outside++) snprintf(first, sizeof(first), "%s", name.UTF8String);
         }
         p = end;
      }
      /* The adapter answers GL_NUM_EXTENSIONS from the same table in legacy contexts too (zink/nvk_cgl.c:442-450). */
      GLint n = -1;
      glGetIntegerv(GL_NUM_EXTENSIONS, &n);
      GLenum e = zk_gl_error();
      if (!e && n == count) zk_pass("num-extensions", "GL_NUM_EXTENSIONS %d equals the names in GL_EXTENSIONS", n);
      else zk_fail("num-extensions", "GL_NUM_EXTENSIONS got=%d (glGetError 0x%x) expected=%d", n, e, count);
   } else {
      GLint n = 0;
      glGetIntegerv(GL_NUM_EXTENSIONS, &n);
      int nulls = 0;
      for (GLint i = 0; i < n && i < 16384; ++i) {
         const char *name = (const char *)glGetStringi(GL_EXTENSIONS, (GLuint)i);
         if (!name) {
            ++nulls;
            continue;
         }
         ++count;
         if (![allowed containsObject:[NSString stringWithUTF8String:name]] && !outside++)
            snprintf(first, sizeof(first), "%s", name);
      }
      if (nulls) zk_fail("extensions-indexed", "glGetStringi gave NULL for %d of %d valid indices", nulls, (int)n);
      zk_gl_error();
      /* An index past the end goes to Mesa as UINT32_MAX, which keeps Mesa's error (zink/nvk_cgl.c:433-440). */
      const GLubyte *past = glGetStringi(GL_EXTENSIONS, (GLuint)n);
      GLenum e = glGetError();
      zk_gl_error();
      if (!past && e == GL_INVALID_VALUE)
         zk_pass("extensions-past-end", "glGetStringi(GL_EXTENSIONS, %d) is NULL with GL_INVALID_VALUE", (int)n);
      else
         zk_fail("extensions-past-end", "glGetStringi(GL_EXTENSIONS, %d) got=%s, glGetError 0x%x expected=NULL, 0x%x",
                 (int)n, past ? (const char *)past : "NULL", e, GL_INVALID_VALUE);
   }
   if (count >= 1 && !outside)
      zk_pass("extensions", "%d extensions, all in the %s list of cgl-dispatch-map.json (%lu names)", count,
              legacy ? "compatibility" : "core", (unsigned long)allowed.count);
   else
      zk_fail("extensions", "%d extensions, %d not in the map's %s list (first %s)", count, outside,
              legacy ? "compatibility" : "core", first[0] ? first : "none");
}

static void sub_profile(int profile, bool double_buffer, bool alpha)
{
   const bool legacy = profile == ZK_LEGACY;
   CGLPixelFormatObj pf = zk_pixel_format(profile, double_buffer, alpha);
   GLint described_db = 0, described_alpha = 0;
   CGLDescribePixelFormat(pf, 0, kCGLPFADoubleBuffer, &described_db);
   CGLDescribePixelFormat(pf, 0, kCGLPFAAlphaSize, &described_alpha);
   zk_info("pixel format %s %s %s: double buffer %d, alpha size %d", zk_profile_names[profile],
           double_buffer ? "db" : "sb", alpha ? "alpha" : "noalpha", (int)described_db, (int)described_alpha);
   CGLContextObj ctx = zk_context(pf, NULL);
   zk_make_current(ctx);
   /* A gl3 context whose GLI profile byte is neither 0 nor 1 stays Apple's with the layout line "the GLI profile byte
    * is <n>, neither 0 for legacy nor 1 for core" (zink/nvk_cgl.c:82, :719-726; nvk_cgl_layout_reason in
    * zink/nvk_cgl_guard.h): zk_gate makes the row inactive with that text. */
   zk_gate(zk_string(GL_RENDERER));
   zk_info("profile byte: the %s context is served by Zink", zk_profile_names[profile]);
   zk_check_strings(legacy);
   zk_check_dispatch(ctx);

   /* The default framebuffer mirrors double buffering and alpha of the pixel format: RGBA8 when the described alpha
    * size is above 0, else RGBX8 (zink/nvk_cgl.c:755-758, :771-772; zink/nvk_zink.c:186-190). */
   GLboolean db = GL_FALSE;
   glGetBooleanv(GL_DOUBLEBUFFER, &db);
   GLint alpha_bits = -1;
   if (legacy) glGetIntegerv(GL_ALPHA_BITS, &alpha_bits);
   else {
      glBindFramebuffer(GL_FRAMEBUFFER, 0);
      glGetFramebufferAttachmentParameteriv(GL_FRAMEBUFFER, described_db ? GL_BACK_LEFT : GL_FRONT_LEFT,
                                            GL_FRAMEBUFFER_ATTACHMENT_ALPHA_SIZE, &alpha_bits);
   }
   GLenum e = zk_gl_error();
   if (!!db == !!described_db) zk_pass("doublebuffer", "GL_DOUBLEBUFFER %d as the pixel format", (int)db);
   else zk_fail("doublebuffer", "GL_DOUBLEBUFFER got=%d expected=%d", (int)db, (int)!!described_db);
   GLint want_alpha = described_alpha > 0 ? 8 : 0;
   if (!e && alpha_bits == want_alpha) zk_pass("alpha", "the default framebuffer has %d alpha bits", (int)alpha_bits);
   else zk_fail("alpha", "default framebuffer alpha bits got=%d (glGetError 0x%x) expected=%d", (int)alpha_bits, e,
                (int)want_alpha);

   zk_gl g;
   if (zk_gl_setup(&g, legacy ? 120 : profile == ZK_GL3 ? 150 : 410, "programs") && zk_gl_fbo(&g, 64, 64, "fbo")) {
      const unsigned char clear[4] = {64, 128, 191, 255}, green[4] = {0, 255, 0, 255};
      glClearColor(0.25f, 0.5f, 0.75f, 1.0f);
      glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
      const int at[5][2] = {{0, 0}, {63, 0}, {0, 63}, {63, 63}, {32, 32}};
      char why[160];
      bool ok = true;
      for (int i = 0; i < 5 && ok; ++i) ok = zk_pixel_ok(at[i][0], at[i][1], clear, 1, why, sizeof(why));
      if (ok) zk_pass("clear", "the four corners and the centre are (64,128,191,255) within 1");
      else zk_fail("clear", "%s", why);
      zk_no_error("clear", "the clear and its readback");
      if (legacy) {
         glUseProgram(0);
         glMatrixMode(GL_PROJECTION);
         glLoadIdentity();
         glMatrixMode(GL_MODELVIEW);
         glLoadIdentity();
         glBegin(GL_TRIANGLES);
         glColor3f(0.0f, 1.0f, 0.0f);
         glVertex2f(-1.0f, -1.0f);
         glVertex2f(1.0f, -1.0f);
         glVertex2f(-1.0f, 1.0f);
         glEnd();
      } else {
         zk_gl_lower_left(&g, green);
      }
      ok = zk_pixel_ok(8, 8, green, 0, why, sizeof(why)) && zk_pixel_ok(56, 56, clear, 1, why, sizeof(why));
      if (ok) zk_pass("triangle", "(8,8) is the triangle's (0,255,0,255) and (56,56) still the clear colour");
      else zk_fail("triangle", "%s", why);
      zk_no_error("triangle", "the triangle and its readback");
      zk_gl_free(&g);
   }
   zk_check_extensions(legacy);
   CGLSetCurrentContext(NULL);
   CGLDestroyContext(ctx);
   CGLDestroyPixelFormat(pf);
   zk_finish();
}

/* ---- offscreen --------------------------------------------------------------------------------------------------- */

/* A context of its own, current, whose renderer is the one the check is about. */
static CGLContextObj zk_offscreen_context(CGLPixelFormatObj pf)
{
   CGLContextObj ctx = zk_context(pf, NULL);
   zk_make_current(ctx);
   zk_gate(zk_string(GL_RENDERER));
   return ctx;
}

static void sub_offscreen(void)
{
   CGLPixelFormatObj pf = zk_pixel_format(ZK_GL4, false, true);
   CGLContextObj ctx = zk_offscreen_context(pf);
   /* Zink sizes the default drawable from Apple's GL_VIEWPORT at the first call: 1x1 without a drawable
    * (zink/nvk_cgl.c:759-764). Information only. */
   GLint viewport[4] = {-1, -1, -1, -1};
   glGetIntegerv(GL_VIEWPORT, viewport);
   zk_info("default framebuffer GL_VIEWPORT %d,%d,%d,%d", viewport[0], viewport[1], viewport[2], viewport[3]);

   zk_gl g;
   if (!zk_gl_setup(&g, 150, "programs") || !zk_gl_fbo(&g, 256, 256, "fbo")) zk_finish();
   const unsigned char blue[4] = {0, 0, 255, 255}, red[4] = {255, 0, 0, 255}, green[4] = {0, 255, 0, 255};
   zk_clear(blue);
   glEnable(GL_DEPTH_TEST);
   glDepthFunc(GL_LESS);
   /* Red, nearer (z 0.2), is drawn first; green (z 0.5) after it must fail the depth test where they overlap. */
   zk_gl_rect(&g, 96, 96, 224, 224, 0.2f, red);
   zk_gl_rect(&g, 32, 32, 160, 160, 0.5f, green);
   glDisable(GL_DEPTH_TEST);
   static unsigned char pixels[256 * 256 * 4], mapped_copy[256 * 256 * 4];
   memset(pixels, 0xee, sizeof(pixels));
   glPixelStorei(GL_PACK_ALIGNMENT, 4);
   glReadPixels(0, 0, 256, 256, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
   int wrong = 0;
   char first[200] = "";
   for (int y = 0; y < 256; ++y)
      for (int x = 0; x < 256; ++x) {
         bool in_red = x >= 96 && x < 224 && y >= 96 && y < 224, in_green = x >= 32 && x < 160 && y >= 32 && y < 160;
         const unsigned char *want = in_red ? red : in_green ? green : blue;
         const unsigned char *got = pixels + 4 * (y * 256 + x);
         if (memcmp(got, want, 4) && !wrong++)
            snprintf(first, sizeof(first), "pixel (%d,%d) got=(%u,%u,%u,%u) expected=(%u,%u,%u,%u)", x, y, got[0],
                     got[1], got[2], got[3], want[0], want[1], want[2], want[3]);
      }
   if (!wrong) zk_pass("depth", "all 65536 pixels: red in front where the quads overlap, green, and the clear colour");
   else zk_fail("depth", "%d of 65536 pixels wrong, first %s", wrong, first);
   zk_no_error("depth", "the depth-tested draws and the readback");

   GLuint pbo = 0;
   glGenBuffers(1, &pbo);
   glBindBuffer(GL_PIXEL_PACK_BUFFER, pbo);
   glBufferData(GL_PIXEL_PACK_BUFFER, sizeof(pixels), NULL, GL_STREAM_READ);
   glReadPixels(0, 0, 256, 256, GL_RGBA, GL_UNSIGNED_BYTE, (void *)0);
   const void *mapped = glMapBufferRange(GL_PIXEL_PACK_BUFFER, 0, sizeof(pixels), GL_MAP_READ_BIT);
   bool same = false;
   if (mapped) {
      memcpy(mapped_copy, mapped, sizeof(mapped_copy));
      same = !memcmp(mapped_copy, pixels, sizeof(pixels));
      glUnmapBuffer(GL_PIXEL_PACK_BUFFER);
   }
   glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
   glDeleteBuffers(1, &pbo);
   if (same) zk_pass("pbo", "a pixel-pack buffer mapped for reading holds the same 262144 bytes");
   else zk_fail("pbo", "%s",
                mapped ? "the mapped pixel-pack buffer differs from glReadPixels" : "glMapBufferRange is NULL");
   zk_no_error("pbo", "the pixel-pack readback");

   zk_gl_rect(&g, 0, 0, 8, 8, 0.0f, green);
   GLsync sync = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
   GLenum waited = sync ? glClientWaitSync(sync, GL_SYNC_FLUSH_COMMANDS_BIT, 2000000000ull) : 0;
   if (sync) glDeleteSync(sync);
   if (waited == GL_ALREADY_SIGNALED || waited == GL_CONDITION_SATISFIED)
      zk_pass("fence", "glClientWaitSync gave 0x%x within 2 s", waited);
   else zk_fail("fence", "glClientWaitSync got=0x%x expected=GL_ALREADY_SIGNALED or GL_CONDITION_SATISFIED", waited);
   zk_no_error("fence", "the fence");

   /* A 4x4 texture uploaded with glTexSubImage2D, copied texel by texel into a 4x4 framebuffer and read back. */
   unsigned char texels[4 * 4 * 4], back[4 * 4 * 4];
   zk_pattern(texels, 4, 4, 5);
   GLuint tex = 0;
   glGenTextures(1, &tex);
   glBindTexture(GL_TEXTURE_2D, tex);
   glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
   glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
   glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 4, 4, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
   glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, 4, 4, GL_RGBA, GL_UNSIGNED_BYTE, texels);
   zk_gl small;
   if (zk_gl_setup(&small, 150, "texture") && zk_gl_fbo(&small, 4, 4, "texture")) {
      zk_clear(blue);
      zk_gl_fetch(&small, tex);
      memset(back, 0xee, sizeof(back));
      glReadPixels(0, 0, 4, 4, GL_RGBA, GL_UNSIGNED_BYTE, back);
      if (!memcmp(back, texels, sizeof(back))) zk_pass("texture", "a 4x4 texture is sampled back exactly");
      else zk_fail("texture", "texel (0,0) got=(%u,%u,%u,%u) expected=(%u,%u,%u,%u), or another texel differs", back[0],
                   back[1], back[2], back[3], texels[0], texels[1], texels[2], texels[3]);
      zk_no_error("texture", "the texture upload and the copy");
      zk_gl_free(&small);
   }
   glDeleteTextures(1, &tex);

   /* No view and no drawable: present_drawable returns 2 without a failure, so the adapter's last error is NULL,
    * which nvk_cgl_last_error() reports as "none" (zink/nvk_cgl.c:157, :1117-1147). */
   CGLError flushed[3];
   for (int i = 0; i < 3; ++i) flushed[i] = CGLFlushDrawable(ctx);
   GLenum flush_error = zk_gl_error();
   if (zk_software) {
      zk_info("CGLFlushDrawable on the software control gave %d, %d, %d", (int)flushed[0], (int)flushed[1],
              (int)flushed[2]);
      if (!flush_error) zk_pass("flush", "three CGLFlushDrawable calls without a drawable, no GL error");
      else zk_fail("flush", "glGetError got=0x%x expected=0 after CGLFlushDrawable", flush_error);
   } else {
      const char *last = zk_last_error();
      if (!flushed[0] && !flushed[1] && !flushed[2] && !flush_error && !strcmp(last, "none"))
         zk_pass("flush", "three CGLFlushDrawable calls without a drawable: kCGLNoError, no GL error, last error none");
      else
         zk_fail("flush", "CGLFlushDrawable got=%d,%d,%d glGetError 0x%x nvk_cgl_last_error %s expected=0,0,0 0x0 none",
                 (int)flushed[0], (int)flushed[1], (int)flushed[2], flush_error, last);
   }
   zk_gl_free(&g);
   CGLSetCurrentContext(NULL);
   CGLDestroyContext(ctx);

   /* The process still works after a destroy. */
   CGLContextObj again = zk_context(pf, NULL);
   zk_make_current(again);
   if (zk_same_renderer("second-context")) {
      zk_gl h;
      if (zk_gl_setup(&h, 150, "second-context") && zk_gl_fbo(&h, 16, 16, "second-context")) {
         zk_clear(blue);
         zk_gl_rect(&h, 0, 0, 8, 16, 0.0f, red);
         char why[160];
         if (zk_pixel_ok(4, 8, red, 0, why, sizeof(why)) && zk_pixel_ok(12, 8, blue, 0, why, sizeof(why)))
            zk_pass("second-context", "a context made after the first was destroyed renders");
         else zk_fail("second-context", "%s", why);
         zk_gl_free(&h);
      }
   }
   CGLSetCurrentContext(NULL);
   CGLDestroyContext(again);
   CGLDestroyPixelFormat(pf);
   zk_finish();
}

/* ---- legacy ------------------------------------------------------------------------------------------------------ */

static void sub_legacy(void)
{
   CGLPixelFormatObj pf = zk_pixel_format(ZK_LEGACY, false, true);
   CGLContextObj ctx = zk_offscreen_context(pf);
   zk_gl g;
   if (!zk_gl_setup(&g, 120, "programs") || !zk_gl_fbo(&g, 64, 64, "fbo")) zk_finish();
   const unsigned char black[4] = {0, 0, 0, 255}, red[4] = {255, 0, 0, 255}, blue[4] = {0, 0, 255, 255};
   zk_clear(black);
   glUseProgram(0);
   glMatrixMode(GL_PROJECTION);
   glLoadIdentity();
   glOrtho(0, 64, 0, 64, -1, 1);
   glMatrixMode(GL_MODELVIEW);
   glLoadIdentity();
   GLuint list = glGenLists(1);
   glNewList(list, GL_COMPILE);
   glBegin(GL_QUADS);
   glColor3ub(255, 0, 0);
   glVertex2f(0, 0);
   glVertex2f(32, 0);
   glVertex2f(32, 64);
   glVertex2f(0, 64);
   glEnd();
   glEndList();
   glCallList(list);
   zk_no_error("display-list", "the display list");
   /* A #version 120 shader over x >= 32; it writes gl_Position itself, so the matrices do not apply. */
   zk_gl_rect(&g, 32, 0, 64, 64, 0.0f, blue);
   zk_no_error("glsl-120", "the GLSL 1.20 draw");
   char why[160];
   if (zk_pixel_ok(16, 32, red, 0, why, sizeof(why))) zk_pass("display-list", "(16,32) is the list's red quad");
   else zk_fail("display-list", "%s", why);
   if (zk_pixel_ok(48, 32, blue, 0, why, sizeof(why))) zk_pass("glsl-120", "(48,32) is the shader's blue");
   else zk_fail("glsl-120", "%s", why);
   glDeleteLists(list, 1);

   const char *extensions = (const char *)glGetString(GL_EXTENSIONS);
   if (extensions) zk_pass("extensions", "glGetString(GL_EXTENSIONS) is a list of %zu bytes", strlen(extensions));
   else zk_fail("extensions", "glGetString(GL_EXTENSIONS) got=NULL");
   zk_gl_error();
   if (!zk_software) {
      /* Answered by the adapter even in legacy contexts (zink/nvk_cgl.c:442-450); Apple's 2.1 path may give
       * GL_INVALID_ENUM, so this is judged on Zink only. */
      GLint major = -1;
      glGetIntegerv(GL_MAJOR_VERSION, &major);
      GLenum e = zk_gl_error();
      const char *version = zk_string(GL_VERSION);
      if (!e && major >= 2 && atoi(version) == major)
         zk_pass("major-version", "GL_MAJOR_VERSION %d matches GL_VERSION %s", major, version);
      else zk_fail("major-version", "GL_MAJOR_VERSION got=%d (glGetError 0x%x) expected=at least 2 and the major of %s",
                   major, e, version);
      /* Apple's VAO names are mapped onto Mesa's core names (zink/nvk_cgl.c:330-347). */
      GLuint vao = 0;
      glGenVertexArraysAPPLE(1, &vao);
      glBindVertexArrayAPPLE(vao);
      GLboolean is = glIsVertexArrayAPPLE(vao);
      glBindVertexArrayAPPLE(0);
      glDeleteVertexArraysAPPLE(1, &vao);
      GLboolean gone = glIsVertexArrayAPPLE(vao);
      e = zk_gl_error();
      if (vao && is && !gone && !e) zk_pass("apple-vao", "glGenVertexArraysAPPLE, Bind, Is and Delete work");
      else zk_fail("apple-vao", "name %u, IsVertexArrayAPPLE after bind %d, after delete %d, glGetError 0x%x", vao,
                   (int)is, (int)gone, e);
   }
   zk_gl_free(&g);
   CGLSetCurrentContext(NULL);
   CGLDestroyContext(ctx);
   CGLDestroyPixelFormat(pf);
   zk_finish();
}

/* ---- iosurface --------------------------------------------------------------------------------------------------- */

static IOSurfaceRef zk_surface(int w, int h, int bytes_per_element, uint32_t fourcc)
{
   NSDictionary *p = @{
      (__bridge NSString *)kIOSurfaceWidth : @(w),
      (__bridge NSString *)kIOSurfaceHeight : @(h),
      (__bridge NSString *)kIOSurfaceBytesPerElement : @(bytes_per_element),
      (__bridge NSString *)kIOSurfacePixelFormat : @(fourcc),
   };
   IOSurfaceRef s = IOSurfaceCreate((__bridge CFDictionaryRef)p);
   if (!s)
      zk_setup_error("IOSurfaceCreate %dx%d '%c%c%c%c' failed", w, h, (int)(fourcc >> 24), (int)(fourcc >> 16 & 255),
                     (int)(fourcc >> 8 & 255), (int)(fourcc & 255));
   return s;
}

/* Patterns of the 64x64 BGRA surface, as RGBA. 0: quadrants, one with alpha 77; 1 and 2: gradients. */
static void zk_surface_rgba(int which, int x, int y, unsigned char *rgba)
{
   if (which == 0) {
      static const unsigned char q[4][4] = {{255, 0, 0, 255}, {0, 255, 0, 255}, {0, 0, 255, 255}, {10, 20, 30, 77}};
      memcpy(rgba, q[(y >= 32) * 2 + (x >= 32)], 4);
   } else if (which == 1) {
      rgba[0] = (unsigned char)(x * 4);
      rgba[1] = (unsigned char)(y * 4);
      rgba[2] = 128;
      rgba[3] = 255;
   } else {
      rgba[0] = (unsigned char)(255 - x * 4);
      rgba[1] = 60;
      rgba[2] = (unsigned char)(y * 4);
      rgba[3] = 255;
   }
}

static void zk_surface_fill(IOSurfaceRef s, int which)
{
   IOSurfaceLock(s, 0, NULL);
   unsigned char *base = IOSurfaceGetBaseAddress(s);
   size_t row = IOSurfaceGetBytesPerRow(s);
   for (int y = 0; y < 64; ++y)
      for (int x = 0; x < 64; ++x) {
         unsigned char rgba[4], *p = base + (size_t)y * row + 4 * (size_t)x;
         zk_surface_rgba(which, x, y, rgba);
         p[0] = rgba[2];
         p[1] = rgba[1];
         p[2] = rgba[0];
         p[3] = rgba[3];
      }
   IOSurfaceUnlock(s, 0, NULL);
}

/* Reads the 64x64 attachment of the bound framebuffer; returns the number of pixels unlike pattern which. */
static int zk_read_surface_pattern(int which, char *first, size_t size)
{
   static unsigned char got[64 * 64 * 4];
   memset(got, 0xee, sizeof(got));
   glReadPixels(0, 0, 64, 64, GL_RGBA, GL_UNSIGNED_BYTE, got);
   int wrong = 0;
   for (int y = 0; y < 64; ++y)
      for (int x = 0; x < 64; ++x) {
         unsigned char want[4];
         const unsigned char *p = got + 4 * (y * 64 + x);
         zk_surface_rgba(which, x, y, want);
         if (memcmp(p, want, 4) && !wrong++)
            snprintf(first, size, "pixel (%d,%d) got=(%u,%u,%u,%u) expected=(%u,%u,%u,%u)", x, y, p[0], p[1], p[2],
                     p[3], want[0], want[1], want[2], want[3]);
      }
   return wrong;
}

static GLuint zk_rect_texture(void)
{
   GLuint t = 0;
   glGenTextures(1, &t);
   glBindTexture(GL_TEXTURE_RECTANGLE, t);
   glTexParameteri(GL_TEXTURE_RECTANGLE, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
   glTexParameteri(GL_TEXTURE_RECTANGLE, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
   return t;
}

static GLuint zk_rect_fbo(GLuint t, GLenum *status)
{
   GLuint fbo = 0;
   glGenFramebuffers(1, &fbo);
   glBindFramebuffer(GL_FRAMEBUFFER, fbo);
   glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_RECTANGLE, t, 0);
   *status = glCheckFramebufferStatus(GL_FRAMEBUFFER);
   glViewport(0, 0, 64, 64);
   return fbo;
}

static uint16_t zk_half(float f)
{
   /* Only the exact values used here: 0, 0.25, 0.5, 0.75 and 1. */
   return f == 1.0f ? 0x3C00 : f == 0.75f ? 0x3A00 : f == 0.5f ? 0x3800 : f == 0.25f ? 0x3400 : 0;
}

static void sub_iosurface(void)
{
   CGLPixelFormatObj pf = zk_pixel_format(ZK_GL4, false, true);
   CGLContextObj ctx = zk_offscreen_context(pf);
   char why[200];
   /* (i) read. The adapter gets the call through a cloned GLI integer callback, slot 9, parameter 910 (zink/nvk_cgl.c:
    * 566-608), placed when Zink first serves the context (:623-654). Where the adapter cannot place it
    * (the CGL private object is neither a heap block nor a span of the CGL context's block, the engine list neither a
    * heap block nor a span of the private object's block, the list pointer is null or the engine count is outside 1 to
    * 16, no record names the engine, the callback vector is null, starts no heap block or is a block of another size,
    * or dladdr does not name its entries 4, 6, 9 and 15 gliCreateContext, gliDestroyContext, gliSetInteger and
    * gliGetVersion), CGLTexImageIOSurface2D keeps Apple's handler and the adapter prints "NVK Zink:
    * CGLTexImageIOSurface2D in a Zink context stays Apple's; ...", which this row's messages check reports. Only a
    * failed allocation of the clone leaves the hook out with no line. */
   IOSurfaceRef s = zk_surface(64, 64, 4, 0x42475241 /* 'BGRA' */);
   zk_surface_fill(s, 0);
   GLuint tex = zk_rect_texture();
   CGLError bound = CGLTexImageIOSurface2D(ctx, GL_TEXTURE_RECTANGLE, GL_RGBA, 64, 64, GL_BGRA,
                                           GL_UNSIGNED_INT_8_8_8_8_REV, s, 0);
   if (bound) {
      if (zk_software) zk_skip("Apple's software renderer refused CGLTexImageIOSurface2D (%d)", (int)bound);
      zk_fail("bind", "CGLTexImageIOSurface2D(BGRA 64x64) got=%d expected=0; on Zink only, this points at the "
              "integer-callback clone (zink/nvk_cgl.c:623-654; where the adapter cannot place it, it prints a line "
              "that starts NVK Zink: CGLTexImageIOSurface2D in a Zink context stays Apple's)", (int)bound);
      zk_finish();
   }
   zk_pass("bind", "CGLTexImageIOSurface2D(GL_TEXTURE_RECTANGLE, GL_RGBA, BGRA 8888 REV) returned 0");
   GLenum status = 0;
   GLuint fbo = zk_rect_fbo(tex, &status);
   if (status != GL_FRAMEBUFFER_COMPLETE) {
      zk_fail("read", "a framebuffer with the IOSurface texture: status 0x%x", status);
      zk_finish();
   }
   int wrong = zk_read_surface_pattern(0, why, sizeof(why));
   if (!wrong) zk_pass("read", "all 4096 pixels of the surface, BGRA in memory read as RGBA");
   else zk_fail("read", "%d of 4096 pixels wrong, first %s", wrong, why);
   zk_no_error("read", "the IOSurface read");

   /* (ii) write-back: published to the surface on glFlush, glFinish, glFenceSync and present
    * (zink/nvk_cgl.c:365-368 and the generated wrappers). */
   glEnable(GL_SCISSOR_TEST);
   glScissor(0, 0, 32, 64);
   glClearColor(1, 0, 0, 1);
   glClear(GL_COLOR_BUFFER_BIT);
   glDisable(GL_SCISSOR_TEST);
   glFinish();
   IOSurfaceLock(s, kIOSurfaceLockReadOnly, NULL);
   const unsigned char *base = IOSurfaceGetBaseAddress(s);
   size_t row = IOSurfaceGetBytesPerRow(s);
   wrong = 0;
   for (int y = 0; y < 64; ++y)
      for (int x = 0; x < 64; ++x) {
         unsigned char want[4], rgba[4];
         const unsigned char *p = base + (size_t)y * row + 4 * (size_t)x;
         if (x < 32) memcpy(rgba, (const unsigned char[4]){255, 0, 0, 255}, 4);
         else zk_surface_rgba(0, x, y, rgba);
         want[0] = rgba[2];
         want[1] = rgba[1];
         want[2] = rgba[0];
         want[3] = rgba[3];
         if (memcmp(p, want, 4) && !wrong++)
            snprintf(why, sizeof(why), "byte quad (%d,%d) got=B%u G%u R%u A%u expected=B%u G%u R%u A%u", x, y, p[0],
                     p[1], p[2], p[3], want[0], want[1], want[2], want[3]);
      }
   IOSurfaceUnlock(s, kIOSurfaceLockReadOnly, NULL);
   if (!wrong) zk_pass("write", "after glFinish the surface's left half is red and its right half unchanged");
   else zk_fail("write", "%d of 4096 surface pixels wrong after glFinish, first %s", wrong, why);
   zk_no_error("write", "the scissored clear and glFinish");

   /* (iii) refresh: a CPU write inside IOSurfaceLock and IOSurfaceUnlock changes the seed, then glBindTexture and
    * glBindFramebuffer of the names already bound. The adapter copies the surface in again on every such bind when
    * the seed changed (build/generate_cgl_dispatch.py:290-293, zink/nvk_cgl.c:370-374, zink/nvk_iosurface.c:299-332),
    * so this is judged on Zink only. Apple's software renderer kept its own contents through the same binds on macOS
    * 26.7.3 (25G342): the control reports what it reads, after these binds and after an unbind and a bind, as
    * information. */
   zk_surface_fill(s, 1);
   glBindTexture(GL_TEXTURE_RECTANGLE, tex);
   glBindFramebuffer(GL_FRAMEBUFFER, fbo);
   wrong = zk_read_surface_pattern(1, why, sizeof(why));
   if (zk_software) {
      zk_info("refresh on the software control: after a CPU write and a rebind of the bound names GL reads %s%s",
              !wrong ? "the new contents" : "other contents, first ", wrong ? why : "");
      glBindTexture(GL_TEXTURE_RECTANGLE, 0);
      glBindFramebuffer(GL_FRAMEBUFFER, 0);
      glBindTexture(GL_TEXTURE_RECTANGLE, tex);
      glBindFramebuffer(GL_FRAMEBUFFER, fbo);
      wrong = zk_read_surface_pattern(1, why, sizeof(why));
      zk_info("refresh on the software control: after an unbind and a bind GL reads %s%s",
              !wrong ? "the new contents" : "other contents, first ", wrong ? why : "");
   } else if (!wrong) zk_pass("refresh", "after a CPU write and a rebind GL reads the new contents");
   else zk_fail("refresh", "%d of 4096 pixels wrong after a CPU write and a rebind, first %s", wrong, why);
   zk_no_error("refresh", "the rebind and read");

   /* (iii-b) without a rebind: information. Per code Zink keeps its copy until the next bind; what Apple's software
    * renderer reads here is reported as it is. */
   zk_surface_fill(s, 2);
   wrong = zk_read_surface_pattern(2, why, sizeof(why));
   int old = wrong ? zk_read_surface_pattern(1, why, sizeof(why)) : -1;
   zk_info("no rebind after a CPU write: GL reads %s", !wrong ? "the new contents" : !old ? "the previous contents"
                                                                                       : "neither pattern");
   zk_gl_error();
   glBindFramebuffer(GL_FRAMEBUFFER, 0);
   glDeleteFramebuffers(1, &fbo);
   glDeleteTextures(1, &tex);

   /* (iv) formats of surface_format (zink/nvk_iosurface.c:334-370): RGBA16F from 'RGhA' and R8 from 'L008'. */
   IOSurfaceRef h = zk_surface(64, 64, 8, 0x52476841 /* 'RGhA' */);
   IOSurfaceLock(h, 0, NULL);
   unsigned char *hb = IOSurfaceGetBaseAddress(h);
   size_t hrow = IOSurfaceGetBytesPerRow(h);
   for (int y = 0; y < 64; ++y)
      for (int x = 0; x < 64; ++x) {
         uint16_t *p = (uint16_t *)(void *)(hb + (size_t)y * hrow + 8 * (size_t)x);
         p[0] = zk_half(x < 32 ? 0.25f : 0.75f);
         p[1] = zk_half(y < 32 ? 0.5f : 1.0f);
         p[2] = zk_half(0.75f);
         p[3] = zk_half(1.0f);
      }
   IOSurfaceUnlock(h, 0, NULL);
   GLuint htex = zk_rect_texture();
   CGLError hbound =
      CGLTexImageIOSurface2D(ctx, GL_TEXTURE_RECTANGLE, GL_RGBA16F, 64, 64, GL_RGBA, GL_HALF_FLOAT, h, 0);
   if (hbound) {
      if (zk_software) zk_info("the software control refused RGhA as GL_RGBA16F (%d); not judged", (int)hbound);
      else zk_fail("rgba16f", "CGLTexImageIOSurface2D(RGhA, GL_RGBA16F, GL_HALF_FLOAT) got=%d expected=0", (int)hbound);
   } else {
      GLuint hfbo = zk_rect_fbo(htex, &status);
      static float hf[64 * 64 * 4];
      for (size_t i = 0; i < sizeof(hf) / sizeof(hf[0]); ++i) hf[i] = -1.0f;
      if (status == GL_FRAMEBUFFER_COMPLETE) glReadPixels(0, 0, 64, 64, GL_RGBA, GL_FLOAT, hf);
      wrong = 0;
      for (int i = 0; i < 64 * 64 && status == GL_FRAMEBUFFER_COMPLETE; ++i) {
         int x = i % 64, y = i / 64;
         const float want[4] = {x < 32 ? 0.25f : 0.75f, y < 32 ? 0.5f : 1.0f, 0.75f, 1.0f};
         for (int c = 0; c < 4; ++c)
            if (fabsf(hf[4 * i + c] - want[c]) > 1e-3f && !wrong++)
               snprintf(why, sizeof(why), "pixel (%d,%d) channel %d got=%g expected=%g", x, y, c, hf[4 * i + c],
                        want[c]);
      }
      if (status != GL_FRAMEBUFFER_COMPLETE) zk_fail("rgba16f", "a framebuffer with the RGBA16F texture: 0x%x", status);
      else if (!wrong) zk_pass("rgba16f", "an RGhA surface reads back as RGBA16F within 0.001");
      else zk_fail("rgba16f", "%d values wrong, first %s", wrong, why);
      glBindFramebuffer(GL_FRAMEBUFFER, 0);
      glDeleteFramebuffers(1, &hfbo);
   }
   zk_no_error("rgba16f", "the RGBA16F surface");
   glDeleteTextures(1, &htex);
   CFRelease(h);

   IOSurfaceRef l = zk_surface(64, 64, 1, 0x4C303038 /* 'L008' */);
   IOSurfaceLock(l, 0, NULL);
   unsigned char *lb = IOSurfaceGetBaseAddress(l);
   size_t lrow = IOSurfaceGetBytesPerRow(l);
   for (int y = 0; y < 64; ++y)
      for (int x = 0; x < 64; ++x) lb[(size_t)y * lrow + (size_t)x] = (unsigned char)(x * 4 + y);
   IOSurfaceUnlock(l, 0, NULL);
   GLuint ltex = zk_rect_texture();
   CGLError lbound = CGLTexImageIOSurface2D(ctx, GL_TEXTURE_RECTANGLE, GL_R8, 64, 64, GL_RED, GL_UNSIGNED_BYTE, l, 0);
   if (lbound) {
      if (zk_software) zk_info("the software control refused L008 as GL_R8 (%d); not judged", (int)lbound);
      else zk_fail("r8", "CGLTexImageIOSurface2D(L008, GL_R8, GL_RED) got=%d expected=0", (int)lbound);
   } else {
      GLuint lfbo = zk_rect_fbo(ltex, &status);
      static unsigned char lv[64 * 64];
      memset(lv, 0xee, sizeof(lv));
      glPixelStorei(GL_PACK_ALIGNMENT, 1);
      if (status == GL_FRAMEBUFFER_COMPLETE) glReadPixels(0, 0, 64, 64, GL_RED, GL_UNSIGNED_BYTE, lv);
      wrong = 0;
      for (int i = 0; i < 64 * 64 && status == GL_FRAMEBUFFER_COMPLETE; ++i) {
         unsigned char want = (unsigned char)((i % 64) * 4 + i / 64);
         if (lv[i] != want && !wrong++)
            snprintf(why, sizeof(why), "pixel (%d,%d) got=%u expected=%u", i % 64, i / 64, lv[i], want);
      }
      if (status != GL_FRAMEBUFFER_COMPLETE) zk_fail("r8", "a framebuffer with the R8 texture: 0x%x", status);
      else if (!wrong) zk_pass("r8", "an L008 surface reads back as R8 exactly");
      else zk_fail("r8", "%d values wrong, first %s", wrong, why);
      glBindFramebuffer(GL_FRAMEBUFFER, 0);
      glDeleteFramebuffers(1, &lfbo);
   }
   zk_no_error("r8", "the R8 surface");
   glDeleteTextures(1, &ltex);
   CFRelease(l);

   /* (v) errors: only GL_TEXTURE_RECTANGLE is accepted, else 10010; a size beyond the surface is 10008
    * (zink/nvk_iosurface.c:25-26, :377, :391). Apple's own values are not specified, so any nonzero error passes and
    * the value is reported. */
   GLuint t2 = 0;
   glGenTextures(1, &t2);
   glBindTexture(GL_TEXTURE_2D, t2);
   CGLError wrong_target = CGLTexImageIOSurface2D(ctx, GL_TEXTURE_2D, GL_RGBA, 64, 64, GL_BGRA,
                                                  GL_UNSIGNED_INT_8_8_8_8_REV, s, 0);
   GLuint t3 = zk_rect_texture();
   CGLError too_big = CGLTexImageIOSurface2D(ctx, GL_TEXTURE_RECTANGLE, GL_RGBA, 128, 128, GL_BGRA,
                                             GL_UNSIGNED_INT_8_8_8_8_REV, s, 0);
   zk_info("errors: GL_TEXTURE_2D gave %d (Zink's code: 10010), 128x128 of a 64x64 surface gave %d (Zink's code: "
           "10008)", (int)wrong_target, (int)too_big);
   if (wrong_target && too_big) zk_pass("errors", "both invalid calls return a CGL error");
   else zk_fail("errors", "GL_TEXTURE_2D got=%d, 128x128 got=%d expected=both nonzero", (int)wrong_target,
                (int)too_big);
   zk_gl_error();
   glDeleteTextures(1, &t2);
   glDeleteTextures(1, &t3);
   CFRelease(s);
   CGLSetCurrentContext(NULL);
   CGLDestroyContext(ctx);
   CGLDestroyPixelFormat(pf);
   zk_finish();
}

/* ---- share ------------------------------------------------------------------------------------------------------- */

/* A materializing context shares Mesa objects with an already materialized bridge whose GLI_SHARE_GROUP word is equal
 * (zink/nvk_cgl.c:765-773); the guard does not verify that word (nvk_cgl.c:79). */
static void sub_share(char which)
{
   CGLPixelFormatObj pf = zk_pixel_format(ZK_GL4, false, true);
   unsigned char want[64], got[64];
   char why[200];
   zk_buffer_pattern(want, sizeof(want), 3);
   if (which == 'a' || which == 'b') {
      CGLContextObj a = zk_context(pf, NULL);
      zk_make_current(a);
      zk_gate(zk_string(GL_RENDERER));
      GLuint t = zk_texture(4, 4, 1), bo = 0;
      glGenBuffers(1, &bo);
      glBindBuffer(GL_ARRAY_BUFFER, bo);
      glBufferData(GL_ARRAY_BUFFER, sizeof(want), want, GL_STATIC_DRAW);
      glFinish();
      zk_no_error("objects", "creating a texture and a buffer in A");
      if (which == 'a') {
         CGLContextObj b = zk_context(pf, a);
         zk_make_current(b);
         if (zk_same_renderer("shared")) {
            GLboolean is_t = glIsTexture(t), is_b = glIsBuffer(bo);
            if (is_t && is_b) zk_pass("names", "texture %u and buffer %u of A are names in B", t, bo);
            else zk_fail("names", "in B glIsTexture(%u) got=%d glIsBuffer(%u) got=%d expected=1 1", t, (int)is_t, bo,
                         (int)is_b);
            int d = zk_texture_differs(t, 4, 4, 1, why, sizeof(why));
            if (!d) zk_pass("texture", "B reads A's 4x4 texture exactly");
            else zk_fail("texture", "B reading A's texture: %s", why);
            memset(got, 0xee, sizeof(got));
            glBindBuffer(GL_ARRAY_BUFFER, bo);
            glGetBufferSubData(GL_ARRAY_BUFFER, 0, sizeof(got), got);
            if (!memcmp(got, want, sizeof(got))) zk_pass("buffer", "B reads A's 64 buffer bytes exactly");
            else zk_fail("buffer", "B reading A's buffer: byte 0 got=%u expected=%u, or another byte differs", got[0],
                         want[0]);
            zk_no_error("shared", "the reads in B");
         }
         CGLSetCurrentContext(NULL);
         CGLDestroyContext(b);
      } else {
         CGLContextObj c = zk_context(pf, NULL);
         zk_make_current(c);
         if (zk_same_renderer("isolated")) {
            GLboolean is_t = glIsTexture(t), is_b = glIsBuffer(bo);
            if (!is_t && !is_b)
               zk_pass("isolated", "A's texture %u and buffer %u are not names in the unshared C", t, bo);
            else zk_fail("isolated", "in C glIsTexture(%u) got=%d glIsBuffer(%u) got=%d expected=0 0", t, (int)is_t, bo,
                         (int)is_b);
            GLuint mine[4] = {0};
            glGenTextures(4, mine);
            GLuint other = 0;
            for (int i = 0; i < 4; ++i) {
               glBindTexture(GL_TEXTURE_2D, mine[i]);
               if (mine[i] != t) other = mine[i];
            }
            glBindTexture(GL_TEXTURE_2D, 0);
            glFinish();
            zk_make_current(a);
            GLboolean in_a = other ? glIsTexture(other) : GL_TRUE;
            if (other && !in_a) zk_pass("isolated-back", "C's texture %u is not a name in A", other);
            else zk_fail("isolated-back", "in A glIsTexture(%u) of C got=%d expected=0", other, (int)in_a);
            zk_no_error("isolated", "the name queries");
         }
         CGLSetCurrentContext(NULL);
         CGLDestroyContext(c);
      }
      CGLSetCurrentContext(NULL);
      CGLDestroyContext(a);
   } else if (which == 'c') {
      /* B (sharing A) makes the first GL call; A materializes later and must find B's objects. */
      CGLContextObj a = zk_context(pf, NULL), b = zk_context(pf, a);
      zk_make_current(b);
      zk_gate(zk_string(GL_RENDERER));
      GLuint t = zk_texture(4, 4, 2);
      glFinish();
      zk_no_error("objects", "creating a texture in B");
      zk_make_current(a);
      if (zk_same_renderer("order")) {
         GLboolean is_t = glIsTexture(t);
         int d = is_t ? zk_texture_differs(t, 4, 4, 2, why, sizeof(why)) : -1;
         if (is_t && !d) zk_pass("order", "A, materialized after B, reads B's texture %u exactly", t);
         else zk_fail("order", "in A glIsTexture(%u) got=%d%s%s expected=1 and the pattern", t, (int)is_t,
                      is_t ? ", " : "", is_t ? why : "");
         zk_no_error("order", "the read in A");
      }
      CGLSetCurrentContext(NULL);
      CGLDestroyContext(b);
      CGLDestroyContext(a);
   } else {
      /* B shares A but makes its first GL call only after A is destroyed. Apple's CGL keeps the share group alive;
       * the adapter looks only at live, materialized bridges (zink/nvk_cgl.c:765-773 and :779-804), so per code B
       * gets a new, empty group. */
      CGLContextObj a = zk_context(pf, NULL);
      zk_make_current(a);
      zk_gate(zk_string(GL_RENDERER));
      GLuint t = zk_texture(4, 4, 4);
      glFinish();
      zk_no_error("objects", "creating a texture in A");
      CGLContextObj b = zk_context(pf, a);
      CGLSetCurrentContext(NULL);
      CGLDestroyContext(a);
      zk_make_current(b);
      if (zk_same_renderer("partner-destroyed")) {
         GLboolean is_t = glIsTexture(t);
         int d = is_t ? zk_texture_differs(t, 4, 4, 4, why, sizeof(why)) : -1;
         zk_gl_error();
         if (is_t && !d) zk_pass("partner-destroyed", "B, first used after A was destroyed, reads A's texture exactly");
         else if (!is_t && !zk_software)
            zk_known_difference("partner-destroyed",
                                "B, first used after A was destroyed, does not see A's texture %u: the adapter shares "
                                "only with a live, materialized bridge (zink/nvk_cgl.c:765-773); Apple's CGL keeps it",
                                t);
         else zk_fail("partner-destroyed", "in B glIsTexture(%u) got=%d%s%s expected=1 and the pattern", t, (int)is_t,
                      is_t ? ", " : "", is_t ? why : "");
      }
      CGLSetCurrentContext(NULL);
      CGLDestroyContext(b);
   }
   CGLDestroyPixelFormat(pf);
   zk_finish();
}

/* ---- threads ----------------------------------------------------------------------------------------------------- */

static CGLPixelFormatObj zk_thread_pf;

typedef struct {
   int index;
   bool done;
} zk_thread_arg;

static void zk_frame_colour(int t, int frame, unsigned char *clear, unsigned char *tri)
{
   clear[0] = (unsigned char)(10 + t * 60);
   clear[1] = (unsigned char)(frame % 256);
   clear[2] = (unsigned char)(255 - t * 40);
   clear[3] = 255;
   tri[0] = (unsigned char)(250 - t * 60);
   tri[1] = (unsigned char)(255 - frame % 256);
   tri[2] = (unsigned char)(20 + t * 30);
   tri[3] = 255;
}

static void *zk_threads_a_main(void *p)
{
   zk_thread_arg *arg = p;
   char name[32], why[200];
   snprintf(name, sizeof(name), "thread-%d", arg->index);
   CGLContextObj ctx = zk_context(zk_thread_pf, NULL);
   if (CGLSetCurrentContext(ctx)) {
      zk_fail(name, "CGLSetCurrentContext failed");
      return NULL;
   }
   zk_gl g;
   int wrong = 0, reads = 0;
   if (zk_same_renderer(name) && zk_gl_setup(&g, 150, name) && zk_gl_fbo(&g, 64, 64, name)) {
      for (int f = 0; f < 200; ++f) {
         unsigned char clear[4], tri[4];
         zk_frame_colour(arg->index, f, clear, tri);
         zk_clear(clear);
         zk_gl_lower_left(&g, tri);
         if (f % 20 == 19) {
            ++reads;
            if ((!zk_pixel_ok(16, 16, tri, 0, why, sizeof(why)) || !zk_pixel_ok(48, 48, clear, 0, why, sizeof(why))) &&
                !wrong++)
               zk_fail(name, "frame %d: %s", f, why);
         } else {
            glFlush();
         }
      }
      GLenum e = zk_gl_error();
      if (!wrong && !e) zk_pass(name, "200 frames in its own context, %d readbacks of its own colours", reads);
      else if (e) zk_fail(name, "glGetError got=0x%x expected=0", e);
      zk_gl_free(&g);
   }
   CGLSetCurrentContext(NULL);
   CGLDestroyContext(ctx);
   arg->done = true;
   return NULL;
}

typedef struct {
   CGLContextObj a, b;
   zk_event ready, sent, received;
   GLuint texture;
   GLsync sync;
   int iteration;
   int wrong, timeouts;
} zk_handoff;

static void zk_handoff_colour(int i, unsigned char *rgba)
{
   rgba[0] = (unsigned char)(i * 5);
   rgba[1] = (unsigned char)(255 - i * 3);
   rgba[2] = (unsigned char)(i * 7 + 13);
   rgba[3] = 255;
}

/* T1: renders into the shared texture in A and hands a fence to T2. */
static void *zk_handoff_producer(void *p)
{
   zk_handoff *h = p;
   if (CGLSetCurrentContext(h->a)) return NULL;
   if (!zk_same_renderer("producer")) {
      zk_event_set(&h->ready, -1);
      return NULL;
   }
   GLuint fbo = 0, t = 0;
   glGenTextures(1, &t);
   glBindTexture(GL_TEXTURE_2D, t);
   glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
   glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
   glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 16, 16, 0, GL_RGBA, GL_UNSIGNED_BYTE, NULL);
   glGenFramebuffers(1, &fbo);
   glBindFramebuffer(GL_FRAMEBUFFER, fbo);
   glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, t, 0);
   glViewport(0, 0, 16, 16);
   glFinish();
   h->texture = t;
   zk_event_set(&h->ready, 1);
   for (int i = 0; i < 50; ++i) {
      if (i && !zk_event_wait(&h->received, i, 5.0)) {
         ++h->timeouts;
         break;
      }
      unsigned char c[4];
      zk_handoff_colour(i, c);
      glBindFramebuffer(GL_FRAMEBUFFER, fbo);
      zk_clear(c);
      h->sync = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
      glFlush();
      zk_event_set(&h->sent, i + 1);
   }
   zk_event_wait(&h->received, 50, 5.0);
   GLenum e = zk_gl_error();
   if (e) zk_fail("producer", "glGetError got=0x%x expected=0", e);
   glDeleteFramebuffers(1, &fbo);
   glFinish();
   CGLSetCurrentContext(NULL);
   return NULL;
}

/* T2: waits on the fence in B, copies the shared texture into its own framebuffer and reads it. */
static void *zk_handoff_consumer(void *p)
{
   zk_handoff *h = p;
   if (!zk_event_wait(&h->ready, 1, 10.0)) {
      ++h->timeouts;
      return NULL;
   }
   if (CGLSetCurrentContext(h->b)) return NULL;
   zk_gl g;
   if (!zk_same_renderer("consumer") || !zk_gl_setup(&g, 150, "consumer") || !zk_gl_fbo(&g, 16, 16, "consumer")) {
      zk_event_set(&h->received, 1000);
      CGLSetCurrentContext(NULL);
      return NULL;
   }
   char why[200];
   for (int i = 0; i < 50; ++i) {
      if (!zk_event_wait(&h->sent, i + 1, 5.0)) {
         ++h->timeouts;
         break;
      }
      GLsync sync = h->sync;
      glWaitSync(sync, 0, GL_TIMEOUT_IGNORED);
      GLenum waited = glClientWaitSync(sync, GL_SYNC_FLUSH_COMMANDS_BIT, 2000000000ull);
      if (waited != GL_ALREADY_SIGNALED && waited != GL_CONDITION_SATISFIED) {
         if (!h->timeouts++) zk_fail("fence", "iteration %d: glClientWaitSync got=0x%x expected=signalled", i, waited);
      }
      glDeleteSync(sync);
      unsigned char c[4];
      zk_handoff_colour(i, c);
      zk_gl_fetch(&g, h->texture);
      if ((!zk_pixel_ok(3, 3, c, 0, why, sizeof(why)) || !zk_pixel_ok(12, 12, c, 0, why, sizeof(why))) && !h->wrong++)
         zk_fail("handoff", "iteration %d: %s", i, why);
      zk_event_set(&h->received, i + 1);
   }
   GLenum e = zk_gl_error();
   if (e) zk_fail("consumer", "glGetError got=0x%x expected=0", e);
   zk_gl_free(&g);
   CGLSetCurrentContext(NULL);
   return NULL;
}

static void sub_threads(char which)
{
   zk_thread_pf = zk_pixel_format(ZK_GL4, false, true);
   zk_gate_on_main(zk_thread_pf);
   double start = zk_now();
   if (which == 'a') {
      pthread_t threads[4];
      zk_thread_arg args[4];
      for (int i = 0; i < 4; ++i) {
         args[i].index = i;
         args[i].done = false;
         if (pthread_create(&threads[i], NULL, zk_threads_a_main, &args[i])) zk_setup_error("pthread_create failed");
      }
      for (int i = 0; i < 4; ++i) pthread_join(threads[i], NULL);
      double took = zk_now() - start;
      if (took <= 20.0) zk_pass("time", "four threads of 200 frames took %.1f s", took);
      else zk_fail("time", "four threads of 200 frames took %.1f s, expected at most 20", took);
   } else {
      zk_handoff h = {.ready = ZK_EVENT_INIT, .sent = ZK_EVENT_INIT, .received = ZK_EVENT_INIT};
      h.a = zk_context(zk_thread_pf, NULL);
      h.b = zk_context(zk_thread_pf, h.a);
      pthread_t t1, t2;
      if (pthread_create(&t1, NULL, zk_handoff_producer, &h) || pthread_create(&t2, NULL, zk_handoff_consumer, &h))
         zk_setup_error("pthread_create failed");
      pthread_join(t1, NULL);
      pthread_join(t2, NULL);
      if (!h.wrong && !h.timeouts && h.received.value == 50)
         zk_pass("handoff",
                 "50 fence handoffs from A on one thread to B on another, each with that iteration's colour");
      else zk_fail("handoff", "%d handoffs completed, %d wrong, %d waits timed out", h.received.value, h.wrong,
                   h.timeouts);
      CGLDestroyContext(h.b);
      CGLDestroyContext(h.a);
      zk_info("handoff took %.1f s", zk_now() - start);
   }
   zk_finish();
}

/* ---- destroy ----------------------------------------------------------------------------------------------------- */

/* Renders a frame in the current context and checks it; used by every destroy order. */
static bool zk_render_check(const char *name, unsigned seed)
{
   zk_gl g;
   bool ok = false;
   if (zk_same_renderer(name) && zk_gl_setup(&g, 150, name) && zk_gl_fbo(&g, 32, 32, name)) {
      unsigned char clear[4] = {(unsigned char)(seed * 40), 90, (unsigned char)(200 - seed * 30), 255};
      unsigned char tri[4] = {250, (unsigned char)(seed * 50), 30, 255};
      zk_clear(clear);
      zk_gl_lower_left(&g, tri);
      char why[200];
      ok = zk_pixel_ok(6, 6, tri, 0, why, sizeof(why)) && zk_pixel_ok(26, 26, clear, 0, why, sizeof(why));
      GLenum e = zk_gl_error();
      if (ok && !e) zk_pass(name, "renders and reads back");
      else if (!ok) zk_fail(name, "%s", why);
      else zk_fail(name, "glGetError got=0x%x expected=0", e);
      ok = ok && !e;
      zk_gl_free(&g);
   }
   return ok;
}

typedef struct {
   CGLContextObj a, b;
   zk_event released, destroyed;
} zk_destroy_a;

static void *zk_destroy_a_t1(void *p)
{
   zk_destroy_a *d = p;
   if (CGLSetCurrentContext(d->a)) return NULL;
   zk_render_check("a-first", 1);
   glFinish();
   CGLSetCurrentContext(NULL); /* no further GL call: A stays Mesa-current on this thread (zink/nvk_zink.c:252-258) */
   zk_event_set(&d->released, 1);
   if (!zk_event_wait(&d->destroyed, 1, 10.0)) {
      zk_fail("a-handoff", "the other thread did not destroy A within 10 s");
      return NULL;
   }
   if (CGLSetCurrentContext(d->b)) return NULL;
   zk_render_check("a-after", 2);
   CGLSetCurrentContext(NULL);
   CGLDestroyContext(d->b);
   return NULL;
}

static void *zk_destroy_a_t2(void *p)
{
   zk_destroy_a *d = p;
   if (!zk_event_wait(&d->released, 1, 10.0)) return NULL;
   CGLDestroyContext(d->a);
   zk_event_set(&d->destroyed, 1);
   return NULL;
}

static void *zk_destroy_c_t1(void *p)
{
   CGLContextObj a = p;
   if (CGLSetCurrentContext(a)) return NULL;
   zk_render_check("c-thread", 3);
   glFinish();
   return NULL; /* exits with A current */
}

static void sub_destroy(char which)
{
   CGLPixelFormatObj pf = zk_pixel_format(ZK_GL4, false, true);
   zk_gate_on_main(pf);
   if (which == 'a') {
      zk_destroy_a d = {.released = ZK_EVENT_INIT, .destroyed = ZK_EVENT_INIT};
      d.a = zk_context(pf, NULL);
      d.b = zk_context(pf, NULL);
      pthread_t t1, t2;
      if (pthread_create(&t1, NULL, zk_destroy_a_t1, &d) || pthread_create(&t2, NULL, zk_destroy_a_t2, &d))
         zk_setup_error("pthread_create failed");
      pthread_join(t1, NULL);
      pthread_join(t2, NULL);
   } else if (which == 'b') {
      CGLContextObj a = zk_context(pf, NULL);
      zk_make_current(a);
      zk_render_check("b-first", 4);
      CGLDestroyContext(a); /* while current */
      CGLContextObj now = CGLGetCurrentContext();
      zk_info("after destroying the current context CGLGetCurrentContext is %s", now ? "not NULL" : "NULL");
      CGLContextObj c = zk_context(pf, NULL);
      zk_make_current(c);
      zk_render_check("b-after", 5);
      CGLSetCurrentContext(NULL);
      CGLDestroyContext(c);
   } else {
      CGLContextObj a = zk_context(pf, NULL);
      pthread_t t1;
      if (pthread_create(&t1, NULL, zk_destroy_c_t1, a)) zk_setup_error("pthread_create failed");
      pthread_join(t1, NULL);
      CGLDestroyContext(a);
      CGLContextObj d = zk_context(pf, NULL);
      zk_make_current(d);
      zk_render_check("c-after", 6);
      CGLSetCurrentContext(NULL);
      CGLDestroyContext(d);
      /* A thread that exited with its context Mesa-current keeps it until process exit (zink/nvk_zink.c:252-285):
       * the process must still end within 5 s of its RESULT line, else SIGALRM ends it. */
      zk_exit_alarm = 5;
   }
   zk_finish();
}

/* ---- migrate ----------------------------------------------------------------------------------------------------- */

/* T1 renders A, releases it without a GL call (A stays Mesa-current on T1), and T2 takes A over. While T2 renders A,
 * T1 binds B, and Mesa's _mesa_make_current flushes T1's previous context, A, on T1 (Mesa 26.3.0
 * src/mesa/main/context.c:1364-1371) while T2 uses it. A crash or a wrong pixel is that race. */
typedef struct {
   CGLContextObj a, b;
   zk_gl ga;
   zk_event released, started;
   int rep, wrong_a, wrong_b, frames_a, frames_b;
} zk_migrate;

static int zk_migrate_frames(zk_gl *g, int count, int salt, zk_event *after_first, int *frames)
{
   int wrong = 0;
   char why[200];
   for (int f = 0; f < count; ++f) {
      unsigned char clear[4], tri[4];
      zk_frame_colour(salt, f, clear, tri);
      glBindFramebuffer(GL_FRAMEBUFFER, g->fbo);
      glViewport(0, 0, g->w, g->h);
      zk_clear(clear);
      zk_gl_lower_left(g, tri);
      if (f % 10 == 9) {
         if ((!zk_pixel_ok(8, 8, tri, 0, why, sizeof(why)) || !zk_pixel_ok(56, 56, clear, 0, why, sizeof(why))) &&
             !wrong++)
            zk_fail(salt == 0 ? "migrate-a" : "migrate-b", "frame %d: %s", f, why);
      } else {
         glFlush();
      }
      ++*frames;
      if (!f && after_first) zk_event_set(after_first, 1);
   }
   GLenum e = zk_gl_error();
   if (e) {
      zk_fail(salt == 0 ? "migrate-a" : "migrate-b", "glGetError got=0x%x expected=0", e);
      ++wrong;
   }
   return wrong;
}

static void *zk_migrate_t1(void *p)
{
   zk_migrate *m = p;
   if (CGLSetCurrentContext(m->a)) return NULL;
   if (!zk_same_renderer("migrate-a") || !zk_gl_setup(&m->ga, 150, "migrate-a") ||
       !zk_gl_fbo(&m->ga, 64, 64, "migrate-a")) {
      zk_event_set(&m->released, -1);
      return NULL;
   }
   const unsigned char grey[4] = {128, 128, 128, 255};
   zk_clear(grey);
   glFinish();
   CGLSetCurrentContext(NULL);
   zk_event_set(&m->released, 1);
   if (!zk_event_wait(&m->started, 1, 10.0)) return NULL;
   if (CGLSetCurrentContext(m->b)) return NULL;
   zk_gl gb;
   if (zk_same_renderer("migrate-b") && zk_gl_setup(&gb, 150, "migrate-b") && zk_gl_fbo(&gb, 64, 64, "migrate-b")) {
      m->wrong_b = zk_migrate_frames(&gb, 300, 1, NULL, &m->frames_b);
      zk_gl_free(&gb);
   } else ++m->wrong_b;
   CGLSetCurrentContext(NULL);
   return NULL;
}

static void *zk_migrate_t2(void *p)
{
   zk_migrate *m = p;
   if (!zk_event_wait(&m->released, 1, 10.0)) {
      zk_event_set(&m->started, 1);
      return NULL;
   }
   if (CGLSetCurrentContext(m->a)) {
      zk_event_set(&m->started, 1);
      return NULL;
   }
   m->wrong_a = zk_migrate_frames(&m->ga, 300, 0, &m->started, &m->frames_a);
   zk_gl_free(&m->ga);
   CGLSetCurrentContext(NULL);
   return NULL;
}

static void sub_migrate(void)
{
   CGLPixelFormatObj pf = zk_pixel_format(ZK_GL4, false, true);
   zk_gate_on_main(pf);
   for (int rep = 0; rep < 3; ++rep) {
      zk_migrate m = {.released = ZK_EVENT_INIT, .started = ZK_EVENT_INIT, .rep = rep};
      m.a = zk_context(pf, NULL);
      m.b = zk_context(pf, NULL);
      double start = zk_now();
      pthread_t t1, t2;
      if (pthread_create(&t1, NULL, zk_migrate_t1, &m) || pthread_create(&t2, NULL, zk_migrate_t2, &m))
         zk_setup_error("pthread_create failed");
      pthread_join(t1, NULL);
      pthread_join(t2, NULL);
      char name[16];
      snprintf(name, sizeof(name), "run-%d", rep + 1);
      if (!m.wrong_a && !m.wrong_b && m.frames_a == 300 && m.frames_b == 300)
         zk_pass(name, "A moved to a second thread and B on the first: 300 frames each, every readback right, %.1f s",
                 zk_now() - start);
      else zk_fail(name, "frames A %d, B %d of 300; wrong readbacks or errors A %d, B %d", m.frames_a, m.frames_b,
                   m.wrong_a, m.wrong_b);
      CGLDestroyContext(m.a);
      CGLDestroyContext(m.b);
   }
   zk_finish();
}

/* ---- main -------------------------------------------------------------------------------------------------------- */

static _Noreturn void zk_usage(void)
{
   zk_setup_error("usage: nvmtltest path|rinfo|offscreen|legacy|iosurface|migrate | profile legacy|gl3|gl4 db|sb "
                  "alpha|noalpha | share a|b|c|d | threads a|b | destroy a|b|c  [--renderer software] [--map PATH]");
}

int main(int argc, char **argv)
{
   zk_tee_start();
   const char *sub = argc > 1 ? argv[1] : "";
   unsigned limit = 20;
   if (!strcmp(sub, "threads") || !strcmp(sub, "iosurface")) limit = 30;
   if (!strcmp(sub, "migrate")) limit = 45;
   zk_install_handlers(limit);
   const char *words[4] = {NULL, NULL, NULL, NULL};
   int nwords = 0;
   for (int i = 2; i < argc; ++i) {
      if (!strcmp(argv[i], "--renderer") && i + 1 < argc) {
         if (strcmp(argv[++i], "software")) zk_usage();
         zk_software = true;
      } else if (!strcmp(argv[i], "--map") && i + 1 < argc) {
         zk_map_path = argv[++i];
      } else if (nwords < 4) {
         words[nwords++] = argv[i];
      } else {
         zk_usage();
      }
   }
   @autoreleasepool {
      zk_require_device();
      zk_read_map();
      if (!strcmp(sub, "path") && !nwords) sub_path();
      if (!strcmp(sub, "rinfo") && !nwords) sub_rinfo();
      if (!strcmp(sub, "offscreen") && !nwords) sub_offscreen();
      if (!strcmp(sub, "legacy") && !nwords) sub_legacy();
      if (!strcmp(sub, "iosurface") && !nwords) sub_iosurface();
      if (!strcmp(sub, "migrate") && !nwords) sub_migrate();
      if (!strcmp(sub, "profile") && nwords == 3) {
         int p = !strcmp(words[0], "legacy") ? ZK_LEGACY : !strcmp(words[0], "gl3") ? ZK_GL3
               : !strcmp(words[0], "gl4")    ? ZK_GL4 : -1;
         bool db = !strcmp(words[1], "db"), alpha = !strcmp(words[2], "alpha");
         if (p < 0 || (!db && strcmp(words[1], "sb")) || (!alpha && strcmp(words[2], "noalpha"))) zk_usage();
         sub_profile(p, db, alpha);
      }
      if (nwords == 1 && strlen(words[0]) == 1) {
         char w = words[0][0];
         if (!strcmp(sub, "share") && w >= 'a' && w <= 'd') sub_share(w);
         if (!strcmp(sub, "threads") && (w == 'a' || w == 'b')) sub_threads(w);
         if (!strcmp(sub, "destroy") && w >= 'a' && w <= 'c') sub_destroy(w);
      }
   }
   zk_usage();
}
