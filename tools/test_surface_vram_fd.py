#!/usr/bin/env python3
"""test_surface_vram_fd.py: the plugin's import of a surface's family VRAM gets its descriptor where a sandbox refuses
/dev/null, and says so when it gets none.

The descriptor handed to NVK's import carries nothing (the staged record names the surface), but the import needs one,
and Firefox's sandboxes refuse /dev/null. NVK's own opens already fall back to the read end of an empty pipe there
(plugin/nvmtl_fd_token.h); the import opened /dev/null with a plain open(), failed without a word, and the surface went
through a private image and its copies. The idea is from pull request 487 of the original project (dreamelite96).

Compiles nvmtl_vk_surface_vram_pipe_on and nvmtl_vk_surface_vram_words from plugin/nvmtl_vk.c as they are, with
plugin/nvmtl_fd_token.h, stand-ins for Vulkan and NVK and an open() that refuses /dev/null as the sandbox does, and
checks: a refusal (EPERM, EACCES) still gives an import over a descriptor that reads as empty and is closed on exec;
/dev/null itself where it opens; no import tried and one log line naming the errno when no descriptor can be had (the
pipe fails too); a refused import closes its descriptor; no descriptor is left open by any of them. A second run with
NVMTL_SURFACE_VRAM_PIPE=0, the change's one way back, checks that the import then opens /dev/null with a plain open()
as in 3.0.4 (no import under the sandbox, /dev/null itself where it opens) and that the log says so once; a third run
with NVMTL_SURFACE_VRAM_PIPE=1 checks that only 0 turns the change off.

  python3 tools/test_surface_vram_fd.py [--baseline [REF]]
      --baseline REF   the sources of that revision (default HEAD); there this test fails (the import is not tried
                       under the sandbox and the missing descriptor is not logged)

Needs a C compiler."""
import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument('--baseline', nargs='?', const='HEAD', metavar='REF')
args = parser.parse_args()


def source(path):
    if args.baseline is not None:
        return subprocess.check_output(['git', 'show', f'{args.baseline}:{path}'], cwd=root, text=True)
    return (root / path).read_text()


def function(text, signature):
    start = text.index(signature)
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]


vk = source('plugin/nvmtl_vk.c')
header = source('plugin/nvmtl_vk.h')
token = source('plugin/nvmtl_fd_token.h')
buffer_type = header[header.index('typedef struct { void *buf;'):header.index('} nvk_buffer;') + len('} nvk_buffer;')]
record_type = vk[vk.index('typedef struct { uint32_t version, surfaceID, plane, pad; uint64_t size_B; } nvmtl_vram_rec;'):]
record_type = record_type[:record_type.index('\n')]
under_test = function(vk, 'static int nvmtl_vk_surface_vram_words(')
# The one way back (NVMTL_SURFACE_VRAM_PIPE=0). A tree without it is held to the checks of the default path, which it
# fails, and its escape run finds no line saying the switch was read.
if 'static int nvmtl_vk_surface_vram_pipe_on(' in vk:
    under_test = function(vk, 'static int nvmtl_vk_surface_vram_pipe_on(') + '\n' + under_test

fixture = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
/* Vulkan, as far as the function uses it; the fields in the order of vulkan_core.h */
typedef int32_t VkResult; typedef uint64_t VkDeviceSize; typedef uint32_t VkFlags;
typedef void *VkDevice, *VkDeviceMemory, *VkBuffer;
#define VK_NULL_HANDLE NULL
enum { VK_SUCCESS = 0, VK_ERROR_OUT_OF_DEVICE_MEMORY = -2, VK_ERROR_INVALID_EXTERNAL_HANDLE = -1000072003 };
enum { VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO = 12, VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO = 5,
       VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR = 1000074000 };
enum { VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT = 1, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT = 1,
       VK_BUFFER_USAGE_TRANSFER_SRC_BIT = 1, VK_BUFFER_USAGE_TRANSFER_DST_BIT = 2, VK_SHARING_MODE_EXCLUSIVE = 0 };
typedef VkFlags VkMemoryPropertyFlags;
typedef struct { int sType; const void *pNext; int handleType; int fd; } VkImportMemoryFdInfoKHR;
typedef struct { int sType; const void *pNext; VkDeviceSize allocationSize; uint32_t memoryTypeIndex; } VkMemoryAllocateInfo;
typedef struct { int sType; const void *pNext; VkFlags flags; VkDeviceSize size; VkFlags usage; int sharingMode;
                 uint32_t queueFamilyIndexCount; const uint32_t *pQueueFamilyIndices; } VkBufferCreateInfo;
