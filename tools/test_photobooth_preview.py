#!/usr/bin/env python3
"""Photo Booth's preview adapter (plugin/NVMTLPhotoBooth.m), run from the plugin's own source against stand-in classes.

The adapter's functions are lifted from the source and compiled with doubles of CAMetalLayer's drawable, CIImage and
CIContext, so nothing of Core Image, Metal or Photo Booth is loaded. The cases: only Photo Booth itself
(com.apple.PhotoBooth) is matched; only a drawable texture of this driver is marked; the reflection maps the rows of the
rendered bounds onto each other, nonzero origins included, and refuses empty, negative and non-finite bounds; the render
call passes every other argument through unchanged and reflects only for a marked texture; the type-signature check
accepts the two shapes the adapter is written for and refuses others; a replacement given to a subclass leaves the
superclass's method as it was. A control compiles the reflection with its transform taken out and must fail.

Needs an Objective-C compiler (xcrun clang: a Mac, or the Linux stand-in environment); without one the test is skipped.
The source of the adapter is the HighDelay/nvidia-macos-driver-nvk fork."""
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'plugin/NVMTLPhotoBooth.m').read_text()


def function(signature):
    start = SOURCE.index(signature)
    cursor = SOURCE.index('{', start) + 1
    depth = 1
    while depth:
        depth += (SOURCE[cursor] == '{') - (SOURCE[cursor] == '}')
        cursor += 1
    return SOURCE[start:cursor] + '\n'


TYPES = SOURCE[SOURCE.index('typedef struct { double a, b, c, d, tx, ty; }'):SOURCE.index('static BOOL nvmtl_photobooth_process(')]
FUNCTIONS = ''.join(function(s) for s in (
    'static BOOL nvmtl_photobooth_process(',
    'static id nvmtl_photobooth_next_drawable(',
    'static BOOL nvmtl_photobooth_reflection(',
    'static void nvmtl_photobooth_render_preview(',
    'static BOOL nvmtl_photobooth_signature(',
    'static void nvmtl_photobooth_replace(',
    'static BOOL nvmtl_photobooth_flip_on(',
))
TRANSFORM = '*transform = (nvmtl_photobooth_affine){ 1, 0, 0, -1, 0, y };'
if TRANSFORM not in FUNCTIONS:
    print('FAIL: nvmtl_photobooth_reflection does not build the transform this test checks')
    sys.exit(1)

