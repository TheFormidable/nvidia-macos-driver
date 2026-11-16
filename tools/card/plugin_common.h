/* plugin_common.h: what the Metal plugin's card programs (tools/card/plugin_*.m) share, as static functions in each
 * program: handlers that turn a crash signal into "CRASH <signal>" and a backtrace on stderr and exit 70 (so that a
 * crash ends as an exit code and macOS writes no crash report), the program's own alarm, the device check (class
 * exactly NVMTLDevice, else exit 3 with "SKIP: no device of this driver (<class>)"), the PASS, FAIL, INFO and RESULT
 * lines with the exit code they give (0 pass, 1 a case failed, 2 only setup failed, 3 skip), reads of the driver's
 * texture fields by name (no header of the plugin is included), and the lines this process wrote to the driver's own
 * log. The log is the file NVMTL_LOG_PATH names, which the card runner puts in its run folder; it is read, never
 * written. Not yet run on a Mac. */
#ifndef PLUGIN_COMMON_H
#define PLUGIN_COMMON_H
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <objc/runtime.h>
#include <execinfo.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

// every helper is static and may go unused in a program
#define NM_FN static __attribute__((unused))

static _Atomic unsigned nm_passed, nm_failed, nm_setup_failed;
static char nm_first[256]; // the first failure, repeated on the RESULT line
static pthread_mutex_t nm_lock = PTHREAD_MUTEX_INITIALIZER;

NM_FN const char *nm_signal_name(int sig)
{
    switch (sig) {
    case SIGSEGV:
        return "SIGSEGV";
    case SIGBUS:
        return "SIGBUS";
    case SIGILL:
        return "SIGILL";
    case SIGFPE:
        return "SIGFPE";
    case SIGABRT:
        return "SIGABRT";
    default:
        return "SIGNAL";
    }
}

NM_FN void nm_crashed(int sig)
{
    const char *name = nm_signal_name(sig);
    write(2, "CRASH ", 6);
    write(2, name, strlen(name));
    write(2, "\n", 1);
    void *frames[64];
    int count = backtrace(frames, 64);
    backtrace_symbols_fd(frames, count, 2);
    _exit(70);
}

// the crash handlers, line-buffered stdout and the program's own time limit
NM_FN void nm_start(unsigned seconds)
{
    const int crash_signals[] = {SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT};
    for (unsigned i = 0; i < sizeof crash_signals / sizeof crash_signals[0]; i++) signal(crash_signals[i], nm_crashed);
    setvbuf(stdout, NULL, _IOLBF, 0);
    alarm(seconds);
}

NM_FN void nm_record(const char *line)
{
    pthread_mutex_lock(&nm_lock);
    if (!nm_first[0]) snprintf(nm_first, sizeof nm_first, "%s", line);
    pthread_mutex_unlock(&nm_lock);
}

