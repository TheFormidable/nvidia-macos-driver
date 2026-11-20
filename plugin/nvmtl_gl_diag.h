/* nvmtl_gl_diag.h - the once-per-process lines that say which way two OpenGL decisions went.
   Two decisions of the OpenGL path used to be silent in a release build, where the system log only gets lines that name
   a failure (nvmtl_log_failure.h):
     - whether Apple's OpenGL renderer was told that the render encoder takes setProvokingVertexMode: (NVMTLGL.m,
       nvmtl_gl_caps_patch). Without it the renderer rewrites the index buffer of every indexed draw itself, and the
       patch accepts only the table of one macOS build, so on any other build nothing said that the rewrite stays.
     - which way -[NSCGLSurface synchronize] waits (NVMTLGLVBlank.m, nvglvbl_synchronize): by the display's refresh
       period, or AppKit's own wait.
   Each process says each once, as a notice (nvlog_notice in NVMTLDevice.m). The wording is picked here, and no line
   may hold a word the failure rule forwards, or the line would reach the system log twice. The vblank branch rule
   lives here too, so the line and the wait cannot disagree. Pure, so tools/test_gl_diag.c checks it without a Mac.
   Find the lines on the Mac with:
     log show --last 1d --style compact --predicate 'eventMessage CONTAINS "GL provoking vertex"'
     log show --last 1d --style compact --predicate 'eventMessage CONTAINS "GL vblank wait"' */
#ifndef NVMTL_GL_DIAG_H
#define NVMTL_GL_DIAG_H
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#define NVMTL_GL_CAPS_BYTES 20   /* the size of the renderer's NVIDIA profile table the patch compares */

/* what nvmtl_gl_caps_patch did in a process that loaded Apple's renderer */
typedef enum {
    NVMTL_GL_CAPS_PATCHED = 0,      /* the bit is set: the renderer sends setProvokingVertexMode: */
    NVMTL_GL_CAPS_SET_BY_APPLE,     /* the renderer's own table already has the bit */
    NVMTL_GL_CAPS_DIFFERS,          /* the table is not the one this plugin knows: left alone */
    NVMTL_GL_CAPS_NO_SYMBOL,        /* the table's symbol is not in the renderer's symbol table */
    NVMTL_GL_CAPS_NOT_WRITABLE,     /* vm_protect would not make the table's page writable */
    NVMTL_GL_CAPS_NO_FEATURE,       /* the Vulkan device cannot make the last vertex provoke: nothing to tell */
    NVMTL_GL_CAPS_OUTCOMES
} nvmtl_gl_caps_outcome;

static inline const char *nvmtl_gl_caps_what(nvmtl_gl_caps_outcome o)
{
    switch (o) {
    case NVMTL_GL_CAPS_PATCHED:      return "the renderer sends it per pass (bit set in its NVIDIA profile)";
    case NVMTL_GL_CAPS_SET_BY_APPLE: return "the renderer sends it per pass (its NVIDIA profile has the bit already)";
    case NVMTL_GL_CAPS_DIFFERS:      return "left to the renderer's index rewrite (its NVIDIA profile is not the macOS 15.8.1 one)";
    case NVMTL_GL_CAPS_NO_SYMBOL:    return "left to the renderer's index rewrite (no NVIDIA profile symbol in it)";
    case NVMTL_GL_CAPS_NOT_WRITABLE: return "left to the renderer's index rewrite (its profile page is not writable)";
    case NVMTL_GL_CAPS_NO_FEATURE:   return "not patched, its NVIDIA profile is not read (the Vulkan device has no last-vertex provoking)";
    default:                         return "?";
    }
}

/* The line for an outcome. caps is the table as the renderer had it before any write (NVMTL_GL_CAPS_BYTES bytes), or
   NULL when there is none to show; kr is vm_protect's answer, shown for NVMTL_GL_CAPS_NOT_WRITABLE only. prog and build
   (kern.osversion) may be NULL. The bytes are what a new build's table must be compared with before it is accepted. */
static inline void nvmtl_gl_caps_text(char *out, size_t size, nvmtl_gl_caps_outcome o, const uint8_t *caps, int kr,
                                      const char *prog, const char *build)
{
    char hex[NVMTL_GL_CAPS_BYTES * 3 + 1] = "none";
    if (caps) {
        for (int i = 0; i < NVMTL_GL_CAPS_BYTES; i++) snprintf(hex + 3 * i, sizeof hex - 3 * (size_t)i, "%02x ", caps[i]);
        hex[NVMTL_GL_CAPS_BYTES * 3 - 1] = 0;   /* no blank after the last byte */
    }
    char why[24] = "";
    if (o == NVMTL_GL_CAPS_NOT_WRITABLE) snprintf(why, sizeof why, ", kr %d", kr);
    if (size) snprintf(out, size, "GL provoking vertex: %s in %s on macOS %s (NVIDIA profile %s%s)", nvmtl_gl_caps_what(o),
                       prog ? prog : "?", build && *build ? build : "?", hex, why);
}

/* which way nvglvbl_synchronize waits once the window server still holds the previous frame */
typedef enum {
    NVGLVBL_CORRECTED = 0,   /* by the display's refresh period from the window server's vblank */
    NVGLVBL_FEW_ENTRIES,     /* AppKit's wait: the image queue gave fewer than two VBL entries, or more than asked */
    NVGLVBL_NO_PERIOD,       /* AppKit's wait: the window's display gave no refresh rate */
    NVGLVBL_NOT_LONGER,      /* AppKit's wait: CoreAnimation's spacing is not longer than the display's period */
    NVGLVBL_DECISIONS
} nvglvbl_decision;

/* n entries (asked for max), the display's period, and the first two entries' host times (read only when n >= 2).
   NVGLVBL_CORRECTED exactly when n >= 2 && n <= max && period && t1 > t0 && period < t1 - t0, the rule the wait had
   inline before. */
static inline nvglvbl_decision nvglvbl_decide(uint32_t n, uint32_t max, uint64_t period, uint64_t t0, uint64_t t1)
{
    if (n < 2 || n > max) return NVGLVBL_FEW_ENTRIES;
    if (!period) return NVGLVBL_NO_PERIOD;
    if (!(t1 > t0) || !(period < t1 - t0)) return NVGLVBL_NOT_LONGER;
    return NVGLVBL_CORRECTED;
}

static inline const char *nvglvbl_what(nvglvbl_decision d)
{
    switch (d) {
    case NVGLVBL_CORRECTED:   return "waits by the display's refresh period";
    case NVGLVBL_FEW_ENTRIES: return "left to AppKit (not two to four VBL entries from the image queue)";
    case NVGLVBL_NO_PERIOD:   return "left to AppKit (the window's display gave no refresh rate)";
    case NVGLVBL_NOT_LONGER:  return "left to AppKit (CoreAnimation's spacing is not longer than the display's period)";
    default:                  return "?";
    }
}

/* The line for the first decision of a process (its first synchronize while the window server still holds a frame,
   not its first frame): periods in microseconds (spacing 0 when there were not two entries). */
static inline void nvglvbl_text(char *out, size_t size, nvglvbl_decision d, uint32_t entries, uint64_t period_us,
                                uint64_t spacing_us, const char *prog)
{
    if (size) snprintf(out, size, "GL vblank wait: first wait in %s %s (entries %u, display period %llu us, "
                       "CoreAnimation spacing %llu us)", prog ? prog : "?", nvglvbl_what(d), (unsigned)entries,
                       (unsigned long long)period_us, (unsigned long long)spacing_us);
}
#endif
