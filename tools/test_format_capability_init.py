#!/usr/bin/env python3
"""nvmtl_vk_format_is_storage on many threads at once, the first time, run from the plugin's own source.

The instance-function list (INSTANCE_FNS), its loader (LOAD_I), the pthread_once guard of nvmtl_vk_init and the query are
lifted from plugin/nvmtl_vk.c and compiled with a stand-in vkGetInstanceProcAddr that takes 150 ms to resolve the format
query, so 24 threads ask while it is being resolved. Every thread must hear that VkFormat 64 is a storage format, the
query must be resolved once, and an unsupported format (58) must stay unsupported. With the query missing, nvmtl_vk_init
fails and every thread hears "not storage". A control compiles the racy form of the query (its own static pointer and a
"tried" flag set before the pointer) and must give at least one thread the wrong answer. No GPU or Vulkan SDK is needed.

The fix is from the HighDelay/nvidia-macos-driver-nvk fork."""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'plugin/nvmtl_vk.c').read_text()


def function(signature):
    start = SOURCE.index(signature)
    return SOURCE[start:SOURCE.index('\n}\n', start) + 3]


MACRO = SOURCE[SOURCE.index('#define INSTANCE_FNS('):SOURCE.index('#define DEVICE_FNS(')]
NAMES = re.findall(r'X\((\w+)\)', MACRO)
if 'vkGetPhysicalDeviceFormatProperties' not in NAMES:
    print('FAIL: vkGetPhysicalDeviceFormatProperties is not one of the instance functions nvmtl_vk_init resolves')
    sys.exit(1)
DECLARATIONS = '\n'.join('typedef void (*PFN_' + name + ')(void);' for name in NAMES if name != 'vkGetPhysicalDeviceFormatProperties')
LOADER = next(line for line in SOURCE.splitlines() if line.startswith('#define LOAD_I('))
GUARD = (next(line for line in SOURCE.splitlines() if line.startswith('static pthread_once_t g_vk_once ='))
         + '\n' + next(line for line in SOURCE.splitlines() if line.startswith('static void nvmtl_vk_initialize_once(void)'))
         + '\n' + function('int nvmtl_vk_init(void) {'))
