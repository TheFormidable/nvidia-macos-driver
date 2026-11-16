/* device_probe.m: the Metal device and session facts that tools/card_selftest.py checks before any card check runs.
 * The runner builds it as <out>/bin/device-probe/nvmtltest (the plugin grants a Metal device to that program name,
 * plugin/NVMTLDevice.m) and runs it once, with its own 20 s alarm inside the runner's 30 s limit:
 *   /usr/bin/xcrun clang -arch x86_64 -mmacosx-version-min=15.0 -fobjc-arc -O1 tools/card/device_probe.m \
 *     -framework Foundation -framework Metal -framework CoreGraphics -o <out>/bin/device-probe/nvmtltest
 * It prints the class and name of MTLCreateSystemDefaultDevice and of every device of MTLCopyAllDevices, and whether a
 * console GUI session is usable for the windowed checks: CGSessionCopyCurrentDictionary present, its on-console,
 * login-done and screen-locked keys, and CGDisplayIsAsleep of the main display. Of the session it reads only those
 * keys and prints the names (never the values) of the keys that mention a lock; it reads no key that names the user.
 * Exit 0: the default device is of class NVMTLDevice and its name contains " (NVMTL over "; 3: it is not (first line
 * "SKIP: no device of this driver (<class>)"); 70: a crash signal (the handler writes "CRASH <signal>" and a backtrace
 * on stderr). It writes no file, opens no window and makes no GPU work beyond opening the device. Not yet run on a
 * Mac. */
#import <CoreGraphics/CoreGraphics.h>
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <execinfo.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static const char *signal_name(int sig)
{
    switch (sig) {
    case SIGSEGV: return "SIGSEGV";
    case SIGBUS: return "SIGBUS";
    case SIGILL: return "SIGILL";
    case SIGFPE: return "SIGFPE";
    case SIGABRT: return "SIGABRT";
    default: return "SIGNAL";
    }
}

static void crashed(int sig)
{
    const char *name = signal_name(sig);
    write(2, "CRASH ", 6);
    write(2, name, strlen(name));
    write(2, "\n", 1);
    void *frames[64];
    int count = backtrace(frames, 64);
    backtrace_symbols_fd(frames, count, 2);
    _exit(70);
}

/* 1 when the key holds true (a CFBoolean or a nonzero CFNumber), 0 when it is false or absent */
static int session_flag(CFDictionaryRef session, CFStringRef key)
{
    if (!session) return 0;
    CFTypeRef value = CFDictionaryGetValue(session, key);
    if (!value) return 0;
    if (CFGetTypeID(value) == CFBooleanGetTypeID()) return CFBooleanGetValue((CFBooleanRef)value) ? 1 : 0;
    if (CFGetTypeID(value) == CFNumberGetTypeID()) {
        long long n = 0;
        CFNumberGetValue((CFNumberRef)value, kCFNumberLongLongType, &n);
        return n != 0;
    }
    return 1;
}

int main(void)
{
    @autoreleasepool {
        int crash_signals[] = {SIGSEGV, SIGBUS, SIGILL, SIGFPE, SIGABRT};
        for (unsigned i = 0; i < sizeof crash_signals / sizeof crash_signals[0]; i++) signal(crash_signals[i], crashed);
        alarm(20);
        setvbuf(stdout, NULL, _IOLBF, 0);

        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        NSString *cls = device ? NSStringFromClass([device class]) : @"none";
        NSString *name = device ? device.name : @"";
        BOOL ours = [cls isEqualToString:@"NVMTLDevice"] && [name rangeOfString:@" (NVMTL over "].location != NSNotFound;
        if (!ours) printf("SKIP: no device of this driver (%s)\n", cls.UTF8String);
        printf("DEVICE class=%s name=%s\n", cls.UTF8String, name.UTF8String);
        NSArray<id<MTLDevice>> *all = MTLCopyAllDevices();
        printf("ALLDEVICES count=%lu\n", (unsigned long)all.count);
        unsigned index = 0;
        for (id<MTLDevice> each in all)
            printf("ALLDEVICE %u class=%s name=%s\n", index++, NSStringFromClass([each class]).UTF8String, each.name.UTF8String);

        /* the keys of CGSession.h: kCGSessionOnConsoleKey and kCGSessionLoginDoneKey; the lock key has no constant */
        CFDictionaryRef session = CGSessionCopyCurrentDictionary();
        int console = session_flag(session, CFSTR("kCGSSessionOnConsoleKey"));
        int login_done = session_flag(session, CFSTR("kCGSessionLoginDoneKey"));
        int locked = session_flag(session, CFSTR("CGSSessionScreenIsLocked"));
        /* the display only with a session: without one there is no window server connection to ask (-1) */
        int asleep = session ? (CGDisplayIsAsleep(CGMainDisplayID()) ? 1 : 0) : -1;
        printf("GUI session=%d console=%d logindone=%d locked=%d asleep=%d\n", session != NULL, console, login_done, locked, asleep);
        if (session) {
            NSMutableArray<NSString *> *lock_keys = [NSMutableArray array];
            for (id key in (__bridge NSDictionary *)session)
                if ([key isKindOfClass:[NSString class]] && [key rangeOfString:@"lock" options:NSCaseInsensitiveSearch].location != NSNotFound)
                    [lock_keys addObject:key];
            [lock_keys sortUsingSelector:@selector(compare:)];
            printf("GUI lockkeys=%s\n", lock_keys.count ? [lock_keys componentsJoinedByString:@","].UTF8String : "none");
            CFRelease(session);
        }

        if (ours) printf("PASS device class=NVMTLDevice, name contains \" (NVMTL over \"\n");
        printf("RESULT %s 1 %s\n", ours ? "pass" : "skip", ours ? "pass" : "skip");
        return ours ? 0 : 3;
    }
}
