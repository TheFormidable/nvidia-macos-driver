#!/usr/bin/env python3
"""Exercise committed surface ownership using the production queue helpers."""
import pathlib, subprocess, tempfile
root=pathlib.Path(__file__).resolve().parents[1]
source=(root/'plugin/NVMTLEventSync.m').read_text()
start=source.index('@interface NVMTLPendingSurfaceWrite')
end=source.index('@implementation NVMTLSubmissionPart',start)
helpers=source[start:end]
fixture=r'''
#import <Foundation/Foundation.h>
#include <assert.h>
#include <stdint.h>
#include <stdatomic.h>
#include <objc/runtime.h>
#include <string.h>
#include <stdlib.h>
#include <time.h>
#include "nvmtl_fence.h"
// the process is WindowServer; the kernel's answer to the marks is markAnswer (-1 = an accelerator without them, the
// default for the first arms; 0 = taken), and every mark is logged as "id:pending"
static const char *test_progname(void){return "WindowServer";}
#define getprogname() test_progname()
static int markAnswer=-1; static NSMutableArray *markLog;
// whether NVAccel publishes NVAccelSurfaceWait (its page-off waits for a pending surface): KERNEL_HALF=1 or 0
static int nvmtl_vk_accel_surface_wait(void){return getenv("KERNEL_HALF")&&!strcmp(getenv("KERNEL_HALF"),"1");}
static int nvmtl_vk_surface_gpu_mark(uint32_t sid,int pending){if(sid&&markLog)[markLog addObject:[NSString stringWithFormat:@"%u:%d",sid,pending]];return markAnswer;}
static void nvlog(const char *f,...){(void)f;}
struct __IOSurface {unsigned id,seed;};
// the kernel dirty mark makes the surface device-current, and IOSurface moves the seed when that happens (measured on
// Apple's driver: one seed step per GPU write); dirtyBumpsSeed models that, dirtyCalls counts the marks
static int dirtyBumpsSeed, dirtyCalls; static struct __IOSurface *dirtySurface;
static int nvmtl_vk_surface_dirty(uint32_t s,uint32_t p){(void)p;dirtyCalls++;if(dirtyBumpsSeed&&dirtySurface&&dirtySurface->id==s)dirtySurface->seed++;return 0;}
static void nvtrace(const char *f,...){(void)f;}
// the kernel surface fence (nvmtl_vk.c): every open is counted with its flags and takes nothing, so the marks below are
// exactly the older mechanism's; tools/test_fence_vk_calls.py proves what a taken hold changes
static unsigned fenceOpens; static uint32_t fenceFlags;
static void nvmtl_vk_fence_open(nvmtl_fence_hold *h,uint32_t flags){fenceOpens++;fenceFlags=flags;h->cookie=0;}
static void nvmtl_vk_fence_close(uint64_t cookie,uint32_t sid){(void)cookie;(void)sid;}
static void nvmtl_vk_fence_close_all(nvmtl_fence_hold *h){h->cookie=0;}
// cross-process busy marks (PR #398) are off in WindowServer, which this test runs as; the consumer side is covered
// by test_surface_publication.py
static int busyWrites;
static void nvmtl_busy_write(IOSurfaceRef s,NSUInteger p,uint64_t seq,int done){(void)s;(void)p;(void)seq;(void)done;busyWrites++;}
static unsigned IOSurfaceGetID(IOSurfaceRef s){return s->id;}
static unsigned IOSurfaceGetSeed(IOSurfaceRef s){return s->seed;}
@class NVMTLCommandBuffer;
@interface NVMTLTexture:NSObject{@public NVMTLTexture *_parent; IOSurfaceRef _surf; BOOL _vramOn,_failOut;NSUInteger _plane;struct {void *img;} _surfaceLinear;}@end
// the planes of a two-plane private surface published together (NVMTLObjects.m): only twoPlane has two planes, so the
// planner groups nothing in the arms before the last one, where a grouped copy-out would abort. In the last arm every
// grouped copy-out is recorded with its roots and answers planesAnswer (the planes NOT copied out, one bit each; -1 =
// not tried), every plane-by-plane publication is recorded in outLog as [texture, dirtied], and a texture with _failOut
// refuses its own.
#include "nvmtl_plane_group.h"
static IOSurfaceRef twoPlane;
static size_t IOSurfaceGetPlaneCount(IOSurfaceRef s){return s==twoPlane?2:0;}
static BOOL planesOn=YES;
static BOOL nvmtl_surfout_planes_on(void){return planesOn;}
static NSMutableArray *planeCalls,*outLog; static int planesAnswer;
static int nvmtl_surface_out_planes(NSArray *roots){if(!planeCalls)abort();[planeCalls addObject:[roots copy]];return planesAnswer;}
@implementation NVMTLTexture
- (BOOL)nvmtlSurfaceOut{if(outLog)[outLog addObject:@[self,@NO]];return !_failOut;}
- (BOOL)nvmtlSurfaceOutDirtied:(BOOL)dirtied{if(outLog)[outLog addObject:@[self,@(dirtied)]];return !_failOut;}
@end
@interface NVMTLCommandBuffer:NSObject{@public id _queue;NSArray *_parts;NSDictionary *_busyMarks;nvmtl_fence_hold _fence;}@end
@implementation NVMTLCommandBuffer @end
@interface NVMTLSubmissionPart:NSObject{@public NSSet *surfaces;}@end
@implementation NVMTLSubmissionPart @end
'''
checks=r'''
static NVMTLTexture *texture(IOSurfaceRef surface){NVMTLTexture*t=[NVMTLTexture new];t->_surf=surface;t->_vramOn=YES;return t;}
static NVMTLDeferredPublication *settledOf(NVMTLTexture*t,BOOL dirtied,NVMTLCommandBuffer*owner){NVMTLDeferredPublication*d=[NVMTLDeferredPublication new];d->texture=t;d->dirtied=dirtied;d->owner=owner;return d;}
static BOOL published(NSArray*log,NVMTLTexture*t){for(NSArray*e in log)if(e[0]==t)return YES;return NO;}
static NVMTLCommandBuffer *writer(id queue,NVMTLTexture*t){NVMTLCommandBuffer*b=[NVMTLCommandBuffer new];b->_queue=queue;NVMTLSubmissionPart*p=[NVMTLSubmissionPart new];p->surfaces=[NSSet setWithObject:t];b->_parts=@[p];return b;}
int main(void){@autoreleasepool{
 struct {unsigned id,seed;} surface={4,7};
 NVMTLTexture*t=texture((IOSurfaceRef)&surface),*alias=texture((IOSurfaceRef)&surface);
 NSObject*q=[NSObject new],*other=[NSObject new];
 NVMTLCommandBuffer*a=writer(q,t),*b=writer(q,t),*c=writer(other,t);
 assert(!nvmtl_surface_pending_write(b,t));
 nvmtl_surface_register_writes(a);
 assert(nvmtl_surface_pending_write(b,t));
 assert(nvmtl_surface_pending_write(b,alias));
 assert(!nvmtl_surface_pending_write(a,t));
 assert(nvmtl_surface_pending_write(c,t));
 struct {unsigned id,seed;} otherSurface={8,7};NVMTLTexture *unrelated=texture((IOSurfaceRef)&otherSurface);
 assert(!nvmtl_surface_pending_write(c,unrelated));
 surface.seed++;assert(!nvmtl_surface_pending_write(b,t));surface.seed--;
 alias->_vramOn=NO;assert(!nvmtl_surface_pending_write(b,alias));alias->_vramOn=YES;
 t->_vramOn=NO;assert(nvmtl_surface_pending_write(b,t));assert(!nvmtl_surface_pending_write(b,alias));t->_vramOn=YES;
 // a Managed wrapper's tiled image is a backing of its own, not the shared VRAM: two wrappers of one surface are one
 // backing only when both are direct imports, and a texture still matches its own record
 alias->_surfaceLinear.img=(void*)1;assert(!nvmtl_surface_pending_write(b,alias));
 t->_surfaceLinear.img=(void*)1;assert(nvmtl_surface_pending_write(b,t));
 assert(!nvmtl_surface_pending_write(b,alias));t->_surfaceLinear.img=NULL;alias->_surfaceLinear.img=NULL;
 alias->_plane=1;assert(!nvmtl_surface_pending_write(b,alias));alias->_plane=0;
 nvmtl_surface_register_writes(b);nvmtl_surface_finish_writes(a);
 assert(nvmtl_surface_pending_write(a,t));
 nvmtl_surface_finish_writes(b);assert(!nvmtl_surface_pending_write(a,t));
 assert(nvmtl_surface_writers.count==0);
 for(unsigned i=0;i<1000;i++){nvmtl_surface_register_writes(a);assert(nvmtl_surface_pending_write(b,t));nvmtl_surface_finish_writes(a);assert(nvmtl_surface_writers.count==0);}
 __weak NVMTLCommandBuffer *weak;
 @autoreleasepool{NVMTLCommandBuffer*ephemeral=writer(q,t);weak=ephemeral;nvmtl_surface_register_writes(ephemeral);}
 assert(!weak);assert(!nvmtl_surface_pending_write(b,t));
 nvmtl_surface_finish_writes(b);assert(nvmtl_surface_writers.count==0);
 // WindowServer with an accelerator that takes the marks. The commit runs in production order (nvmtlSubmitWithEvents,
 // checked from its source below): the kernel marks, then the pending-write records.
 markAnswer=0; markLog=[NSMutableArray new]; dirtyBumpsSeed=1; dirtySurface=(struct __IOSurface*)&surface; dirtyCalls=0;
 const BOOL half=nvmtl_vk_accel_surface_wait();
 NVMTLCommandBuffer*w1=writer(q,t),*w2=writer(q,t);
 #define COMMIT(w) do{nvmtl_ws_mark_pending(w);nvmtl_surface_register_writes(w);}while(0)
 unsigned seed0=surface.seed;
 COMMIT(w1);
 assert([markLog containsObject:@"4:1"]);        // pending in the kernel either way
 // the commit asks the kernel fence for a compositor hold of the surface it writes, ended by the GPU itself only with the
 // early ordering
 assert(fenceOpens==1&&fenceFlags==(NVMTL_FENCE_F_COMPOSITOR|(half?NVMTL_FENCE_F_AUTO:0u))&&w1->_fence.n==1&&w1->_fence.sid[0]==4);
 if(half){
  // the capture fix: device-current at commit, which moves the seed, and the record must be stamped after that move,
  // or the next command buffer reads the surface as if its write had landed and imports the CPU copy over the GPU's
  assert(dirtyCalls==1&&surface.seed==seed0+1);
  assert(nvmtl_surface_pending_write(w2,t));
 }else{
  // an accelerator whose page-off does not wait: nothing is marked newest at commit (a CPU lock would page off a
  // surface still being drawn); the record is stamped as before
  assert(dirtyCalls==0&&surface.seed==seed0);
  assert(nvmtl_surface_pending_write(w2,t));
 }
 nvmtl_surface_finish_writes(w1);
 assert([markLog.lastObject isEqual:@"4:0"]&&!nvmtl_surface_pending_write(w2,t));
 // a surface without a shared VRAM copy is never marked newest at commit, and its record is stamped the same way
 t->_vramOn=NO;dirtyCalls=0;[markLog removeAllObjects];const unsigned seed1=surface.seed;
 COMMIT(w1);
 assert(dirtyCalls==0&&surface.seed==seed1&&[markLog containsObject:@"4:1"]&&nvmtl_surface_pending_write(w2,t));
 nvmtl_surface_finish_writes(w1);t->_vramOn=YES;
 // a tiled image of a Managed wrapper reaches the VRAM only by its copy at publication: its VRAM is not marked newest at
 // commit
 t->_surfaceLinear.img=(void*)1;dirtyCalls=0;const unsigned tiledSeed=surface.seed;
 COMMIT(w1);assert(dirtyCalls==0&&surface.seed==tiledSeed);
 nvmtl_surface_finish_writes(w1);t->_surfaceLinear.img=NULL;
 // a CPU write after the commit still invalidates the record (later CPU edits import)
 COMMIT(w1);surface.seed++;assert(!nvmtl_surface_pending_write(w2,t));nvmtl_surface_finish_writes(w1);
 assert(nvmtl_surface_writers.count==0);
 // two command buffers in flight write the same surface: the kernel has one pending bit, so the older one finishing
 // first must not release it while the newer one still writes
 [markLog removeAllObjects];
 COMMIT(w1);COMMIT(w2);
 assert([nvmtl_ws_pending_holds countForObject:@4]==2);
 nvmtl_surface_finish_writes(w1);
 assert(![markLog containsObject:@"4:0"]&&[nvmtl_ws_pending_holds countForObject:@4]==1);
 nvmtl_surface_finish_writes(w1);   // a second finish of the same buffer releases nothing more
 assert(![markLog containsObject:@"4:0"]&&[nvmtl_ws_pending_holds countForObject:@4]==1);
 nvmtl_surface_finish_writes(w2);
 assert([markLog.lastObject isEqual:@"4:0"]&&[nvmtl_ws_pending_holds countForObject:@4]==0);
 assert(nvmtl_surface_writers.count==0&&!nvmtl_ws_deferred.count);
 // The private planes of one two-plane surface that settle together (the NV12 frame WindowServer draws for a screen
 // share) are handed to ONE copy-out, with their root textures in plane order, whatever order they settled in; a
 // surface of one plane beside them is published on its own, and no plane of the group is published twice.
 { struct __IOSurface nv={9,1},single={10,1}; twoPlane=(IOSurfaceRef)&nv;
   NVMTLTexture *y=[NVMTLTexture new],*uv=[NVMTLTexture new],*one=[NVMTLTexture new],*view=[NVMTLTexture new];
   y->_surf=twoPlane;uv->_surf=twoPlane;uv->_plane=1;one->_surf=(IOSurfaceRef)&single;view->_parent=uv;view->_surf=twoPlane;view->_plane=1;
   NVMTLCommandBuffer *A=[NVMTLCommandBuffer new],*B=[NVMTLCommandBuffer new];
   planeCalls=[NSMutableArray new];outLog=[NSMutableArray new];planesAnswer=0;
   assert(nvmtl_ws_publish_settled(@[settledOf(view,NO,A),settledOf(one,NO,A),settledOf(y,NO,B)],B));
   assert(planeCalls.count==1&&[planeCalls[0] count]==2&&planeCalls[0][0]==y&&planeCalls[0][1]==uv);   // a view stands for its root
   assert(outLog.count==1&&outLog[0][0]==one);
   // each publication answers for its own plane: B fails when ITS plane (0) was not copied out, and not when A's was
   planesAnswer=1<<0; assert(!nvmtl_ws_publish_settled(@[settledOf(uv,NO,A),settledOf(y,NO,B)],B));
   planesAnswer=1<<1; assert(nvmtl_ws_publish_settled(@[settledOf(uv,NO,A),settledOf(y,NO,B)],B));
   assert(!nvmtl_ws_publish_settled(@[settledOf(uv,NO,A),settledOf(y,NO,B)],A));
   planesAnswer=(1<<0)|(1<<1); assert(!nvmtl_ws_publish_settled(@[settledOf(uv,NO,A),settledOf(y,NO,B)],B));
   assert(planeCalls.count==5&&outLog.count==1);   // answered by the group: no plane was published again on its own
   // not tried (a plane is not copyable): every member on its own, as before, each answering for itself
   planesAnswer=-1;[outLog removeAllObjects];uv->_failOut=YES;
   assert(nvmtl_ws_publish_settled(@[settledOf(uv,NO,A),settledOf(y,NO,B)],B)&&outLog.count==2&&published(outLog,y)&&published(outLog,uv));
   assert(!nvmtl_ws_publish_settled(@[settledOf(uv,NO,A),settledOf(y,NO,B)],A));
   uv->_failOut=NO;
   // a plane whose commit marked its VRAM (dirtied), or one that lives in VRAM, keeps the whole surface plane by plane
   const NSUInteger calls=planeCalls.count;planesAnswer=0;[outLog removeAllObjects];
   assert(nvmtl_ws_publish_settled(@[settledOf(uv,YES,A),settledOf(y,NO,B)],B)&&planeCalls.count==calls&&outLog.count==2);
   assert([outLog[0][1] boolValue]&&![outLog[1][1] boolValue]);   // and the dirtied one is still published as dirtied
   uv->_vramOn=YES;[outLog removeAllObjects];
   assert(nvmtl_ws_publish_settled(@[settledOf(uv,NO,A),settledOf(y,NO,B)],B)&&planeCalls.count==calls&&outLog.count==2);
   uv->_vramOn=NO;
   // one plane alone, or two publications of the same plane, are not a group
   [outLog removeAllObjects];
   assert(nvmtl_ws_publish_settled(@[settledOf(y,NO,B)],B)&&nvmtl_ws_publish_settled(@[settledOf(y,NO,A),settledOf(y,NO,B)],B));
   assert(planeCalls.count==calls&&outLog.count==3);
   // NVMTL_SURFOUT_PLANES=0: plane by plane, as before
   planesOn=NO;[outLog removeAllObjects];
   assert(nvmtl_ws_publish_settled(@[settledOf(uv,NO,A),settledOf(y,NO,B)],B)&&planeCalls.count==calls&&outLog.count==2);
   planesOn=YES;
   assert(nvmtl_ws_publish_settled(@[settledOf(uv,NO,A),settledOf(y,NO,B)],B)&&planeCalls.count==calls+1);
   planeCalls=nil;outLog=nil;twoPlane=NULL;
 }
 printf("PASS (kernel half %s): aliases, CPU seeds, queues, planes, private backing, latest writer, weak lifetime, 1000 cleanup "
        "cycles, WindowServer commit: %s, CPU edits still import, overlapping writers release the kernel mark once, with "
        "the last of them; the private planes of a two-plane surface settle into one copy-out, each answering for itself\n", half?"present":"absent",
        half?"VRAM marked newest at commit and the record stamped after the kernel's seed move"
            :"nothing marked newest at commit (old ordering for an accelerator without the page-off wait)");
}return 0;}
'''
# The commit's order lives in nvmtlSubmitWithEvents, which this harness cannot run: read it from the source. The kernel
# marks can wait for the accelerator lock, so they sit between the two class-lock sections, before the records are
# stamped; inside them the VRAM marks go in before this command buffer's own pending marks.
commit=source[source.index('- (void)nvmtlSubmitWithEvents'):]
commit=commit[:commit.index('\n}\n')]
first_lock_end=commit.index('\n    }\n')
marks=commit.index('nvmtl_ws_mark_pending(self);')
second_lock=commit.index('@synchronized ([NVMTLCommandBuffer class])',first_lock_end)
stamp=commit.index('nvmtl_surface_register_writes(self);')
assert first_lock_end<marks<second_lock<stamp, 'commit order: (class lock) seal, (unlocked) kernel marks, (class lock) records'
assert commit.index('_commitMarking = YES')<first_lock_end and second_lock<commit.index('_commitMarking = NO'), 'no submit-ahead while marking'
assert commit.count('nvmtl_vk_surface_dirty')==0
pend=source[source.index('static void nvmtl_ws_mark_pending('):]
pend=pend[:pend.index('\n}\n')]
assert pend.index('nvmtl_vk_surface_dirty(')<pend.index('nvmtl_ws_mark(n.unsignedIntValue, YES)'), 'VRAM marks before pending marks'
print('PASS: production commit order: kernel marks outside the class lock, VRAM marks before pending marks, records after both')
with tempfile.TemporaryDirectory() as d:
 p=pathlib.Path(d);(p/'test.m').write_text(fixture+helpers+checks)
 subprocess.run(['clang','-fobjc-arc','-I',str(root/'plugin'),'-framework','Foundation',str(p/'test.m'),'-o',str(p/'test')],check=True)
 import os
 for half in ('1','0'): subprocess.run([str(p/'test')],check=True,env=dict(os.environ,KERNEL_HALF=half))
