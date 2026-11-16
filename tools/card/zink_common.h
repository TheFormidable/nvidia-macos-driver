/* Shared parts of the native OpenGL checks of the card self-test. zink_gl_check.m (offscreen CGL contexts) and
 * zink_win_check.m (AppKit windows) each include this file once; tools/card/checks_zink.py builds and runs them.
 *
 * It holds the conventions of the programs in tools/card: exit status 0 pass, 1 a check failed, 2 setup error (the
 * program could not build its own objects), 3 skip (first stdout line "SKIP: <why>"), 4 inactive (the adapter's
 * layout guard kept the context Apple's: first stdout line "INACTIVE: <why>"); one stdout line per case
 * ("PASS <case> ...", "FAIL <case>: ...", "INFO ...") and a last line "RESULT pass|fail|skip|inactive <counts>"; the
 * program's own alarm; handlers that turn a crash into "CRASH <signal>" and exit status 70, so that no crash report is
 * written; and a copy of standard error kept in memory, so that the program can read the driver's own lines and tell
 * why a hardware context stayed Apple's. Nothing here writes a file. The programs that include it have run once, on one
 * Mac, on macOS 26.7.3 (25G342) with release 3.0.1's package. */
#ifndef ZINK_COMMON_H
#define ZINK_COMMON_H

#define GL_SILENCE_DEPRECATION 1
/* The core and the legacy prototypes together, as zink/nvk_cgl.c:5-7 includes them. */
#define GL_DO_NOT_WARN_IF_MULTI_GL_VERSION_HEADERS_INCLUDED 1
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <CoreGraphics/CoreGraphics.h>
#include <OpenGL/OpenGL.h>
#include <OpenGL/gl3.h>
#include <OpenGL/gl.h>
#include <OpenGL/CGLContext.h>
#include <dlfcn.h>
#include <errno.h>
#include <execinfo.h>
#include <fcntl.h>
#include <limits.h>
#include <math.h>
#include <mach-o/dyld.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/* ---- output ---------------------------------------------------------------------------------------------------- */

static pthread_mutex_t zk_out_lock = PTHREAD_MUTEX_INITIALIZER;
static int zk_passed, zk_failed, zk_known;
/* INFO lines printed before the first PASS or FAIL are held, so that a SKIP or INACTIVE line can still come first. */
static bool zk_opened;
static char zk_held[16384];
static size_t zk_held_len;
/* Seconds the process may take to exit after its RESULT line; past them SIGALRM ends it and the runner reports a
 * time-out. */
static unsigned zk_exit_alarm = 10;

static double zk_now(void)
{
   struct timespec t;
   clock_gettime(CLOCK_MONOTONIC, &t);
   return (double)t.tv_sec + (double)t.tv_nsec / 1e9;
}

static void zk_open_locked(void)
{
   if (zk_opened) return;
   zk_opened = true;
   if (zk_held_len) fputs(zk_held, stdout);
   zk_held_len = 0;
   fflush(stdout);
}

__attribute__((format(printf, 5, 0))) static void
zk_emit(const char *head, const char *name, const char *sep, bool opens, const char *fmt, va_list ap)
{
   char text[2048];
   int n = snprintf(text, sizeof(text), "%s%s%s", head, name ? name : "", sep);
   if (n < 0) n = 0;
   if ((size_t)n >= sizeof(text)) n = (int)sizeof(text) - 1;
   vsnprintf(text + n, sizeof(text) - (size_t)n, fmt, ap);
   size_t len = strlen(text);
   for (size_t i = 0; i < len; ++i)
      if (text[i] == '\n' || text[i] == '\r') text[i] = ' ';
   while (len && text[len - 1] == ' ') --len;
   if (len > sizeof(text) - 2) len = sizeof(text) - 2;
   text[len++] = '\n';
   text[len] = 0;
   pthread_mutex_lock(&zk_out_lock);
   if (opens) zk_open_locked();
   if (zk_opened) {
      fputs(text, stdout);
      fflush(stdout);
   } else if (zk_held_len + len < sizeof(zk_held)) {
      memcpy(zk_held + zk_held_len, text, len + 1);
      zk_held_len += len;
   }
   pthread_mutex_unlock(&zk_out_lock);
}

__attribute__((format(printf, 1, 2))) static void zk_info(const char *fmt, ...)
{
   va_list ap;
   va_start(ap, fmt);
   zk_emit("INFO ", NULL, "", false, fmt, ap);
   va_end(ap);
}