FIXTURE = r'''
#import <Foundation/Foundation.h>
#import <objc/runtime.h>
#import <objc/message.h>
#include <assert.h>
#include <float.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

@interface NMPhotoBoothProbeTexture : NSObject @end
@implementation NMPhotoBoothProbeTexture @end
#define NVMTLTexture NMPhotoBoothProbeTexture
@@TYPES@@
@interface NMPhotoBoothProbeImage : NSObject { @public nvmtl_photobooth_affine applied; }
- (id)imageByApplyingTransform:(nvmtl_photobooth_affine)t;
@end
@implementation NMPhotoBoothProbeImage
- (id)imageByApplyingTransform:(nvmtl_photobooth_affine)t { NMPhotoBoothProbeImage *r = [NMPhotoBoothProbeImage new]; r->applied = t; return r; }
@end
@interface NMPhotoBoothProbeDrawable : NSObject
@property (strong) id texture;
@end
@implementation NMPhotoBoothProbeDrawable @end
@interface NMPhotoBoothProbeLayer : NSObject
- (id)nextDrawable;
- (double)wrongReturn;
@end
@implementation NMPhotoBoothProbeLayer
- (id)nextDrawable { return @1; }
- (double)wrongReturn { return 0; }
@end
@interface NMPhotoBoothProbeChildLayer : NMPhotoBoothProbeLayer @end
@implementation NMPhotoBoothProbeChildLayer @end
@interface NMPhotoBoothProbeContext : NSObject
- (void)render:(id)i toMTLTexture:(id)t commandBuffer:(id)c bounds:(CGRect)b colorSpace:(void *)cs;
@end
@implementation NMPhotoBoothProbeContext
- (void)render:(id)i toMTLTexture:(id)t commandBuffer:(id)c bounds:(CGRect)b colorSpace:(void *)cs { (void)i; (void)t; (void)c; (void)b; (void)cs; }
@end

static id nextResult, receivedImage, receivedTexture, receivedCommand;
static CGRect receivedBounds;
static void *receivedColorSpace;
static id fakeNext(id layer, SEL selector) { (void)layer; (void)selector; return nextResult; }
static void fakeRender(id ctx, SEL sel, id i, id t, id c, CGRect b, void *cs)
{ (void)ctx; (void)sel; receivedImage = i; receivedTexture = t; receivedCommand = c; receivedBounds = b; receivedColorSpace = cs; }
@@FUNCTIONS@@
int main(void) { @autoreleasepool {
    nvmtl_photobooth_next = fakeNext; nvmtl_photobooth_render = fakeRender;
    assert(nvmtl_photobooth_process("Photo Booth", @"com.apple.PhotoBooth"));
    assert(!nvmtl_photobooth_process("OBS", @"com.apple.PhotoBooth"));
    assert(!nvmtl_photobooth_process("WindowServer", @"com.apple.PhotoBooth"));
    assert(!nvmtl_photobooth_process("Photo Booth", @"com.example.other"));
    assert(!nvmtl_photobooth_process(NULL, @"com.apple.PhotoBooth"));
    assert(!nvmtl_photobooth_process("Photo Booth", nil));
    unsetenv("NVMTL_PHOTOBOOTH_FLIP"); assert(nvmtl_photobooth_flip_on());
    setenv("NVMTL_PHOTOBOOTH_FLIP", "1", 1); assert(nvmtl_photobooth_flip_on());
    setenv("NVMTL_PHOTOBOOTH_FLIP", "0", 1); assert(!nvmtl_photobooth_flip_on());
    unsetenv("NVMTL_PHOTOBOOTH_FLIP");
    CGRect b = {{13, 17}, {100, 40}}; nvmtl_photobooth_affine tr;
    NMPhotoBoothProbeTexture *offscreen = [NMPhotoBoothProbeTexture new], *preview = [NMPhotoBoothProbeTexture new];
    NSObject *foreign = [NSObject new]; NMPhotoBoothProbeDrawable *drawable = [NMPhotoBoothProbeDrawable new];
    nextResult = drawable; drawable.texture = foreign;
    assert(nvmtl_photobooth_next_drawable(nil, NULL) == drawable);
    assert(!nvmtl_photobooth_reflection(foreign, b, &tr));
    drawable.texture = preview; assert(nvmtl_photobooth_next_drawable(nil, NULL) == drawable);
    assert(nvmtl_photobooth_reflection(preview, b, &tr));
    assert(tr.a == 1 && tr.b == 0 && tr.c == 0 && tr.d == -1 && tr.tx == 0 && tr.ty == 74);
    assert(tr.d * 17 + tr.ty == 57 && tr.d * 57 + tr.ty == 17);   /* the bounds' first and last rows swap */
    assert(!nvmtl_photobooth_reflection(offscreen, b, &tr));
    assert(!nvmtl_photobooth_reflection(nil, b, &tr));
    CGRect invalid[] = {{{0, 0}, {0, 40}}, {{0, 0}, {1, -1}}, {{0, NAN}, {1, 1}}, {{0, 0}, {INFINITY, 1}}, {{0, DBL_MAX}, {1, DBL_MAX}}};
    for (unsigned i = 0; i < sizeof invalid / sizeof *invalid; i++) assert(!nvmtl_photobooth_reflection(preview, invalid[i], &tr));
    NMPhotoBoothProbeImage *image = [NMPhotoBoothProbeImage new]; NSObject *command = [NSObject new];
    void *colorSpace = (void *)(uintptr_t)1;
    nvmtl_photobooth_render_preview(nil, NULL, image, preview, command, b, colorSpace);
    assert(receivedImage != image && ((NMPhotoBoothProbeImage *)receivedImage)->applied.d == -1 && ((NMPhotoBoothProbeImage *)receivedImage)->applied.ty == 74);
    assert(receivedTexture == preview && receivedCommand == command && receivedColorSpace == colorSpace);
    assert(!memcmp(&receivedBounds, &b, sizeof b));
    nvmtl_photobooth_render_preview(nil, NULL, image, offscreen, command, b, colorSpace); assert(receivedImage == image);
    nvmtl_photobooth_render_preview(nil, NULL, image, foreign, command, b, colorSpace); assert(receivedImage == image);
    nvmtl_photobooth_render_preview(nil, NULL, nil, preview, command, b, colorSpace); assert(receivedImage == nil);
    nextResult = nil; assert(nvmtl_photobooth_next_drawable(nil, NULL) == nil);
    Method next = class_getInstanceMethod([NMPhotoBoothProbeLayer class], @selector(nextDrawable));
    Method render = class_getInstanceMethod([NMPhotoBoothProbeContext class], @selector(render:toMTLTexture:commandBuffer:bounds:colorSpace:));
    assert(nvmtl_photobooth_signature(next, NO) && nvmtl_photobooth_signature(render, YES));
    assert(!nvmtl_photobooth_signature(NULL, YES));
    assert(!nvmtl_photobooth_signature(next, YES));
    assert(!nvmtl_photobooth_signature(class_getInstanceMethod([NMPhotoBoothProbeLayer class], @selector(wrongReturn)), NO));
    nextResult = drawable;
    nvmtl_photobooth_replace([NMPhotoBoothProbeChildLayer class], @selector(nextDrawable), next, (IMP)fakeNext);
    assert([[[NMPhotoBoothProbeLayer new] nextDrawable] isEqual:@1] && [[NMPhotoBoothProbeChildLayer new] nextDrawable] == drawable);
    puts("PASS: process identity, the switch, drawable-only marking, reflection within the bounds, pass-through arguments, "
         "invalid bounds, method signatures and inherited-method isolation");
    return 0;
}}
'''


