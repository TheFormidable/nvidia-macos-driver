#!/usr/bin/env python3
"""The plugin's calls into the kernel surface fence (plugin/nvmtl_vk.c, the nvmtl_vk_fence_* functions and the routing
in nvmtl_vk_surface_gpu_mark), run on the host against a stand-in for NVK's nvmtl_nvk_fence_v1.

The functions are cut out of nvmtl_vk.c as they are and compiled with a few stand-ins (the device, the clock, the log,
the program name), so what runs here is the production text. What is proven:
  - no NVK entry, or a fence that answers off: nothing is opened, nothing is skipped, the older marks all go out;
  - a fence that is off is asked again at most once a second, by one caller;
  - a compositor hold needs NVAccel to read the kernel's table, an application hold does not;
  - a hold of 16 surfaces goes out as two OPENs of 8 with one cookie and the planes of each surface (none for the
    compositor); a refusal of the second gives back what the first took (one CLOSE), a refusal of the first needs none;
  - after any refusal the thread's record is empty, so every busy tag and every pending mark goes out as before;
  - ARM names only the surfaces whose last writer is the part, with that part's fence; CLOSE of everything happens once;
  - a reader's wait passes the surface, the plane bit, the writer's pid and the 2 s budget, and only the answers that
    settle the question end it; WindowServer never waits for compositor holds;
  - surface 0 always reaches the kernel; a pending mark the open covers is not sent and its clear is not either; a
    mark the kernel took is cleared, a refused one is not;
  - an answer of off from any request switches the fence off until the next probe.
Negative controls: the same program built from a copy with one line of the production text changed must fail (the
thread's record not cleared before a refusal; the partial refusal's CLOSE left out; a covered pending mark sent; a
refused mark remembered as taken).
"""
import os, pathlib, re, subprocess, sys, tempfile

root = pathlib.Path(__file__).resolve().parents[1]
src = (root / 'plugin/nvmtl_vk.c').read_text()

def cut(text, start, end_marker='\n}\n'):
    a = text.index(start)
    b = text.index(end_marker, a) + len(end_marker)
    return text[a:b]

typedef = re.search(r'^typedef uint32_t nvmtl_nvk_fence_v1_fn\(.*?\);\n', src, re.M)
assert typedef, 'nvmtl_vk.c: the typedef of NVK\'s fence entry is missing'
proto = 'VkDevice device, uint32_t op, uint32_t flags, uint64_t cookie, uint32_t count, const uint32_t *ids, const uint8_t *planes, VkFence fence, uint32_t pid, uint32_t timeout_ms, uint32_t out[6]'
assert '(' + proto + ');' in typedef.group(0), 'the plugin\'s prototype of nvmtl_nvk_fence_v1 is not the contract\'s, word for word'
assert src.count('dlsym(g_lib, "nvmtl_nvk_fence_v1")') == 1, 'nvmtl_nvk_fence_v1 is looked up exactly once'

block_start = src.index('/* ---- the kernel surface fence (nvmtl_fence.h) ----')
block = src[block_start:src.index('\n}\n', src.index('uint32_t nvmtl_vk_surface_gpu_mark(uint32_t surfaceID, int pending)')) + 3]
arm = cut(src, 'void nvmtl_vk_fence_arm(const nvmtl_fence_hold *h, uint32_t part, const nvmtl_inflight *f)')

