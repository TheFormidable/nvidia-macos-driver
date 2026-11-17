// nvmtl_ws_capture.h: the rules of the 3.0.1 switches that take work off WindowServer's main thread while the screen
// is shared (each captured frame cost it about 16 ms in the plugin before 3.0's fast grants). Pure, so
// tools/test_ws_capture.c holds every answer against these rules.
//
// Each switch is on in WindowServer only and has one way back, NAME=0 in the settings file (nvrm610.conf, read once
// per process). In any other process it answers 0 whatever the variable says, so no application changes behaviour.
//   NVMTL_WS_FREE_OFFMAIN  a released texture's import of the family's VRAM is freed on a serial queue of its own.
//   NVMTL_SURFIN_ZERO      a surface whose CPU pages are all zero is not uploaded; its VRAM is zero-filled on the GPU
//                          ahead of the next command buffer (the scan: nvmtl_zero_scan.h).
#ifndef NVMTL_WS_CAPTURE_H
#define NVMTL_WS_CAPTURE_H
#include <stdint.h>
#include <string.h>

// env: the variable's value after the settings file was read (NULL when unset); progname: getprogname().
static inline int nvmtl_ws_capture_switch(const char *env, const char *progname)
{
    if (!progname || strcmp(progname, "WindowServer")) return 0;
    return !(env && env[0] == '0');
}

// NVMTL_SURFIN_ZERO: the rule above, and off as well when the pre-submit prologue is (NVMTL_PRESUBMIT_PROLOGUE=0).
// Without the prologue the pre-submit hook runs outside the submit mutex, so a thread whose hook found the owed set
// already taken by another thread could submit work that reads a surface before that thread's fill reached the queue.
static inline int nvmtl_surfin_zero_switch(const char *env, const char *prologue_env, const char *progname)
{
    return nvmtl_ws_capture_switch(env, progname) && !(prologue_env && prologue_env[0] == '0');
}

// NVMTL_WS_FREE_OFFMAIN: the free of a dead buffer may go to the queue. Only a family VRAM import qualifies (imported,
// no CPU map; the host-pointer import is the other imported kind and keeps the surface's user pages wired, so it is
// freed where it was), never a pooled, placed or bind-lost buffer. pending is the bytes already queued, size this
// buffer's, cap the bound on the queue. 1: queue it; 0: not this kind, free it inline; -1: the right kind but the
// queue already holds cap bytes, free it inline as in 3.0 (counted).
static inline int nvmtl_free_offmain_ok(int on, int imported, int mapped, int pooled, int placed, int bind_lost,
                                        uint64_t pending, uint64_t size, uint64_t cap)
{
    if (!on || !imported || mapped || pooled || placed || bind_lost) return 0;
    if (size > cap || pending > cap - size) return -1;
    return 1;
}
#endif
