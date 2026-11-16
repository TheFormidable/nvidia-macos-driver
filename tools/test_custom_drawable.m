/* Application-defined drawables presented through command buffers of the installed Metal driver.
 * Mac only, with the NullMoth driver installed and its card as the system's Metal device. It starts no virtual
 * machine, installs nothing and writes no file. A drawable that answers -addPresentScheduledHandler: (as the source
 * fork reports Parallels' AsyncDrawable does, calling the stored block from -present without a nil check) must be
 * given a handler before it is presented; one that does not answer it, and a CAMetalDrawable, must not be.
 *   xcrun clang -fobjc-arc -O2 tools/test_custom_drawable.m -framework Foundation -framework Metal \
 *     -framework QuartzCore -o custom-drawable && ./custom-drawable
 * Exit 0: every case passed; 1: a case failed, and a "FAIL <case>: <what>" line on stdout names it; 2: no device of
 * this driver; 70: a crash signal ("CRASH <signal>" and a backtrace on stderr). A failed check is counted, never an
 * assertion, and the probe drawable counts a missing handler instead of calling it, so that a regression ends as exit 1
 * and macOS writes no crash report. The card runner (tools/card_selftest.py) builds and runs it; this form has not yet
 * run on a Mac.
 * From the HighDelay/nvidia-macos-driver-nvk fork, which reports 11 cases and 64 concurrent presents passing on an
 * RTX 2060 SUPER, macOS 26.7.1; here it uses the installed driver instead of loading a plugin by path, and the fork's
 * negative control (which crashes this program when the driver lacks the fix) is left out.
 */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <QuartzCore/QuartzCore.h>
#import <objc/runtime.h>
#include <execinfo.h>
#include <pthread.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

// Failed checks are counted; the first few are printed with the case they belong to. Checks run on the presenting
// threads and on the 64 concurrent ones too, hence the atomic count and the lock around the first failure's text.
static _Atomic unsigned g_failures;
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static char g_first[256];
static void failed(const char *name, const char *what) {
    const unsigned n = atomic_fetch_add(&g_failures, 1);
    pthread_mutex_lock(&g_lock);
    if (!g_first[0]) snprintf(g_first, sizeof g_first, "%s: %s", name, what);
    pthread_mutex_unlock(&g_lock);
    if (n < 64) printf("FAIL %s: %s\n", name, what);
}
#define CHECK(name, condition) do { if (!(condition)) failed((name), #condition); } while (0)

static void crashed(int sig) {
    const char *name = sig == SIGSEGV ? "SIGSEGV" : sig == SIGBUS ? "SIGBUS" : sig == SIGILL ? "SIGILL" : sig == SIGFPE ? "SIGFPE" : sig == SIGABRT ? "SIGABRT" : "SIGNAL";
    write(2, "CRASH ", 6); write(2, name, strlen(name)); write(2, "\n", 1);
    void *frames[64]; int count = backtrace(frames, 64); backtrace_symbols_fd(frames, count, 2);
    _exit(70);
}

@interface NMProbeDrawable : NSObject <MTLDrawable> {
@public
    unsigned _presented, _kind, _registrations, _scheduledCalls, _textureReads;
    double _value;
    dispatch_semaphore_t _done;
    void (^_scheduled)(id<MTLDrawable>);
    const char *_case;   // the case this drawable belongs to, for the failures found in -present
}
@end
@implementation NMProbeDrawable
- (instancetype)init {
    if ((self=[super init])) { _done=dispatch_semaphore_create(0); _case="drawable"; }
    return self;
}
- (void)present { _presented++;dispatch_semaphore_signal(_done); }
- (void)presentAtTime:(CFTimeInterval)t { _kind=1;_value=t;[self present]; }
- (void)presentAfterMinimumDuration:(CFTimeInterval)d { _kind=2;_value=d;[self present]; }
- (void)addPresentedHandler:(MTLDrawablePresentedHandler)block { (void)block; }
- (CFTimeInterval)presentedTime { return 0; }
- (NSUInteger)drawableID { return 1; }
@end

@interface NMAsyncProbeDrawable : NMProbeDrawable @end
@implementation NMAsyncProbeDrawable
- (void)addPresentScheduledHandler:(void (^)(id<MTLDrawable>))block {
    CHECK(_case, block != nil);
    if (block) { _scheduled=[block copy]; _registrations++; }
}
- (void)present {
    // Models the reported Parallels behavior, which calls the stored block without a nil check; a missing block is
    // counted as a failure here instead of being called.
    if (_scheduled) { _scheduled(self); _scheduledCalls++; }
    else failed(_case, "presented without a scheduled handler (the reported AsyncDrawable would call a nil block here)");
    [super present];
}
@end

@interface NMCAMetalProbeDrawable : NMAsyncProbeDrawable <CAMetalDrawable> @end
@implementation NMCAMetalProbeDrawable
- (id<MTLTexture>)texture { _textureReads++;return nil; }
- (CAMetalLayer *)layer { return nil; }
- (void)present {
    // The CAMetalDrawable path must not receive the custom compatibility block.
    CHECK(_case, !_scheduled&&!_registrations);_presented++;dispatch_semaphore_signal(_done);
}
@end