prelude = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include "nvmtl_fence.h"
typedef struct VkDevice_T *VkDevice;
typedef struct VkFence_T *VkFence;
#define VK_NULL_HANDLE NULL
static uint64_t fake_now = 5000000000ull;
#ifndef CLOCK_UPTIME_RAW   /* a Mac's <time.h> (through <pthread.h>) has it; the stand-in clock ignores it */
#define CLOCK_UPTIME_RAW 0
#endif
#define clock_gettime_nsec_np(c) ((void)(c), fake_now)
static const char *fake_name = "app";
#define getprogname() (fake_name)
static unsigned logs; static char lastlog[256];
static void nvlog(const char *f, ...) { logs++; snprintf(lastlog, sizeof lastlog, "%s", f); }
static VkDevice g_dev;
static uint32_t (*pnvk_surface_dirty)(VkDevice, uint32_t, uint32_t);
struct nvmtl_inflight { void *c; VkFence fence; };
typedef struct nvmtl_inflight nvmtl_inflight;
'''

harness = r'''
/* ---- the stand-in NVK: every request logged, the answers scripted ---- */
typedef struct { uint32_t op, flags, count, ids[8], pid, timeout; uint64_t cookie; uint8_t planes[8]; int planesNull; VkFence fence; } call;
static call calls[256]; static unsigned ncalls;
static uint32_t q_status = 0, q_active = 1, q_accel = 1;      /* QUERY */
static uint32_t open_answer[4]; static unsigned nopen;        /* OPEN, one per request in order; past the end OK */
static uint32_t other_status;                                  /* ARM, CLOSE, WAIT */
static uint32_t wait_result = NVMTL_FENCE_R_READY;
static uint32_t nvk(VkDevice d, uint32_t op, uint32_t flags, uint64_t cookie, uint32_t count, const uint32_t *ids,
                    const uint8_t *planes, VkFence fence, uint32_t pid, uint32_t timeout_ms, uint32_t out[6])
{
    (void)d;
    for (int i = 0; i < 6; i++) out[i] = 0;
    call *c = &calls[ncalls++ & 255]; memset(c, 0, sizeof *c);
    c->op = op; c->flags = flags; c->cookie = cookie; c->count = count; c->fence = fence; c->pid = pid; c->timeout = timeout_ms;
    c->planesNull = planes == NULL;
    for (uint32_t i = 0; i < count && i < 8; i++) { c->ids[i] = ids[i]; if (planes) c->planes[i] = planes[i]; }
    if (op == NVMTL_FENCE_OP_WAIT && planes) c->planes[0] = planes[0];
    if (op == NVMTL_FENCE_OP_QUERY) { if (!q_status) { out[0] = q_active; out[1] = q_accel; } return q_status; }
    if (op == NVMTL_FENCE_OP_OPEN) return nopen < 4 ? open_answer[nopen++] : 0;
    if (op == NVMTL_FENCE_OP_WAIT && !other_status) { out[0] = wait_result; out[1] = 1234; out[2] = pid; }
    return other_status;
}
static uint32_t marks[64][2]; static unsigned nmarks; static uint32_t mark_answer;
static uint32_t surface_dirty(VkDevice d, uint32_t sid, uint32_t plane) { (void)d; marks[nmarks & 63][0] = sid; marks[nmarks & 63][1] = plane; nmarks++; return mark_answer; }
static unsigned count_op(uint32_t op) { unsigned n = 0; for (unsigned i = 0; i < ncalls && i < 256; i++) n += calls[i].op == op; return n; }
static void reset_calls(void) { ncalls = 0; nopen = 0; memset(open_answer, 0, sizeof open_answer); other_status = 0; nmarks = 0; }
static void reset_state(void) { memset(&g_fence_state, 0, sizeof g_fence_state); memset(&g_fence_legacy, 0, sizeof g_fence_legacy); g_fence_said = 0; }
static int fails;
#define T(name, cond) do { if (!(cond)) { printf("FAIL %s\n", name); fails++; } else printf("ok   %s\n", name); } while (0)
static nvmtl_fence_hold hold_of(unsigned n, uint32_t first, uint32_t plane)
{
    nvmtl_fence_hold h; nvmtl_fence_hold_reset(&h);
    for (unsigned i = 0; i < n; i++) nvmtl_fence_hold_note(&h, first + i, plane, i % 3);
    return h;
}
static uint32_t other_thread_word;
static void *other_thread(void *a) { other_thread_word = nvmtl_vk_fence_tag_word(*(uint32_t *)a); return NULL; }