typedef struct { uint32_t memoryTypeCount; } memory_properties;
/*BUFFER_TYPE*/
/*RECORD_TYPE*/
#define NVMTL_PA_PLANE_RANGED 0x20000000u
static memory_properties g_memp = { 4 };
static VkDevice g_dev = (VkDevice)0x1;
static uint64_t g_buf_made;
static int share_on = 1, refuse_import, staged, imports;
static int nvmtl_vk_surface_share_on(void) { return share_on; }
static int memtype(uint32_t bits, VkMemoryPropertyFlags want) { (void)bits; (void)want; return 1; }

/* the sandbox: /dev/null refused with `denial`, anything else as the system does */
static int denial, pipe_failure;
static int test_open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) { va_list a; va_start(a, flags); mode = (mode_t)va_arg(a, int); va_end(a); }
    if (denial && !strcmp(path, "/dev/null")) { errno = denial; return -1; }
    return openat(AT_FDCWD, path, flags, mode);
}
static int test_pipe(int p[2]) {
    if (pipe_failure) { errno = EMFILE; return -1; }
    return pipe(p);
}
#define open test_open
#define pipe test_pipe
/*TOKEN*/
#undef pipe

static char logged[4096]; static int lines;
void nvlog(const char *fmt, ...) {
    va_list a; va_start(a, fmt);
    size_t n = strlen(logged);
    vsnprintf(logged + n, sizeof logged - n, fmt, a);
    va_end(a);
    n = strlen(logged); if (n + 1 < sizeof logged) { logged[n] = '\n'; logged[n + 1] = 0; }
    lines++;
}
/* NVK: the staged record must be there, the descriptor must be open; a successful import takes it over */
static int seen_fd = -1, seen_fifo, seen_chr, seen_cloexec, seen_eof;
static void stage(const nvmtl_vram_rec *r) { staged = r != NULL; }
static void (*pnvk_stage_import)(const nvmtl_vram_rec *) = stage;
static VkResult pvkAllocateMemory(VkDevice d, const VkMemoryAllocateInfo *i, const void *a, VkDeviceMemory *m) {
    (void)d; (void)a;
    const VkImportMemoryFdInfoKHR *imp = i->pNext;
    assert(staged && imp && imp->sType == VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR);
    imports++;
    struct stat st; assert(fstat(imp->fd, &st) == 0);
    seen_fd = imp->fd; seen_fifo = S_ISFIFO(st.st_mode); seen_chr = S_ISCHR(st.st_mode);
    seen_cloexec = (fcntl(imp->fd, F_GETFD) & FD_CLOEXEC) != 0;
    char c; seen_eof = read(imp->fd, &c, 1) == 0;
    if (refuse_import) return VK_ERROR_INVALID_EXTERNAL_HANDLE;
    close(imp->fd);
    *m = (VkDeviceMemory)0x2;
    return VK_SUCCESS;
}
static VkResult pvkCreateBuffer(VkDevice d, const VkBufferCreateInfo *i, const void *a, VkBuffer *b) {
    (void)d; (void)i; (void)a; *b = (VkBuffer)0x3; return VK_SUCCESS; }
static VkResult pvkBindBufferMemory(VkDevice d, VkBuffer b, VkDeviceMemory m, VkDeviceSize o) {
    (void)d; (void)b; (void)m; (void)o; return VK_SUCCESS; }
static void pvkDestroyBuffer(VkDevice d, VkBuffer b, const void *a) { (void)d; (void)b; (void)a; }
static void pvkFreeMemory(VkDevice d, VkDeviceMemory m, const void *a) { (void)d; (void)m; (void)a; }

/*UNDER_TEST*/