QUERY = function('int nvmtl_vk_format_is_storage(uint32_t vkfmt)\n{')
BEFORE = r'''
int nvmtl_vk_format_is_storage(uint32_t vkfmt)
{
    static void (*fp)(VkPhysicalDevice, VkFormat, VkFormatProperties *); static int tried;
    if (nvmtl_vk_init()) return 0;
    if (!tried) { tried = 1; fp = (void (*)(VkPhysicalDevice, VkFormat, VkFormatProperties *))pvkGetInstanceProcAddr(g_inst, "vkGetPhysicalDeviceFormatProperties"); }
    if (!fp) return 0;
    VkFormatProperties fpz; memset(&fpz, 0, sizeof fpz); fp(g_pdev, (VkFormat)vkfmt, &fpz);
    return (fpz.optimalTilingFeatures & VK_FORMAT_FEATURE_STORAGE_IMAGE_BIT) != 0;
}
'''
FIXTURE = r'''
#include <assert.h>
#include <pthread.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
typedef void *VkInstance, *VkPhysicalDevice;
typedef uint32_t VkFormat;
typedef struct { uint32_t linearTilingFeatures, optimalTilingFeatures, bufferFeatures; } VkFormatProperties;
#define VK_FORMAT_FEATURE_STORAGE_IMAGE_BIT 2u
typedef void (*PFN_vkVoidFunction)(void);
typedef void (*PFN_vkGetPhysicalDeviceFormatProperties)(VkPhysicalDevice, VkFormat, VkFormatProperties *);
@@DECLARATIONS@@
static VkInstance g_inst = (void *)1;
static VkPhysicalDevice g_pdev = (void *)2;
static _Atomic int g_state;
static _Atomic unsigned resolutions, ready, start_queries, incorrect;
static int missing, late;   /* late: the control's query resolves after init, in the query itself */
static void properties(VkPhysicalDevice device, VkFormat format, VkFormatProperties *out) {
    assert(device == g_pdev); memset(out, 0, sizeof *out);
    out->optimalTilingFeatures = format == 64 ? VK_FORMAT_FEATURE_STORAGE_IMAGE_BIT : 0;
}
static void placeholder(void) {}
static PFN_vkVoidFunction pvkGetInstanceProcAddr(VkInstance instance, const char *name) {
    assert(instance == g_inst);
    if (!strcmp(name, "vkGetPhysicalDeviceFormatProperties")) {
        atomic_fetch_add(&resolutions, 1);
        usleep(150000);   /* the other threads ask while the pointer is being resolved */
        return missing ? NULL : (PFN_vkVoidFunction)properties;
    }
    return placeholder;
}
#define nvlog(...) ((void)0)
@@MACRO@@
#define DECL(f) static PFN_##f p##f;
INSTANCE_FNS(DECL)
@@LOADER@@
static int nvmtl_vk_initialize(void) {
    if (late) { g_state = 1; return 0; }
    g_state = -1;
    INSTANCE_FNS(LOAD_I)
    g_state = 1; return 0;
}
@@GUARD@@
@@QUERY@@
static void *worker(void *unused) {
    (void)unused; atomic_fetch_add(&ready, 1);
    while (!atomic_load_explicit(&start_queries, memory_order_acquire)) usleep(100);
    if (nvmtl_vk_format_is_storage(64) != !missing) atomic_fetch_add(&incorrect, 1);
    return NULL;
}
int main(int argc, char **argv) {
    missing = argc > 1 && atoi(argv[1]); late = CONTROL;
    enum { Threads = 24 }; pthread_t threads[Threads];
    for (unsigned i = 0; i < Threads; i++) assert(!pthread_create(&threads[i], NULL, worker, NULL));
    while (atomic_load(&ready) != Threads) usleep(100);
    atomic_store_explicit(&start_queries, 1, memory_order_release);
    for (unsigned i = 0; i < Threads; i++) assert(!pthread_join(threads[i], NULL));
    if (CONTROL) { printf("control: %u of %d threads heard the wrong answer\n", atomic_load(&incorrect), (int)Threads); return atomic_load(&incorrect) ? 3 : 0; }
    assert(!atomic_load(&incorrect));
    assert(atomic_load(&resolutions) == 1);
    assert(nvmtl_vk_format_is_storage(58) == 0);
    printf("PASS: %d threads at first use, query %s, resolved %u time(s), an unsupported format stays unsupported\n",
           (int)Threads, missing ? "missing (init fails, nothing is storage)" : "present", atomic_load(&resolutions));
    return 0;
}
'''


def compiler():
    if shutil.which('xcrun'):
        return ['xcrun', 'clang']
    for cc in ('cc', 'clang', 'gcc'):
        if shutil.which(cc):
            return [cc]
    return None


def build(directory, query, control, name):
    src = pathlib.Path(directory) / (name + '.c')
    exe = pathlib.Path(directory) / name
    src.write_text(FIXTURE.replace('@@DECLARATIONS@@', DECLARATIONS).replace('@@MACRO@@', MACRO).replace('@@LOADER@@', LOADER)
                   .replace('@@GUARD@@', GUARD).replace('@@QUERY@@', query).replace('CONTROL', str(control)))
    r = subprocess.run(compiler() + ['-std=gnu11', '-O1', '-g', '-fsanitize=address,undefined', '-fno-sanitize-recover=all',
                                      '-fno-omit-frame-pointer', '-pthread', str(src), '-o', str(exe)], capture_output=True, text=True)
    if r.returncode:
        print('FAIL: the fixture does not compile\n' + r.stderr[-2500:])
        return None
    return exe


def main():
    if not compiler():
        print('SKIP: no C compiler')
        return 2
    with tempfile.TemporaryDirectory(prefix='nvmtl-format-init-') as d:
        exe = build(d, QUERY, 0, 'current')
        if not exe:
            return 1
        for missing in ('0', '1'):
            p = subprocess.run([str(exe), missing], capture_output=True, text=True, timeout=30)
            print((p.stdout + p.stderr).strip()[-1500:])
            if p.returncode:
                print('FAIL: the query gave a wrong answer with the query ' + ('missing' if missing == '1' else 'present'))
                return 1
        exe = build(d, BEFORE, 1, 'control')
        if not exe:
            return 1
        p = subprocess.run([str(exe), '0'], capture_output=True, text=True, timeout=30)
        print((p.stdout + p.stderr).strip()[-500:])
        if p.returncode != 3:
            print('FAIL: control: the query with its own flag gave no thread a wrong answer')
            return 1
        print('ok   control: the query with its own flag set before its pointer fails')
    return 0


if __name__ == '__main__':
    sys.exit(main())