__attribute__((format(printf, 2, 3))) static void zk_pass(const char *name, const char *fmt, ...)
{
   va_list ap;
   va_start(ap, fmt);
   zk_emit("PASS ", name, " ", true, fmt, ap);
   va_end(ap);
   pthread_mutex_lock(&zk_out_lock);
   ++zk_passed;
   pthread_mutex_unlock(&zk_out_lock);
}

__attribute__((format(printf, 2, 3))) static void zk_fail(const char *name, const char *fmt, ...)
{
   va_list ap;
   va_start(ap, fmt);
   zk_emit("FAIL ", name, ": ", true, fmt, ap);
   va_end(ap);
   pthread_mutex_lock(&zk_out_lock);
   ++zk_failed;
   pthread_mutex_unlock(&zk_out_lock);
}

/* A behaviour that differs from Apple's on purpose or by a known limit of the adapter: reported, not failed. */
__attribute__((format(printf, 2, 3), unused)) static void zk_known_difference(const char *name, const char *fmt, ...)
{
   va_list ap;
   va_start(ap, fmt);
   zk_emit("INFO KNOWN-DIFFERENCE ", name, ": ", true, fmt, ap);
   va_end(ap);
   pthread_mutex_lock(&zk_out_lock);
   ++zk_known;
   pthread_mutex_unlock(&zk_out_lock);
}

static void zk_tee_stop(void);
static void zk_tee_sync(void);
static int zk_tee_driver_line(char *out, size_t size);

static _Noreturn void zk_exit(int status)
{
   fflush(stdout);
   zk_tee_stop();
   alarm(zk_exit_alarm);
   exit(status);
}

static _Noreturn void zk_finish(void)
{
   /* Every line the driver writes to standard error in a check is a finding (the runner forbids them too). */
   char line[1024];
   zk_tee_sync();
   if (zk_tee_driver_line(line, sizeof(line))) zk_fail("messages", "the driver wrote: %s", line);
   else zk_pass("messages", "no NVKZinkGLDriver, NVK Zink or Mesa error line on standard error");
   pthread_mutex_lock(&zk_out_lock);
   zk_open_locked();
   int passed = zk_passed, failed = zk_failed, known = zk_known;
   /* "messages" alone is not a check: at least one case of the program's own must have run. */
   bool ok = failed == 0 && passed + known > 1;
   if (known)
      printf("RESULT %s %d passed, %d failed, %d known differences\n", ok ? "pass" : "fail", passed, failed, known);
   else printf("RESULT %s %d passed, %d failed\n", ok ? "pass" : "fail", passed, failed);
   fflush(stdout);
   pthread_mutex_unlock(&zk_out_lock);
   zk_exit(ok ? 0 : 1);
}

static _Noreturn void zk_stop(const char *head, const char *result, int status, const char *why_in)
{
   char why[1024];
   snprintf(why, sizeof(why), "%s", why_in);
   for (char *p = why; *p; ++p)
      if (*p == '\n') *p = ' ';
   pthread_mutex_lock(&zk_out_lock);
   printf("%s: %s\n", head, why);
   if (zk_held_len) fputs(zk_held, stdout);
   zk_held_len = 0;
   zk_opened = true;
   printf("RESULT %s %d passed, %d failed\n", result, zk_passed, zk_failed);
   fflush(stdout);
   pthread_mutex_unlock(&zk_out_lock);
   zk_exit(status);
}

__attribute__((format(printf, 1, 2))) static _Noreturn void zk_skip(const char *fmt, ...)
{
   char why[1024];
   va_list ap;
   va_start(ap, fmt);
   vsnprintf(why, sizeof(why), fmt, ap);
   va_end(ap);
   zk_stop("SKIP", "skip", 3, why);
}

__attribute__((format(printf, 1, 2))) static _Noreturn void zk_inactive(const char *fmt, ...)
{
   char why[1024];
   va_list ap;
   va_start(ap, fmt);
   vsnprintf(why, sizeof(why), fmt, ap);
   va_end(ap);
   zk_stop("INACTIVE", "inactive", 4, why);
}