static int descriptors(void) { int n = 0; for (int fd = 0; fd < 512; fd++) if (fcntl(fd, F_GETFD) >= 0) n++; return n; }
static int bad;
#define T(c) do { if (!(c)) { printf("FAIL %s (line %d)\n", #c, __LINE__); bad++; } } while (0)
static void reset(void) { logged[0] = 0; lines = 0; imports = 0; seen_fd = -1; seen_fifo = seen_chr = seen_cloexec = seen_eof = 0; }
/* NVMTL_SURFACE_VRAM_PIPE=0: the descriptor comes from a plain open(), as in 3.0.4 */
static int escape_run(void) {
    const int before = descriptors();
    nvk_buffer out;
    reset(); denial = EPERM;
    T(nvmtl_vk_surface_vram_words(20, 0, 4096, 4096, &out) == -1 && imports == 0);
    T(strstr(logged, "NVMTL_SURFACE_VRAM_PIPE=0") && strstr(logged, "errno 1"));
    const int first = lines;
    reset(); denial = 0;
    T(nvmtl_vk_surface_vram_words(21, 0, 4096, 4096, &out) == 0 && imports == 1 && seen_chr);
    T(lines == 0);                                   /* the switch is read and logged once */
    T(first == 2);
    T(descriptors() == before);
    if (!bad) puts("NVMTL_SURFACE_VRAM_PIPE=0: no import under the sandbox, /dev/null where it opens, as in 3.0.4");
    return bad != 0;
}
int main(int argc, char **argv) {
    if (argc > 1 && !strcmp(argv[1], "escape")) return escape_run();
    const int before = descriptors();
    nvk_buffer out;
    /* the sandbox refuses /dev/null: the import still runs, over an empty pipe that is closed on exec */
    for (int k = 0; k < 2; k++) {
        reset(); denial = k ? EACCES : EPERM;
        int r = nvmtl_vk_surface_vram_words(7, 0, 4096, 4096, &out);
        T(r == 0 && out.imported == 1 && imports == 1);
        T(seen_fifo && seen_cloexec && seen_eof);
        T(lines == 0);
    }
    if (!bad) printf("sandbox: the import ran over a pipe descriptor (%d)\n", seen_fd);
    /* no sandbox: /dev/null itself */
    reset(); denial = 0;
    T(nvmtl_vk_surface_vram_words(8, 1, 4096, 4096, &out) == 0 && imports == 1 && seen_chr);
    /* no descriptor at all: no import, a line that says why, with the errno */
    reset(); denial = EPERM; pipe_failure = 1;
    T(nvmtl_vk_surface_vram_words(9, 0, 4096, 4096, &out) == -1 && imports == 0);
    T(lines == 1 && strstr(logged, "surface 9") && strstr(logged, "no descriptor") && strstr(logged, "errno 24"));
    if (lines) printf("no descriptor: %s", logged);
    /* a ranged grant says so in the line */
    reset();
    T(nvmtl_vk_surface_vram_words(10, 1 | NVMTL_PA_PLANE_RANGED, 4096, 4096, &out) == -1 && lines == 1 && strstr(logged, "byte range"));
    pipe_failure = 0;
    /* an import NVK refuses closes the descriptor it was given */
    reset(); denial = EPERM; refuse_import = 1;
    T(nvmtl_vk_surface_vram_words(11, 0, 4096, 4096, &out) == -1 && imports == 1);
    T(seen_fd >= 0 && fcntl(seen_fd, F_GETFD) < 0);
    refuse_import = 0;
    T(descriptors() == before);
    if (!bad) puts("every case passed; no descriptor left open");
    return bad != 0;
}
'''
fixture = (fixture.replace('/*BUFFER_TYPE*/', buffer_type).replace('/*RECORD_TYPE*/', record_type)
           .replace('/*TOKEN*/', token).replace('/*UNDER_TEST*/', under_test))

compiler = shutil.which('cc') or shutil.which('clang')
if not compiler:
    sys.exit('A C compiler is required.')
with tempfile.TemporaryDirectory() as directory:
    d = pathlib.Path(directory)
    (d / 'fd.c').write_text(fixture)
    build = subprocess.run([compiler, '-std=gnu11', '-Wall', '-Wno-unused-function', '-g', str(d / 'fd.c'), '-o', str(d / 'fd')],
                           capture_output=True, text=True)
    if build.returncode:
        print(build.stderr[-3000:])
        sys.exit('FAIL: the fixture did not compile')
    environment = {k: v for k, v in os.environ.items() if k != 'NVMTL_SURFACE_VRAM_PIPE'}
    failed = []
    for what, value, argv in (('unset', None, []), ('=1', '1', []), ('=0', '0', ['escape'])):
        env = dict(environment)
        if value is not None:
            env['NVMTL_SURFACE_VRAM_PIPE'] = value
        print(f'-- NVMTL_SURFACE_VRAM_PIPE {what}')
        run = subprocess.run([str(d / 'fd')] + argv, capture_output=True, text=True, env=env)
        print(run.stdout, end='')
        print(run.stderr, end='')
        if run.returncode:
            failed.append(what)
    if failed:
        sys.exit('FAIL (NVMTL_SURFACE_VRAM_PIPE ' + ', '.join(failed) + '): the family VRAM import does not get its descriptor '
                 'in a sandbox, loses it quietly, or its way back does not restore the plain open()')
print('all passed')