int main(int argc, char **argv)
{
    pnvk_surface_dirty = surface_dirty; g_dev = (VkDevice)1;
    if (argc > 1 && !strcmp(argv[1], "ws")) {
        fake_name = "WindowServer"; pnvk_fence = nvk;
        nvmtl_vk_fence_wait_compositor(7);
        T("WindowServer never waits for compositor holds, and does not even ask", ncalls == 0);
        return fails ? 1 : 0;
    }
    /* 1. an older NVK: no entry */
    pnvk_fence = NULL;
    nvmtl_fence_hold h = hold_of(3, 10, 0);
    nvmtl_vk_fence_open(&h, 0);
    T("older NVK: nothing opened", h.cookie == 0 && ncalls == 0);
    T("older NVK: the busy tag carries no fence word", nvmtl_vk_fence_tag_word(10) == 0);
    T("older NVK: a pending mark goes out", nvmtl_vk_surface_gpu_mark(10, 1) == 0 && nmarks == 1 && marks[0][1] == 0x80000001u);
    T("older NVK: and so does its clear", nvmtl_vk_surface_gpu_mark(10, 0) == 0 && nmarks == 2 && marks[1][1] == 0x80000000u);
    T("older NVK: a wait is not asked (the 2.9 poll runs)", nvmtl_vk_fence_wait(10, 1, 99) == 0 && ncalls == 0);
    pnvk_fence = nvk;

    /* 2. a kext without the fence: QUERY answers off */
    reset_calls(); reset_state(); q_status = NVMTL_FENCE_ST_OFF; logs = 0;
    h = hold_of(3, 10, 0); nvmtl_vk_fence_open(&h, 0);
    T("fence off: one QUERY, no OPEN", count_op(NVMTL_FENCE_OP_QUERY) == 1 && count_op(NVMTL_FENCE_OP_OPEN) == 0 && h.cookie == 0);
    T("fence off: said once", logs == 1 && strstr(lastlog, "off"));
    nvmtl_vk_fence_open(&h, 0); fake_now += 999999999ull; nvmtl_vk_fence_open(&h, 0);
    T("fence off: not asked again within the second", count_op(NVMTL_FENCE_OP_QUERY) == 1);
    fake_now += 1; nvmtl_vk_fence_open(&h, 0);
    T("fence off: asked again after a second, and not logged again", count_op(NVMTL_FENCE_OP_QUERY) == 2 && logs == 1);

    /* 3. the fence on, NVAccel not reading it */
    reset_calls(); q_status = 0; q_active = 1; q_accel = 0; fake_now += 2000000000ull; logs = 0;
    h = hold_of(2, 20, 0); nvmtl_vk_fence_open(&h, 0);
    T("fence found on: logged once", logs == 1 && strstr(lastlog, "kernel"));
    T("application hold opened without NVAccel", count_op(NVMTL_FENCE_OP_OPEN) == 1 && h.cookie != 0);
    nvmtl_fence_hold c = hold_of(2, 30, 0); nvmtl_vk_fence_open(&c, NVMTL_FENCE_F_COMPOSITOR | NVMTL_FENCE_F_AUTO);
    T("compositor hold needs NVAccel: not opened", c.cookie == 0 && count_op(NVMTL_FENCE_OP_OPEN) == 1);
    T("compositor hold: the pending marks go out", nvmtl_vk_surface_gpu_mark(30, 1) == 0 && nmarks == 1);
    nvmtl_vk_surface_gpu_mark(30, 0);
    T("compositor hold: and their clears", nmarks == 2);
    nvmtl_vk_fence_close_all(&h);

    /* 4. sixteen surfaces, all taken */
    reset_calls(); q_accel = 1; fake_now += 2000000000ull;
    h = hold_of(16, 100, 1);
    nvmtl_vk_fence_open(&h, 0);
    T("16 surfaces: two OPENs of 8", count_op(NVMTL_FENCE_OP_OPEN) == 2 && calls[ncalls - 2].count == 8 && calls[ncalls - 1].count == 8);
    T("16 surfaces: one cookie, kept", h.cookie != 0 && calls[ncalls - 2].cookie == h.cookie && calls[ncalls - 1].cookie == h.cookie);
    T("16 surfaces: ids in order", calls[ncalls - 2].ids[0] == 100 && calls[ncalls - 1].ids[7] == 115);
    T("application hold: each surface's plane bit", calls[ncalls - 1].planes[3] == 2 && !calls[ncalls - 1].planesNull);
    T("application hold: no flags", calls[ncalls - 1].flags == 0);
    T("tag word on the opening thread", nvmtl_vk_fence_tag_word(100) == NVMTL_BUSY_KFENCE && nvmtl_vk_fence_tag_word(115) == NVMTL_BUSY_KFENCE);
    T("tag word: not for another surface", nvmtl_vk_fence_tag_word(116) == 0 && nvmtl_vk_fence_tag_word(0) == 0);
    { pthread_t th; uint32_t sid = 100; pthread_create(&th, NULL, other_thread, &sid); pthread_join(th, NULL);
      T("tag word: not on another thread", other_thread_word == 0); }
    uint64_t first = h.cookie;
    nvmtl_fence_hold h2 = hold_of(1, 200, 0); nvmtl_vk_fence_open(&h2, 0);
    T("every open gets a new cookie", h2.cookie != 0 && h2.cookie != first);
    T("the next open replaces the thread's record", nvmtl_vk_fence_tag_word(100) == 0 && nvmtl_vk_fence_tag_word(200) == NVMTL_BUSY_KFENCE);

    /* 5. ARM */
    reset_calls();
    struct nvmtl_inflight f = { NULL, (VkFence)0x55 };
    nvmtl_vk_fence_arm(&h, 1, &f);   /* hold_of: surface i's last part is i % 3 */
    unsigned want = 0; for (unsigned i = 0; i < 16; i++) want += i % 3 == 1;
    T("ARM: only the surfaces whose last writer is the part", count_op(NVMTL_FENCE_OP_ARM) == 1 && calls[0].count == want && calls[0].ids[0] == 101 && calls[0].ids[1] == 104);
    T("ARM: with that part's fence and the cookie", calls[0].fence == (VkFence)0x55 && calls[0].cookie == h.cookie);
    nvmtl_fence_hold all = hold_of(16, 300, 0); for (unsigned i = 0; i < 16; i++) all.last[i] = 4;
    open_answer[0] = 0; nvmtl_vk_fence_open(&all, 0); reset_calls();
    nvmtl_vk_fence_arm(&all, 4, &f);
    T("ARM: 16 surfaces go in two requests", count_op(NVMTL_FENCE_OP_ARM) == 2 && calls[0].count == 8 && calls[1].count == 8);
    reset_calls(); struct nvmtl_inflight nof = { NULL, VK_NULL_HANDLE };
    nvmtl_vk_fence_arm(&all, 4, &nof); nvmtl_vk_fence_arm(&all, 4, NULL); nvmtl_vk_fence_arm(&all, 9, &f);
    T("ARM: nothing without a fence, or for a part that is no surface's last", ncalls == 0);

    /* 6. CLOSE */
    reset_calls();
    nvmtl_vk_fence_close(h.cookie, 101);
    T("CLOSE of one surface", ncalls == 1 && calls[0].op == NVMTL_FENCE_OP_CLOSE && calls[0].count == 1 && calls[0].ids[0] == 101);
    nvmtl_vk_fence_close(0, 101); nvmtl_vk_fence_close(h.cookie, 0);
    T("CLOSE: nothing for no cookie or surface 0", ncalls == 1);
    nvmtl_vk_fence_close_all(&h);
    T("CLOSE of all: one request, count 0", ncalls == 2 && calls[1].op == NVMTL_FENCE_OP_CLOSE && calls[1].count == 0 && calls[1].cookie == first && h.cookie == 0);
    nvmtl_vk_fence_close_all(&h); nvmtl_vk_fence_arm(&h, 1, &f);
    T("after CLOSE of all: nothing more is sent for it", ncalls == 2);
    nvmtl_vk_fence_close_all(&h2); nvmtl_vk_fence_close_all(&all);

    /* 7. refusals */
    reset_calls(); h = hold_of(16, 400, 0); open_answer[0] = 0; open_answer[1] = 0x80000004u;   /* FULL */
    nvmtl_vk_fence_open(&h, 0);
    T("second chunk refused: the first chunk is given back", count_op(NVMTL_FENCE_OP_CLOSE) == 1 && calls[ncalls - 1].count == 0 && calls[ncalls - 1].cookie == calls[0].cookie);
    T("second chunk refused: no cookie, nothing on the thread", h.cookie == 0 && nvmtl_vk_fence_tag_word(400) == 0);
    nmarks = 0;
    T("refused: the pending mark goes out", nvmtl_vk_surface_gpu_mark(400, 1) == 0 && nmarks == 1);
    reset_calls(); h = hold_of(3, 500, 0); open_answer[0] = 0x80000007u;   /* MAP: NVK answers it without a call */
    nvmtl_vk_fence_open(&h, 0);
    T("first chunk refused (MAP): no CLOSE", count_op(NVMTL_FENCE_OP_CLOSE) == 0 && h.cookie == 0);
    T("first chunk refused: the fence stays on for the next command buffer", nvmtl_fence_usable(&g_fence_state, 0));
    reset_calls(); h = hold_of(2, 1000, 0); nvmtl_vk_fence_open(&h, 0);
    T("a surface held by the last command buffer is on the thread", h.cookie != 0 && nvmtl_vk_fence_tag_word(1000) == NVMTL_BUSY_KFENCE);
    nvmtl_vk_fence_close_all(&h);
    nvmtl_fence_hold again = hold_of(2, 1000, 0); open_answer[nopen] = 0x80000004u;
    nvmtl_vk_fence_open(&again, 0);
    T("the next one writes it and is refused: its tag carries no fence word", again.cookie == 0 && nvmtl_vk_fence_tag_word(1000) == 0);
    nmarks = 0;
    T("... and its pending mark goes out", nvmtl_vk_surface_gpu_mark(1000, 1) == 0 && nmarks == 1);
    nvmtl_vk_surface_gpu_mark(1000, 0);
    reset_calls(); h = hold_of(3, 520, 0); open_answer[0] = NVMTL_FENCE_ST_OFF; logs = 0;
    nvmtl_vk_fence_open(&h, 0);
    T("OPEN answered off: the fence is off", !nvmtl_fence_usable(&g_fence_state, 0) && h.cookie == 0 && logs == 1);
    reset_calls(); h = hold_of(3, 540, 0); nvmtl_vk_fence_open(&h, 0);
    T("off after an answer: not asked again within the second", ncalls == 0 && h.cookie == 0);
    fake_now += 1000000000ull; nvmtl_vk_fence_open(&h, 0);
    T("off after an answer: a probe a second later turns it back on", count_op(NVMTL_FENCE_OP_QUERY) == 1 && count_op(NVMTL_FENCE_OP_OPEN) == 1 && h.cookie != 0);
    nvmtl_vk_fence_close_all(&h);
    reset_calls(); nvmtl_fence_hold big = hold_of(17, 600, 0); nvmtl_vk_fence_open(&big, 0);
    T("more than 16 surfaces: never opened", ncalls == 0 && big.cookie == 0 && nvmtl_vk_fence_tag_word(600) == 0);
    nvmtl_fence_hold none = hold_of(0, 0, 0); nvmtl_vk_fence_open(&none, 0);
    T("no surface: no request", ncalls == 0 && none.cookie == 0);

    /* 8. compositor holds */
    reset_calls(); c = hold_of(10, 700, 2);
    nvmtl_vk_fence_open(&c, NVMTL_FENCE_F_COMPOSITOR | NVMTL_FENCE_F_AUTO);
    T("compositor hold: flags passed", count_op(NVMTL_FENCE_OP_OPEN) == 2 && calls[0].flags == (NVMTL_FENCE_F_COMPOSITOR | NVMTL_FENCE_F_AUTO));
    T("compositor hold: no plane masks", calls[0].planes[0] == 0 && calls[1].planes[1] == 0);
    nmarks = 0; mark_answer = 0;
    T("covered pending mark: not sent, answered as taken", nvmtl_vk_surface_gpu_mark(700, 1) == 0 && nmarks == 0);
    T("probe: surface 0 always reaches the kernel", (nvmtl_vk_surface_gpu_mark(0, 0), nmarks == 1 && marks[0][0] == 0 && marks[0][1] == 0x80000000u));
    nvmtl_vk_surface_gpu_mark(700, 0);
    T("covered mark's clear: not sent", nmarks == 1);
    T("uncovered pending mark: sent", (nvmtl_vk_surface_gpu_mark(800, 1), nmarks == 2 && marks[1][0] == 800));
    nvmtl_vk_surface_gpu_mark(800, 0);
    T("its clear: sent", nmarks == 3 && marks[2][0] == 800 && marks[2][1] == 0x80000000u);
    nvmtl_vk_surface_gpu_mark(800, 0);
    T("a second clear: not sent", nmarks == 3);
    mark_answer = 0x80000002u; nvmtl_vk_surface_gpu_mark(801, 1); nvmtl_vk_surface_gpu_mark(801, 0);
    T("a refused pending mark: no clear", nmarks == 4);
    mark_answer = 0; nvmtl_vk_fence_close_all(&c);

    /* 9. readers */
    reset_calls(); wait_result = NVMTL_FENCE_R_READY;
    T("wait: READY ends it", nvmtl_vk_fence_wait(900, 4, 321) == 1);
    T("wait: surface, plane bit, writer's pid, 2 s budget, no flags", calls[0].op == NVMTL_FENCE_OP_WAIT && calls[0].ids[0] == 900 &&
      calls[0].planes[0] == 4 && calls[0].pid == 321 && calls[0].timeout == NVMTL_FENCE_WAIT_MS && calls[0].flags == 0);
    wait_result = NVMTL_FENCE_R_NONE; T("wait: NONE ends it", nvmtl_vk_fence_wait(900, 1, 321) == 1);
    wait_result = NVMTL_FENCE_R_TIMEOUT; logs = 0; T("wait: TIMEOUT ends it, logged", nvmtl_vk_fence_wait(900, 1, 321) == 1 && logs == 1);
    wait_result = NVMTL_FENCE_R_ABORTED; T("wait: ABORTED falls through to the poll", nvmtl_vk_fence_wait(900, 1, 321) == 0);
    wait_result = NVMTL_FENCE_R_PENDING; T("wait: PENDING falls through", nvmtl_vk_fence_wait(900, 1, 321) == 0);
    other_status = 0x80000003u; T("wait: an error falls through", nvmtl_vk_fence_wait(900, 1, 321) == 0);
    T("wait: surface 0 is not asked", (reset_calls(), nvmtl_vk_fence_wait(0, 1, 321) == 0 && ncalls == 0));
    reset_calls(); nvmtl_vk_fence_wait_compositor(910);
    T("compositor wait: flag, any plane, no pid", ncalls == 1 && calls[0].flags == NVMTL_FENCE_F_COMPOSITOR && calls[0].planesNull && calls[0].pid == 0 && calls[0].ids[0] == 910);
    reset_calls(); other_status = NVMTL_FENCE_ST_OFF;
    T("wait answered off: falls through", nvmtl_vk_fence_wait(900, 1, 321) == 0);
    reset_calls(); other_status = 0;
    T("wait answered off: the fence is off, nothing is asked within the second", nvmtl_vk_fence_wait(900, 1, 321) == 0 && ncalls == 0);
    nvmtl_vk_fence_wait_compositor(910);
    T("compositor wait while off: nothing asked", ncalls == 0);

    printf(fails ? "\n%d FAILED\n" : "\nall passed\n", fails);
    return fails ? 1 : 0;
}
'''

def build_and_run(block_text, arm_text, d, name):
    p = pathlib.Path(d)
    (p / (name + '.c')).write_text(prelude + typedef.group(0) + 'static nvmtl_nvk_fence_v1_fn *pnvk_fence;\n' + block_text + arm_text + harness)
    cc = os.environ.get('CC', 'cc')
    r = subprocess.run([cc, '-std=gnu11', '-O1', '-pthread', '-Wall', '-Wextra', '-Werror', '-I', str(root / 'plugin'),
                        str(p / (name + '.c')), '-o', str(p / name)], capture_output=True, text=True)
    if r.returncode:
        return None, r.stderr
    out = subprocess.run([str(p / name)], capture_output=True, text=True)
    ws = subprocess.run([str(p / name), 'ws'], capture_output=True, text=True)
    return (out.returncode, ws.returncode), out.stdout + ws.stdout

with tempfile.TemporaryDirectory() as d:
    rc, text = build_and_run(block, arm, d, 'fence_calls')
    if rc is None:
        sys.exit('build failed:\n' + text)
    print(text, end='')
    if rc != (0, 0):
        sys.exit('FAIL: the production fence calls')
    # each control replaces the production line that starts with the anchor by the text given
    controls = [
        ('the thread\'s record is not cleared first', 't_fence.n = 0;   /* first and always', ''),
        ('a partial refusal does not give back the first chunk', 'if (opened) (void)nvmtl_vk_fence_call(NVMTL_FENCE_OP_CLOSE', '(void)opened;'),
        ('a pending mark the kernel hold covers is sent anyway', 'if (!send) return 0;', '(void)send;'),
        ('a clear is sent for a mark the kernel refused', 'nvmtl_fence_mark_sent(&g_fence_legacy, surfaceID, st);', 'nvmtl_fence_mark_sent(&g_fence_legacy, surfaceID, 0);'),
    ]
    for what, line, replacement in controls:
        assert block.count(line) == 1, 'control anchor not found once: ' + line
        a = block.index(line); b = block.index('\n', a)
        mutated = block[:a] + replacement + block[b:]
        rc, out = build_and_run(mutated, arm, d, 'control')
        if rc is None:
            sys.exit('control did not build (' + what + '):\n' + out)
        if rc == (0, 0):
            sys.exit('FAIL: negative control passed: ' + what)
        print('ok   negative control fails: ' + what)
print('PASS')