/* The program could not build its own objects (a pixel format, a context, an IOSurface). */
__attribute__((format(printf, 1, 2))) static _Noreturn void zk_setup_error(const char *fmt, ...)
{
   char why[1024];
   va_list ap;
   va_start(ap, fmt);
   vsnprintf(why, sizeof(why), fmt, ap);
   va_end(ap);
   zk_fail("setup", "%s", why);
   pthread_mutex_lock(&zk_out_lock);
   printf("RESULT fail %d passed, %d failed\n", zk_passed, zk_failed);
   fflush(stdout);
   pthread_mutex_unlock(&zk_out_lock);
   zk_exit(2);
}

/* ---- crash handlers and alarm ------------------------------------------------------------------------------------ */

/* The real standard error. While the copy below runs, fd 2 is a pipe read by a thread that cannot run after a crash,
 * so the handler writes here. */
static int zk_err_fd = STDERR_FILENO;
static volatile sig_atomic_t zk_crashing;

static void zk_write_all(int fd, const char *s)
{
   size_t n = strlen(s);
   while (n) {
      ssize_t w = write(fd, s, n);
      if (w < 0 && errno == EINTR) continue;
      if (w <= 0) return;
      s += w;
      n -= (size_t)w;
   }
}

static void zk_on_crash(int sig)
{
   if (zk_crashing) _exit(70);
   zk_crashing = 1;
   const char *name = sig == SIGSEGV ? "SIGSEGV" : sig == SIGBUS ? "SIGBUS" : sig == SIGILL ? "SIGILL"
                    : sig == SIGFPE ? "SIGFPE" : sig == SIGABRT ? "SIGABRT" : "signal";
   zk_write_all(STDOUT_FILENO, "CRASH ");
   zk_write_all(STDOUT_FILENO, name);
   zk_write_all(STDOUT_FILENO, "\n");
   zk_write_all(zk_err_fd, "CRASH ");
   zk_write_all(zk_err_fd, name);
   zk_write_all(zk_err_fd, "\n");
   void *frames[64];
   int count = backtrace(frames, 64);
   backtrace_symbols_fd(frames, count, zk_err_fd);
   _exit(70);
}

/* seconds: the program's own limit (5 times its expected time, at least 20 s). SIGALRM keeps its default action,
 * which the runner reports as a time-out. */
static void zk_install_handlers(unsigned seconds)
{
   static char stack[1 << 16];
   stack_t alt;
   memset(&alt, 0, sizeof(alt));
   alt.ss_sp = stack;
   alt.ss_size = sizeof(stack);
   sigaltstack(&alt, NULL);
   void *warm[4];
   (void)backtrace(warm, 4); /* binds backtrace's lazy symbols now, outside a signal handler */
   const int sigs[] = {SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT};
   for (size_t i = 0; i < sizeof(sigs) / sizeof(sigs[0]); ++i) {
      struct sigaction sa;
      memset(&sa, 0, sizeof(sa));
      sa.sa_handler = zk_on_crash;
      sa.sa_flags = SA_ONSTACK;
      sigemptyset(&sa.sa_mask);
      sigaction(sigs[i], &sa, NULL);
   }
   alarm(seconds < 20 ? 20 : seconds);
}

/* ---- the copy of standard error -------------------------------------------------------------------------------- */

/* Every byte written to fd 2 goes through a pipe to a thread that writes it on to the real standard error unchanged
 * and keeps the driver's lines: those starting "NVK Zink: " (zink/nvk_cgl.c, nvk_zink.c, native_drawable.m),
 * "NVKZinkGLDriver: " (zink/gld_bridge.c) and "MESA: error: " (Mesa 26.3.0 src/util/log.c:139-141 and :265-268 put the
 * tag and level before every message of mesa_loge, so Zink's own "ZINK: ..." errors start that way). */
#define ZK_LINE_MAX 1024
#define ZK_KEEP_LINES 64
static const char zk_sync_mark[] = "\033zink-card-sync\n"; /* written and swallowed by this program only */
static pthread_mutex_t zk_tee_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t zk_tee_cond = PTHREAD_COND_INITIALIZER;
static char zk_kept[ZK_KEEP_LINES][ZK_LINE_MAX];
static int zk_kept_count;
static unsigned zk_sync_seen;
static bool zk_tee_running, zk_tee_done;
static int zk_tee_fd = -1;

static bool zk_is_driver_line(const char *line)
{
   return !strncmp(line, "NVK Zink: ", 10) || !strncmp(line, "NVKZinkGLDriver: ", 17) ||
          !strncmp(line, "MESA: error: ", 13);
}

