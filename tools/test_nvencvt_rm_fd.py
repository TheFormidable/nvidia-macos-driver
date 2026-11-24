#!/usr/bin/env python3
"""test_nvencvt_rm_fd.py: libnvencvt's RM connections get a descriptor where a sandbox refuses /dev/null, and fail
cleanly where none can be had.

Each user-client connection of the video library (vendor/nvenc/src/rm.c) is bound to a descriptor number, which the
kernel keys the client's RM file on. Firefox's media process refuses /dev/null; the library then bound the connection
to -1, which the kernel takes as "detached", and went on as if it had a connection. Now a refusal (EPERM, EACCES)
gives the read end of an empty pipe, as the plugin's opens do (plugin/nvmtl_fd_token.h), and any other failure closes
the connection and fails the open. From pull request 487 of the original project (dreamelite96).

Compiles open_conn_fd (and rm_fd_token where it exists), rm_conn_open and rm_conn_close from vendor/nvenc/src/rm.c as
they are, with stand-ins for IOKit and an open() that refuses /dev/null as the sandbox does.

  python3 tools/test_nvencvt_rm_fd.py [--baseline [REF]]
      --baseline REF   the sources of that revision (default HEAD); there this test fails

Needs a C compiler."""
import argparse
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


rm = source('vendor/nvenc/src/rm.c')
parts = []
if 'static int rm_fd_token(void)' in rm:
    parts.append(function(rm, 'static int rm_fd_token(void)'))
parts.append(function(rm, 'static io_connect_t open_conn_fd(NvU32 type, int *fdOut)'))
parts.append(function(rm, 'int rm_conn_open(RmConn *c, NvU32 type)'))
parts.append(function(rm, 'void rm_conn_close(RmConn *c)'))
enum = rm[rm.index('enum { SEL_ESC'):]
enum = enum[:enum.index('\n')]

fixture = r'''
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
/* IOKit, as far as rm.c uses it */
typedef uint32_t NvU32; typedef unsigned io_connect_t, io_service_t, mach_port_t; typedef int kern_return_t;
enum { KERN_SUCCESS = 0, KERN_FAILURE = 5 };
#define kIOMainPortDefault 0
#define NV_IO_MAIN_PORT kIOMainPortDefault   /* vendor/nvenc/src/nv_floor.h, macOS 12 and later */
typedef struct { io_connect_t conn; int fd; } RmConn;
static int opened, closed, binds; static int64_t bound = -99; static int bound_fifo, bound_chr, bound_cloexec;
static mach_port_t mach_task_self(void) { return 1; }
static const char *IOServiceMatching(const char *name) { return name; }
static io_service_t IOServiceGetMatchingService(int port, const char *name) { (void)port; return strcmp(name, "NVAccel") ? 0 : 7; }
static void IOObjectRelease(io_service_t s) { (void)s; }
static kern_return_t IOServiceOpen(io_service_t s, mach_port_t t, uint32_t type, io_connect_t *c) {
    (void)s; (void)t; (void)type; opened++; *c = 100 + (io_connect_t)opened; return KERN_SUCCESS; }
static kern_return_t IOServiceClose(io_connect_t c) { (void)c; closed++; return KERN_SUCCESS; }
static kern_return_t IOConnectCallScalarMethod(io_connect_t c, uint32_t sel, const uint64_t *in, uint32_t n, uint64_t *out, uint32_t *on) {
    (void)c; (void)n; (void)out; (void)on;
    if (sel == 3) {
        binds++; bound = (int64_t)in[0];
        struct stat st;
        if (bound >= 0 && fstat((int)bound, &st) == 0) {
            bound_fifo = S_ISFIFO(st.st_mode); bound_chr = S_ISCHR(st.st_mode);
            bound_cloexec = (fcntl((int)bound, F_GETFD) & FD_CLOEXEC) != 0;
        }
    }
    return KERN_SUCCESS;
}
/* the sandbox */
static int denial, pipe_failure;
static int test_open(const char *path, int flags, ...) {
    mode_t mode = 0;
    if (flags & O_CREAT) { va_list a; va_start(a, flags); mode = (mode_t)va_arg(a, int); va_end(a); }
    if (denial && !strcmp(path, "/dev/null")) { errno = denial; return -1; }
    return openat(AT_FDCWD, path, flags, mode);
}
static int test_pipe(int p[2]) { if (pipe_failure) { errno = EMFILE; return -1; } return pipe(p); }
#define open test_open
#define pipe test_pipe
/*ENUM*/
/*UNDER_TEST*/
#undef open
#undef pipe

static int descriptors(void) { int n = 0; for (int fd = 0; fd < 512; fd++) if (fcntl(fd, F_GETFD) >= 0) n++; return n; }
static int bad;
#define T(c) do { if (!(c)) { printf("FAIL %s (line %d)\n", #c, __LINE__); bad++; } } while (0)
static void reset(void) { opened = closed = binds = 0; bound = -99; bound_fifo = bound_chr = bound_cloexec = 0; }
int main(void) {
    const int before = descriptors();
    RmConn c;
    for (int k = 0; k < 2; k++) {   /* the sandbox refuses /dev/null: a pipe's read end, closed on exec */
        reset(); denial = k ? EACCES : EPERM;
        T(rm_conn_open(&c, 0) == 0 && c.conn && c.fd >= 0);
        T(binds == 1 && bound == c.fd && bound_fifo && bound_cloexec);
        rm_conn_close(&c);
        T(closed == 1 && c.conn == 0 && c.fd == -1);
    }
    if (!bad) puts("sandbox: the connection is bound to a pipe descriptor");
    reset(); denial = 0;              /* no sandbox: /dev/null itself */
    T(rm_conn_open(&c, 1) == 0 && bound_chr);
    rm_conn_close(&c);
    /* no descriptor at all (the pipe fails, or another error than a refusal): the connection is closed, the open
       fails, nothing is bound */
    reset(); denial = EPERM; pipe_failure = 1;
    T(rm_conn_open(&c, 0) == -1 && c.conn == 0 && c.fd == -1);
    T(binds == 0 && opened == 1 && closed == 1);
    pipe_failure = 0;
    reset(); denial = ENOENT;
    T(rm_conn_open(&c, 0) == -1 && binds == 0 && closed == 1);
    if (!bad) puts("no descriptor: the connection is closed and the open fails, nothing bound");
    T(descriptors() == before);
    return bad != 0;
}
'''
fixture = fixture.replace('/*ENUM*/', enum).replace('/*UNDER_TEST*/', '\n'.join(parts))

compiler = shutil.which('cc') or shutil.which('clang')
if not compiler:
    sys.exit('A C compiler is required.')
with tempfile.TemporaryDirectory() as directory:
    d = pathlib.Path(directory)
    (d / 'rm.c').write_text(fixture)
    build = subprocess.run([compiler, '-std=gnu11', '-Wall', '-Wno-unused-function', '-g', str(d / 'rm.c'), '-o', str(d / 'rm')],
                           capture_output=True, text=True)
    if build.returncode:
        print(build.stderr[-3000:])
        sys.exit('FAIL: the fixture did not compile')
    run = subprocess.run([str(d / 'rm')], capture_output=True, text=True)
    print(run.stdout, end='')
    print(run.stderr, end='')
    if run.returncode:
        sys.exit('FAIL: an RM connection gets no descriptor in a sandbox, or goes on without one')
print('all passed')