def build_and_run(directory, functions, name):
    src = pathlib.Path(directory) / (name + '.m')
    exe = pathlib.Path(directory) / name
    src.write_text(FIXTURE.replace('@@TYPES@@', TYPES).replace('@@FUNCTIONS@@', functions))
    r = subprocess.run(['xcrun', 'clang', '-fobjc-arc', '-O1', '-g', '-Wall', '-Wextra', '-Wno-unused-function',
                        '-fsanitize=address,undefined', '-fno-sanitize-recover=all', str(src),
                        '-framework', 'Foundation', '-o', str(exe)], capture_output=True, text=True)
    if r.returncode:
        return None, r.stderr[-2500:]
    p = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    return p.returncode, (p.stdout + p.stderr)[-1500:]


def main():
    if not shutil.which('xcrun'):
        print('SKIP: needs xcrun clang with Objective-C (a Mac, or the Linux stand-in environment on PATH)')
        return 2
    with tempfile.TemporaryDirectory(prefix='nm-photobooth-') as d:
        rc, out = build_and_run(d, FUNCTIONS, 'current')
        if rc is None:
            print('FAIL: the fixture does not compile\n' + out)
            return 1
        print(out.strip())
        if rc:
            print('FAIL: the adapter failed a case')
            return 1
        rc, out = build_and_run(d, FUNCTIONS.replace(TRANSFORM, '*transform = (nvmtl_photobooth_affine){ 1, 0, 0, 1, 0, 0 };', 1), 'control')
        if rc is None or rc == 0:
            print('FAIL: control: an identity transform was not refused' + (('\n' + out) if rc is None else ''))
            return 1
        print('ok   control: an identity transform fails')
    return 0


if __name__ == '__main__':
    sys.exit(main())