static void zk_tee_chunk(const char *chunk, size_t len, bool line_start, bool complete)
{
   if (line_start && complete && len == sizeof(zk_sync_mark) - 1 && !memcmp(chunk, zk_sync_mark, len)) {
      pthread_mutex_lock(&zk_tee_lock);
      ++zk_sync_seen;
      pthread_cond_broadcast(&zk_tee_cond);
      pthread_mutex_unlock(&zk_tee_lock);
      return;
   }
   size_t done = 0;
   while (done < len) {
      ssize_t w = write(zk_err_fd, chunk + done, len - done);
      if (w < 0 && errno == EINTR) continue;
      if (w <= 0) break;
      done += (size_t)w;
   }
   if (!line_start) return;
   char line[ZK_LINE_MAX];
   size_t n = len < sizeof(line) - 1 ? len : sizeof(line) - 1;
   memcpy(line, chunk, n);
   line[n] = 0;
   if (n && line[n - 1] == '\n') line[n - 1] = 0;
   if (!zk_is_driver_line(line)) return;
   pthread_mutex_lock(&zk_tee_lock);
   if (zk_kept_count < ZK_KEEP_LINES) memcpy(zk_kept[zk_kept_count++], line, sizeof(line));
   pthread_mutex_unlock(&zk_tee_lock);
}

static void *zk_tee_main(void *unused)
{
   (void)unused;
   char buf[4096], line[ZK_LINE_MAX];
   size_t len = 0;
   bool start = true;
   for (;;) {
      ssize_t n = read(zk_tee_fd, buf, sizeof(buf));
      if (n < 0 && errno == EINTR) continue;
      if (n <= 0) break;
      for (ssize_t i = 0; i < n; ++i) {
         line[len++] = buf[i];
         if (buf[i] == '\n' || len == sizeof(line)) {
            zk_tee_chunk(line, len, start, buf[i] == '\n');
            start = buf[i] == '\n';
            len = 0;
         }
      }
   }
   if (len) zk_tee_chunk(line, len, start, false);
   pthread_mutex_lock(&zk_tee_lock);
   zk_tee_done = true;
   pthread_cond_broadcast(&zk_tee_cond);
   pthread_mutex_unlock(&zk_tee_lock);
   return NULL;
}

/* Without the copy the program still runs; it only explains less. */
static void zk_tee_start(void)
{
   int fds[2];
   if (pipe(fds)) return;
   int saved = dup(STDERR_FILENO);
   if (saved < 0) { close(fds[0]); close(fds[1]); return; }
   fcntl(saved, F_SETFD, FD_CLOEXEC);
   fcntl(fds[0], F_SETFD, FD_CLOEXEC);
   fflush(stderr);
   if (dup2(fds[1], STDERR_FILENO) < 0) { close(fds[0]); close(fds[1]); close(saved); return; }
   close(fds[1]);
   zk_err_fd = saved;
   zk_tee_fd = fds[0];
   pthread_t thread;
   if (pthread_create(&thread, NULL, zk_tee_main, NULL)) {
      dup2(saved, STDERR_FILENO);
      close(fds[0]);
      zk_tee_fd = -1;
      return;
   }
   pthread_detach(thread);
   zk_tee_running = true;
}

static void zk_tee_wait(bool (*ready)(unsigned), unsigned want, double seconds)
{
   struct timespec until;
   clock_gettime(CLOCK_REALTIME, &until);
   long long ns = (long long)until.tv_nsec + (long long)(seconds * 1e9);
   until.tv_sec += (time_t)(ns / 1000000000LL);
   until.tv_nsec = (long)(ns % 1000000000LL);
   pthread_mutex_lock(&zk_tee_lock);
   while (!ready(want) && !zk_tee_done)
      if (pthread_cond_timedwait(&zk_tee_cond, &zk_tee_lock, &until) == ETIMEDOUT) break;
   pthread_mutex_unlock(&zk_tee_lock);
}

static bool zk_tee_synced(unsigned want) { return zk_sync_seen >= want; }
static bool zk_tee_finished(unsigned want) { (void)want; return zk_tee_done; }

/* Wait until everything written to standard error so far has been read (at most 2 s). */
static void zk_tee_sync(void)
{
   if (!zk_tee_running) return;
   pthread_mutex_lock(&zk_tee_lock);
   unsigned want = zk_sync_seen + 1;
   pthread_mutex_unlock(&zk_tee_lock);
   fflush(stderr);
   zk_write_all(STDERR_FILENO, zk_sync_mark);
   zk_tee_wait(zk_tee_synced, want, 2.0);
}

