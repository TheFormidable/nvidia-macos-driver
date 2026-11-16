/* Application-defined drawables presented through command buffers of the installed Metal driver.
 * Mac only, with the NullMoth driver installed and its card as the system's Metal device. It starts no virtual
 * machine, installs nothing and writes no file. A drawable that answers -addPresentScheduledHandler: (as the source
 * fork reports Parallels' AsyncDrawable does, calling the stored block from -present without a nil check) must be
 * given a handler before it is presented; one that does not answer it, and a CAMetalDrawable, must not be.
 *   xcrun clang -fobjc-arc -O2 tools/test_custom_drawable.m -framework Foundation -framework Metal \
 *     -framework QuartzCore -o /tmp/custom-drawable && /tmp/custom-drawable
 * Exit 0: every case passed; an assertion names a failed one; 2: no device of this driver.
 * From the HighDelay/nvidia-macos-driver-nvk fork, which reports 11 cases and 64 concurrent presents passing on an
 * RTX 2060 SUPER, macOS 26.7.1; here it uses the installed driver instead of loading a plugin by path, and the fork's
 * negative control (which crashes this program when the driver lacks the fix) is left out.
 */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <QuartzCore/QuartzCore.h>
#import <objc/runtime.h>
#include <assert.h>
#include <stdatomic.h>
#include <stdio.h>
#include <unistd.h>

@interface NMProbeDrawable : NSObject <MTLDrawable> {
@public
    unsigned _presented, _kind, _registrations, _scheduledCalls, _textureReads;
    double _value;
    dispatch_semaphore_t _done;
    void (^_scheduled)(id<MTLDrawable>);
}
@end
@implementation NMProbeDrawable
- (instancetype)init {
    if ((self=[super init])) _done=dispatch_semaphore_create(0);
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
    assert(block);_scheduled=[block copy];_registrations++;
}
- (void)present {
    // Model the reported Parallels behavior: this deliberately has no nil check.
    _scheduled(self);_scheduledCalls++;[super present];
}
@end

@interface NMCAMetalProbeDrawable : NMAsyncProbeDrawable <CAMetalDrawable> @end
@implementation NMCAMetalProbeDrawable
- (id<MTLTexture>)texture { _textureReads++;return nil; }
- (CAMetalLayer *)layer { return nil; }
- (void)present {
    // The CAMetalDrawable path must not receive the custom compatibility block.
    assert(!_scheduled&&!_registrations);_presented++;dispatch_semaphore_signal(_done);
}
@end

static void record(id<MTLCommandBuffer> cb,id<MTLDrawable> drawable,unsigned kind) {
    if(kind==0)[cb presentDrawable:drawable];
    else if(kind==1)[cb presentDrawable:drawable atTime:123.5];
    else [cb presentDrawable:drawable afterMinimumDuration:0.025];
}
static void waitPresented(NMProbeDrawable *drawable) {
    assert(!dispatch_semaphore_wait(drawable->_done,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC)));
    assert(drawable->_presented==1);
}
static void check(id<MTLCommandQueue> queue,Class cls,unsigned kind,BOOL requireHandler) {
    id<MTLCommandBuffer> cb=[queue commandBuffer];assert(cb);
    NMProbeDrawable *drawable=[cls new];record(cb,drawable,kind);
    assert(drawable->_presented==0);
    assert(drawable->_registrations==(requireHandler?1u:0u));
    if(cls==NMCAMetalProbeDrawable.class)assert(drawable->_textureReads==1);
    [cb commit];[cb waitUntilCompleted];assert(!cb.error);waitPresented(drawable);
    assert(drawable->_kind==kind&&drawable->_scheduledCalls==(requireHandler?1u:0u));
    if(kind==1)assert(drawable->_value==123.5);
    if(kind==2)assert(drawable->_value==0.025);
}
int main(void) { @autoreleasepool {
    setvbuf(stdout,NULL,_IOLBF,0);alarm(120);
    id<MTLDevice> device=MTLCreateSystemDefaultDevice();
    if(!device||![NSStringFromClass([device class]) hasPrefix:@"NVMTL"]) {
        fprintf(stderr,"no Metal device of this driver (got %s)\n",device?NSStringFromClass([device class]).UTF8String:"none");return 2;
    }
    id<MTLCommandQueue> queue=[device newCommandQueue];assert(queue);
    unsigned cases=0;
    for(unsigned kind=0;kind<3;kind++) {
        check(queue,NMAsyncProbeDrawable.class,kind,YES);cases++;
        check(queue,NMProbeDrawable.class,kind,NO);cases++;
        check(queue,NMCAMetalProbeDrawable.class,kind,NO);cases++;
    }
    // Nil arguments and recording after commitment must not install a handler.
    id<MTLCommandBuffer> ignored=[queue commandBuffer];
    for(unsigned kind=0;kind<3;kind++)record(ignored,nil,kind);
    [ignored commit];[ignored waitUntilCompleted];assert(!ignored.error);
    NMAsyncProbeDrawable *late=[NMAsyncProbeDrawable new];
    for(unsigned kind=0;kind<3;kind++)record(ignored,late,kind);
    assert(!late->_registrations&&!late->_presented);cases++;

    // The queued presentation owns the drawable after the caller drops it.
    id<MTLCommandBuffer> owner=[queue commandBuffer];__weak NMAsyncProbeDrawable *weak=nil;
    dispatch_semaphore_t done;
    @autoreleasepool {
        NMAsyncProbeDrawable *temporary=[NMAsyncProbeDrawable new];weak=temporary;done=temporary->_done;
        [owner presentDrawable:temporary];
    }
    @autoreleasepool { assert(weak); }
    [owner commit];[owner waitUntilCompleted];assert(!owner.error);
    assert(!dispatch_semaphore_wait(done,dispatch_time(DISPATCH_TIME_NOW,5*NSEC_PER_SEC)));cases++;

    // Parallel first/ordinary calls use distinct command buffers and drawables.
    dispatch_apply(64,dispatch_get_global_queue(QOS_CLASS_USER_INITIATED,0),^(size_t i){@autoreleasepool{
        check(queue,NMAsyncProbeDrawable.class,(unsigned)i%3,YES);
    }});
    printf("PASS: %u policy/lifetime cases and 64 concurrent custom presents; all three presentation variants, deferred invocation and CAMetalDrawable unchanged\n",cases);
    return 0;
}}