NM_FN void nm_pass(const char *name, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
NM_FN void nm_pass(const char *name, const char *fmt, ...)
{
    char text[512];
    va_list a;
    va_start(a, fmt);
    vsnprintf(text, sizeof text, fmt, a);
    va_end(a);
    printf("PASS %s %s\n", name, text);
    atomic_fetch_add(&nm_passed, 1);
}

NM_FN void nm_fail(const char *name, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
NM_FN void nm_fail(const char *name, const char *fmt, ...)
{
    char text[512], line[640];
    va_list a;
    va_start(a, fmt);
    vsnprintf(text, sizeof text, fmt, a);
    va_end(a);
    snprintf(line, sizeof line, "%s: %s", name, text);
    printf("FAIL %s\n", line);
    nm_record(line);
    atomic_fetch_add(&nm_failed, 1);
}

// a case that could not build its own objects (a library, a pipeline, a texture the case needs to start)
NM_FN void nm_setup(const char *name, const char *fmt, ...) __attribute__((format(printf, 2, 3)));
NM_FN void nm_setup(const char *name, const char *fmt, ...)
{
    char text[512], line[640];
    va_list a;
    va_start(a, fmt);
    vsnprintf(text, sizeof text, fmt, a);
    va_end(a);
    snprintf(line, sizeof line, "%s: setup: %s", name, text);
    printf("FAIL %s\n", line);
    nm_record(line);
    atomic_fetch_add(&nm_setup_failed, 1);
}

NM_FN void nm_info(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
NM_FN void nm_info(const char *fmt, ...)
{
    char text[512];
    va_list a;
    va_start(a, fmt);
    vsnprintf(text, sizeof text, fmt, a);
    va_end(a);
    printf("INFO %s\n", text);
}

// "SKIP: <why>" (the first line on stdout when nothing was printed before) and exit 3
NM_FN void nm_skip(const char *fmt, ...) __attribute__((format(printf, 1, 2), noreturn));
NM_FN void nm_skip(const char *fmt, ...)
{
    char text[512];
    va_list a;
    va_start(a, fmt);
    vsnprintf(text, sizeof text, fmt, a);
    va_end(a);
    printf("SKIP: %s\n", text);
    printf("RESULT skip %u passed, %u failed (%s)\n", atomic_load(&nm_passed), atomic_load(&nm_failed), text);
    exit(3);
}

// the RESULT line and the exit code: 1 when a case failed, 2 when only setup failed, 0 when every case passed; a run
// in which no case ran fails, so that a program cut short never reads as a pass
NM_FN int nm_finish(void)
{
    unsigned p = atomic_load(&nm_passed), f = atomic_load(&nm_failed), s = atomic_load(&nm_setup_failed);
    if (!p && !f && !s) {
        printf("FAIL nothing: no case ran\n");
        f = 1;
        nm_record("nothing: no case ran");
    }
    if (!f && !s) {
        printf("RESULT pass %u passed, 0 failed\n", p);
        return 0;
    }
    pthread_mutex_lock(&nm_lock);
    printf("RESULT fail %u passed, %u failed, %u setup (first: %s)\n", p, f, s, nm_first);
    pthread_mutex_unlock(&nm_lock);
    return f ? 1 : 2;
}

// MTLCreateSystemDefaultDevice when its class is exactly NVMTLDevice; otherwise SKIP and exit 3
NM_FN id<MTLDevice> nm_device(void)
{
    id<MTLDevice> device = MTLCreateSystemDefaultDevice();
    NSString *cls = device ? NSStringFromClass([device class]) : @"none";
    if (![cls isEqualToString:@"NVMTLDevice"]) nm_skip("no device of this driver (%s)", cls.UTF8String);
    return device;
}

// the address of an instance variable of the driver's object, read by name; NULL when the class has no such field
NM_FN const void *nm_field(id object, const char *name)
{
    Ivar iv = object ? class_getInstanceVariable([object class], name) : NULL;
    return iv ? (const char *)(__bridge void *)object + ivar_getOffset(iv) : NULL;
}

// a BOOL field: 1 or 0, or -1 when the field is not there
NM_FN int nm_bool_field(id object, const char *name)
{
    const unsigned char *p = nm_field(object, name);
    return p ? *p != 0 : -1;
}

// the first pointer of a field (an object pointer, or the first member of a struct such as nvk_image's img), read
// without retaining it; *found says whether the field is there
NM_FN const void *nm_pointer_field(id object, const char *name, BOOL *found)
{
    void *const *p = nm_field(object, name);
    if (found) *found = p != NULL;
    return p ? *p : NULL;
}

// the lines of the driver's log that this process wrote, without their "pid N NVMTL: " prefix; nil when NVMTL_LOG_PATH
// is not set, an empty list when the file is missing or unreadable. The driver opens, appends and closes the file for
// every line, so what it said before this call is there.
NM_FN NSArray<NSString *> *nm_log_lines(void)
{
    const char *path = getenv("NVMTL_LOG_PATH");
    if (!path || !*path) return nil;
    NSData *data = [NSData dataWithContentsOfFile:[NSString stringWithUTF8String:path]];
    if (!data) return @[];
    NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    if (!text) text = [[NSString alloc] initWithData:data encoding:NSISOLatin1StringEncoding];
    NSString *prefix = [NSString stringWithFormat:@"pid %d NVMTL: ", (int)getpid()];
    NSMutableArray<NSString *> *mine = [NSMutableArray array];
    for (NSString *line in [text componentsSeparatedByString:@"\n"])
        if ([line hasPrefix:prefix]) [mine addObject:[line substringFromIndex:prefix.length]];
    return mine;
}

// the last line that contains every one of the given pieces (a NULL-terminated list), or nil
NM_FN NSString *nm_log_find(NSArray<NSString *> *lines, const char *piece, ...) __attribute__((sentinel));
NM_FN NSString *nm_log_find(NSArray<NSString *> *lines, const char *piece, ...)
{
    const char *pieces[8];
    unsigned n = 0;
    va_list a;
    va_start(a, piece);
    for (const char *p = piece; p && n < 8; p = va_arg(a, const char *)) pieces[n++] = p;
    va_end(a);
    for (NSString *line in [lines reverseObjectEnumerator]) {
        BOOL all = YES;
        for (unsigned i = 0; i < n && all; i++)
            all = [line rangeOfString:[NSString stringWithUTF8String:pieces[i]]].location != NSNotFound;
        if (all) return line;
    }
    return nil;
}

// a command buffer committed and waited for (the alarm bounds the wait); NO, with a FAIL line, when it did not complete
NM_FN BOOL nm_run(id<MTLCommandBuffer> cb, const char *name)
{
    [cb commit];
    [cb waitUntilCompleted];
    if (cb.status != MTLCommandBufferStatusCompleted) {
        nm_fail(name, "command buffer status %ld: %s", (long)cb.status,
                cb.error ? cb.error.localizedDescription.UTF8String : "no error");
        return NO;
    }
    return YES;
}

NM_FN id<MTLLibrary> nm_library(id<MTLDevice> device, NSString *source, const char *name)
{
    NSError *error = nil;
    id<MTLLibrary> library = [device newLibraryWithSource:source options:nil error:&error];
    if (!library) {
        NSString *text = [error.localizedDescription stringByReplacingOccurrencesOfString:@"\n" withString:@" "];
        nm_setup(name, "library: %.300s", text ? text.UTF8String : "no error text");
    }
    return library;
}

NM_FN id<MTLComputePipelineState> nm_kernel(id<MTLDevice> device, id<MTLLibrary> library, NSString *function,
                                            const char *name)
{
    id<MTLFunction> fn = [library newFunctionWithName:function];
    NSError *error = nil;
    id<MTLComputePipelineState> pipeline = fn ? [device newComputePipelineStateWithFunction:fn error:&error] : nil;
    if (!pipeline)
        nm_setup(name, "compute pipeline %s: %.300s", function.UTF8String,
                 fn ? (error ? error.localizedDescription.UTF8String : "no error text") : "no such function");
    return pipeline;
}
#endif