/* Put the real standard error back; the thread sees the pipe's end and finishes (at most 2 s). */
static void zk_tee_stop(void)
{
   if (!zk_tee_running) return;
   zk_tee_running = false;
   fflush(stderr);
   dup2(zk_err_fd, STDERR_FILENO);
   zk_tee_wait(zk_tee_finished, 0, 2.0);
}

/* The first kept line starting with prefix; 0 when there is none. */
static int zk_tee_find(const char *prefix, char *out, size_t size)
{
   int found = 0;
   pthread_mutex_lock(&zk_tee_lock);
   for (int i = 0; i < zk_kept_count && !found; ++i)
      if (!strncmp(zk_kept[i], prefix, strlen(prefix))) {
         snprintf(out, size, "%s", zk_kept[i]);
         found = 1;
      }
   pthread_mutex_unlock(&zk_tee_lock);
   return found;
}

static int zk_tee_driver_line(char *out, size_t size)
{
   int found = 0;
   pthread_mutex_lock(&zk_tee_lock);
   if (zk_kept_count) {
      snprintf(out, size, "%s", zk_kept[0]);
      found = zk_kept_count;
   }
   pthread_mutex_unlock(&zk_tee_lock);
   return found;
}

/* ---- this process's images and the driver's exports ------------------------------------------------------------ */

static const char ZK_BUNDLE[] = "/NVKZinkGLDriver.bundle/Contents/MacOS/NVKZinkGLDriver";
static const char ZK_CGL[] = "/nvmtl/zink/libnvk_cgl.dylib";
static const char ZK_ZINK[] = "/nvmtl/zink/libnvk_zink.dylib";
static const char ZK_VULKAN[] = "/nvmtl/zink/libnvk_zink_vulkan.dylib";

static bool zk_ends_with(const char *s, const char *suffix)
{
   size_t a = strlen(s), b = strlen(suffix);
   return a >= b && !strcmp(s + a - b, suffix);
}

/* The dyld name of the loaded image whose real path ends with suffix (the bundle loads libnvk_cgl.dylib through
 * @rpath, zink/gld_bridge.c:63, so the name dyld keeps may still hold "../"). */
static const char *zk_image(const char *suffix, char *real_out, size_t size)
{
   uint32_t count = _dyld_image_count();
   for (uint32_t i = 0; i < count; ++i) {
      const char *name = _dyld_get_image_name(i);
      if (!name) continue;
      char real[PATH_MAX];
      const char *path = realpath(name, real) ? real : name;
      if (zk_ends_with(path, suffix)) {
         if (real_out) snprintf(real_out, size, "%s", path);
         return name;
      }
   }
   return NULL;
}

static void *zk_symbol(const char *suffix, const char *symbol)
{
   const char *name = zk_image(suffix, NULL, 0);
   void *handle = name ? dlopen(name, RTLD_NOLOAD | RTLD_LAZY | RTLD_LOCAL) : NULL;
   return handle ? dlsym(handle, symbol) : NULL;
}

/* The real path of the image that holds address, or "(unknown)". */
__attribute__((unused)) static const char *zk_owner(const void *address, char *out, size_t size)
{
   Dl_info info;
   char real[PATH_MAX];
   if (!address || !dladdr(address, &info) || !info.dli_fname) {
      snprintf(out, size, "(unknown)");
      return out;
   }
   snprintf(out, size, "%s", realpath(info.dli_fname, real) ? real : info.dli_fname);
   return out;
}

/* nvk_cgl_last_error() of zink/nvk_cgl.c:121: the calling thread's last adapter failure, or "none". */
static const char *zk_last_error(void)
{
   static const char *(*fn)(void);
   if (!fn) fn = (const char *(*)(void))zk_symbol(ZK_CGL, "nvk_cgl_last_error");
   return fn ? fn() : "(nvk_cgl_last_error not found)";
}

/* ---- the Metal device and the renderer of a context ------------------------------------------------------------ */

static uint64_t zk_device_registry;