static const char *const KINDS[] = { "presentDrawable:", "presentDrawable:atTime:", "presentDrawable:afterMinimumDuration:" };
static void record(id<MTLCommandBuffer> cb,id<MTLDrawable> drawable,unsigned kind) {
    if(kind==0)[cb presentDrawable:drawable];
    else if(kind==1)[cb presentDrawable:drawable atTime:123.5];
    else [cb presentDrawable:drawable afterMinimumDuration:0.025];
}
static void waitPresented(NMProbeDrawable *drawable) {
    const BOOL presented = !dispatch_semaphore_wait(drawable->_done,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC));
    if (!presented) failed(drawable->_case, "not presented within 5 s of the commit");
    CHECK(drawable->_case, drawable->_presented==1);
}
static void check(id<MTLCommandQueue> queue,Class cls,unsigned kind,BOOL requireHandler,const char *name) {
    id<MTLCommandBuffer> cb=[queue commandBuffer];
    if (!cb) { failed(name, "no command buffer"); return; }
    NMProbeDrawable *drawable=[cls new];drawable->_case=name;record(cb,drawable,kind);
    CHECK(name, drawable->_presented==0);
    CHECK(name, drawable->_registrations==(requireHandler?1u:0u));
    if(cls==NMCAMetalProbeDrawable.class)CHECK(name, drawable->_textureReads==1);
    [cb commit];[cb waitUntilCompleted];CHECK(name, !cb.error);waitPresented(drawable);
    CHECK(name, drawable->_kind==kind&&drawable->_scheduledCalls==(requireHandler?1u:0u));
    if(kind==1)CHECK(name, drawable->_value==123.5);
    if(kind==2)CHECK(name, drawable->_value==0.025);
}
// the case names: the drawable's class (with a handler, without one, CAMetalDrawable), then the present variant
static char g_names[9][96];
int main(void) { @autoreleasepool {
    const int crash_signals[]={SIGSEGV,SIGBUS,SIGILL,SIGFPE,SIGABRT};
    for(unsigned i=0;i<sizeof crash_signals/sizeof crash_signals[0];i++)signal(crash_signals[i],crashed);
    setvbuf(stdout,NULL,_IOLBF,0);alarm(120);
    id<MTLDevice> device=MTLCreateSystemDefaultDevice();
    if(!device||![NSStringFromClass([device class]) hasPrefix:@"NVMTL"]) {
        fprintf(stderr,"no Metal device of this driver (got %s)\n",device?NSStringFromClass([device class]).UTF8String:"none");return 2;
    }
    id<MTLCommandQueue> queue=[device newCommandQueue];
    if(!queue){failed("setup","no command queue");printf("FAIL: 1 failed check; first: %s\n",g_first);return 1;}
    unsigned cases=0;
    for(unsigned kind=0;kind<3;kind++) {
        snprintf(g_names[kind*3],sizeof g_names[0],"custom drawable with handler, %s",KINDS[kind]);
        snprintf(g_names[kind*3+1],sizeof g_names[0],"custom drawable without handler, %s",KINDS[kind]);
        snprintf(g_names[kind*3+2],sizeof g_names[0],"CAMetalDrawable, %s",KINDS[kind]);
        check(queue,NMAsyncProbeDrawable.class,kind,YES,g_names[kind*3]);cases++;
        check(queue,NMProbeDrawable.class,kind,NO,g_names[kind*3+1]);cases++;
        check(queue,NMCAMetalProbeDrawable.class,kind,NO,g_names[kind*3+2]);cases++;
    }
    // Nil arguments and recording after commitment must not install a handler.
    id<MTLCommandBuffer> ignored=[queue commandBuffer];
    for(unsigned kind=0;kind<3;kind++)record(ignored,nil,kind);
    [ignored commit];[ignored waitUntilCompleted];CHECK("nil drawable and after commit", !ignored.error);
    NMAsyncProbeDrawable *late=[NMAsyncProbeDrawable new];late->_case="nil drawable and after commit";
    for(unsigned kind=0;kind<3;kind++)record(ignored,late,kind);
    CHECK("nil drawable and after commit", !late->_registrations&&!late->_presented);cases++;

    // The queued presentation owns the drawable after the caller drops it.
    id<MTLCommandBuffer> owner=[queue commandBuffer];__weak NMAsyncProbeDrawable *weak=nil;
    dispatch_semaphore_t done;
    @autoreleasepool {
        NMAsyncProbeDrawable *temporary=[NMAsyncProbeDrawable new];weak=temporary;done=temporary->_done;
        temporary->_case="drawable dropped by the caller";
        [owner presentDrawable:temporary];
    }
    @autoreleasepool { CHECK("drawable dropped by the caller", weak != nil); }
    [owner commit];[owner waitUntilCompleted];CHECK("drawable dropped by the caller", !owner.error);
    if(dispatch_semaphore_wait(done,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC)))
        failed("drawable dropped by the caller","not presented within 5 s of the commit");
    cases++;

    // Parallel first/ordinary calls use distinct command buffers and drawables.
    dispatch_apply(64,dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^(size_t i){@autoreleasepool{
        check(queue,NMAsyncProbeDrawable.class,(unsigned)i%3,YES,g_names[((unsigned)i%3)*3]);
    }});
    const unsigned failures=atomic_load(&g_failures);
    if(failures) {
        pthread_mutex_lock(&g_lock);
        printf("FAIL: %u failed check(s) in %u policy/lifetime cases and 64 concurrent custom presents; first: %s\n",failures,cases,g_first);
        pthread_mutex_unlock(&g_lock);
        return 1;
    }
    printf("PASS: %u policy/lifetime cases and 64 concurrent custom presents; all three presentation variants, deferred invocation and CAMetalDrawable unchanged\n",cases);
    return 0;
}}