/* The conventions of tools/card: the system default device must be this driver's. */
static void zk_require_device(void)
{
   @autoreleasepool {
      id<MTLDevice> device = MTLCreateSystemDefaultDevice();
      NSString *cls = device ? NSStringFromClass([(NSObject *)device class]) : @"none";
      if (![cls isEqualToString:@"NVMTLDevice"]) zk_skip("no device of this driver (%s)", cls.UTF8String);
      zk_device_registry = device.registryID;
      zk_info("Metal device %s", device.name.UTF8String);
   }
}

static bool zk_software;

static const char *zk_string(GLenum name)
{
   const GLubyte *s = glGetString(name);
   return s ? (const char *)s : "(null)";
}

/* zink_set_driver_strings in Mesa 26.3.0 src/gallium/drivers/zink/zink_screen.c:186-197 */
static bool zk_is_zink(const char *renderer)
{
   return renderer && !strncmp(renderer, "zink Vulkan ", 12);
}

static bool zk_is_software(const char *renderer)
{
   return renderer && strstr(renderer, "Apple Software Renderer") != NULL;
}

/* Never returns: a hardware context that is not Zink's. Only the layout guard's refusal makes the check inactive
 * (zink/nvk_cgl.c:125-130, printed once per process, for the first context it refuses): the guard keeps Apple's
 * OpenGL on purpose when GLEngine's private layout is not the recorded one. Every other way a hardware context stays
 * Apple's, while IOGLBundleName names the bundle (the row's need "zink"), is a failure of the installed driver: a
 * screen that could not start (zink/nvk_zink.c:127), a bundle without Apple's renderer or the adapter
 * (zink/gld_bridge.c:36, :48, :70), any other driver line, a bundle that is not loaded at all, or no line that says
 * why. The runner neither reads standard error of an inactive row nor counts the row as a failure, so an inactive
 * verdict for those would hide them. */
static _Noreturn void zk_not_zink(const char *renderer)
{
   char line[ZK_LINE_MAX];
   const char *shown = renderer ? renderer : "(null)";
   zk_tee_sync();
   if (zk_tee_find("NVK Zink: OpenGL stays Apple's; GLEngine's private layout does not match the record (", line,
                   sizeof(line)))
      zk_inactive("%s", line + strlen("NVK Zink: "));
   if (!renderer)
      /* On the Zink path every call returns 0 when the capability table cannot be built (nvk_cgl.c:323-330). */
      zk_fail("zink", "GL_RENDERER is NULL; nvk_cgl_last_error: %s", zk_last_error());
   else if (zk_tee_driver_line(line, sizeof(line)))
      zk_fail("zink", "the context is Apple's (GL_RENDERER %s): %s", shown, line);
   else if (!zk_image(ZK_BUNDLE, NULL, 0))
      zk_fail("zink", "the context is Apple's (GL_RENDERER %s) and NVKZinkGLDriver.bundle is not loaded in this "
              "process, although IOGLBundleName names it", shown);
   else
      zk_fail("zink", "the context is Apple's (GL_RENDERER %s) and the driver printed no reason", shown);
   zk_finish();
}

/* The first context of a check, current on the calling thread: Zink's, or Apple's software renderer for a control.
 * Returns only when it is the one the check is about. */
static void zk_gate(const char *renderer)
{
   if (zk_software) {
      if (!zk_is_software(renderer))
         zk_setup_error("the software control got the renderer %s", renderer ? renderer : "(null)");
      zk_info("control on %s", renderer);
      return;
   }
   if (!zk_is_zink(renderer)) zk_not_zink(renderer);
   zk_info("GL_RENDERER %s", renderer);
}

/* A later context of the same check: the same kind of renderer as the first. */
static bool zk_same_renderer(const char *name)
{
   const char *renderer = (const char *)glGetString(GL_RENDERER);
   bool ok = zk_software ? zk_is_software(renderer) : zk_is_zink(renderer);
   if (!ok)
      zk_fail(name, "this context's GL_RENDERER is %s, the first context's was %s", renderer ? renderer : "(null)",
              zk_software ? "Apple's software renderer" : "Zink's");
   return ok;
}

/* Drain GL errors; the first one is returned. */
static GLenum zk_gl_error(void)
{
   GLenum first = glGetError();
   for (int i = 0; i < 16 && glGetError() != GL_NO_ERROR; ++i) {}
   return first;
}

__attribute__((unused)) static bool zk_near(const unsigned char *got, const unsigned char *want, int tolerance)
{
   for (int i = 0; i < 4; ++i)
      if (abs((int)got[i] - (int)want[i]) > tolerance) return false;
   return true;
}

#endif
