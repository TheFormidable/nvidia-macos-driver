/* Framebuffer fetch into IOSurface wrappers of the installed Metal driver, read back by wrappers of their own.
 * Mac only, with the NullMoth driver installed and its card as the system's Metal device. It installs nothing and writes
 * no file: it makes synthetic IOSurfaces of 128x64 and 52x37 pixels in memory (BGRA8, RGBA8, RGBA16Float, RGBA16Unorm),
 * wraps each first read-only and then with the usage under test (Unknown, ShaderRead, ShaderRead|RenderTarget), in
 * Shared and Managed storage, alone and next to a tiled R16Float attachment, and draws three frames into the left half.
 * Each frame clears the wrapper and adds its colour to what framebuffer fetch reads, which must be the clear colour, not
 * a stale earlier frame. After each frame a new Shared wrapper, the first read-only
 * wrapper and the CPU through IOSurfaceLock must all read the same pixels. It reads two fields of the driver's texture
 * objects (_vramOn and the first word of _surfaceLinear) to say which backing each wrapper got.
 *   xcrun clang -fobjc-arc -O2 tools/test_surface_render_formats.m -framework Foundation -framework Metal \
 *     -framework IOSurface -o /tmp/surface-render-formats && /tmp/surface-render-formats
 * Exit 0: every case passed; 1: a case failed (its line says errors=N); 2: no device of this driver, or a setup step
 * failed (the line before says which; a device without framebuffer fetch fails to compile the library).
 * From the HighDelay/nvidia-macos-driver-nvk fork, which reports it passing on an RTX 2060 SUPER, macOS 26.7.1; here it
 * loads the installed driver through MTLCreateSystemDefaultDevice instead of a plugin path, and reads the backing
 * through the two fields above instead of the plugin's header. */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <IOSurface/IOSurface.h>
#import <objc/runtime.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static float component(const void *pixels, NSUInteger n, NSUInteger channel, MTLPixelFormat format) {
    if (format == MTLPixelFormatRGBA16Float) return ((const __fp16 *)pixels)[4*n+channel];
    if (format == MTLPixelFormatRGBA16Unorm) return ((const uint16_t *)pixels)[4*n+channel]/65535.0f;
    if (format == MTLPixelFormatBGRA8Unorm && channel != 1 && channel != 3) channel=2-channel;
    return ((const uint8_t *)pixels)[4*n+channel]/255.0f;
}
// The left half holds the frame's colour over the clear colour; the right half stays cleared (the scissor).
static unsigned verify(const void *pixels, size_t row, NSUInteger width, NSUInteger height, MTLPixelFormat format, unsigned frame) {
    unsigned bad=0;
    for (NSUInteger y=0;y<height;y++) for (NSUInteger x=0;x<width;x++) {
        const void *p=(const uint8_t *)pixels+y*row;
        float expected[]={((x+frame)&3)*0.25f,((y+frame)&3)*0.25f,frame*0.25f,1.0f};
        if (x>=width/2) expected[0]=expected[1]=expected[2]=expected[3]=0;
        for (NSUInteger k=0;k<4;k++) {
            float value=component(p,x,k,format);
            bad+=!isfinite(value)||fabsf(value-expected[k])>0.006f;
        }
    }
    return bad;
}
// a field of the driver's texture object, read as its first byte (BOOL) or first pointer
static const void *field(id object, const char *name) {
    Ivar iv=class_getInstanceVariable([object class],name);
    return iv ? (const char *)(__bridge void *)object+ivar_getOffset(iv) : NULL;
}
int main(void) { @autoreleasepool {
    alarm(120); setvbuf(stdout,NULL,_IOLBF,0);
    id<MTLDevice> dev=MTLCreateSystemDefaultDevice();
    if (!dev || ![NSStringFromClass([dev class]) hasPrefix:@"NVMTL"]) {
        fprintf(stderr,"no Metal device of this driver (got %s)\n",dev ? NSStringFromClass([dev class]).UTF8String : "none");
        return 2;
    }
    printf("device=%s\n",dev.name.UTF8String);
    NSError *error=nil;
    // The fragment adds its colour to the pixel framebuffer fetch reads: a stale earlier frame shows in the result.
    id<MTLLibrary> lib=[dev newLibraryWithSource:@"#include <metal_stdlib>\nusing namespace metal;\n"
        "vertex float4 v(uint i [[vertex_id]]) { float2 p=float2((i<<1)&2,i&2); return float4(p*2.0-1.0,0,1); }\n"
        "fragment float4 f(float4 pos [[position]], constant uint &frame [[buffer(0)]], float4 previous [[color(0)]]) { uint2 p=uint2(pos.xy);"
        " return previous+float4(float((p.x+frame)&3)*0.25,float((p.y+frame)&3)*0.25,float(frame)*0.25,1); }\n"
        options:nil error:&error];
    if (!lib) { fprintf(stderr,"library: %s\n",error.description.UTF8String); return 2; }
    id<MTLCommandQueue> queue=[dev newCommandQueue];
    const MTLPixelFormat formats[]={MTLPixelFormatBGRA8Unorm,MTLPixelFormatRGBA8Unorm,MTLPixelFormatRGBA16Float,MTLPixelFormatRGBA16Unorm};
    const uint32_t surfaceFormats[]={0x42475241,0x52474241,0x52476841,0x6c363472};   // BGRA, RGBA, RGhA, l64r
    const MTLTextureUsage usages[]={MTLTextureUsageUnknown,MTLTextureUsageShaderRead,MTLTextureUsageShaderRead|MTLTextureUsageRenderTarget};
    unsigned failures=0,cases=0;
    for (unsigned fi=0;fi<4;fi++) for (unsigned ui=0;ui<3;ui++)
    for (unsigned managed=0;managed<2;managed++) for (unsigned mixed=0;mixed<2;mixed++)
    for (unsigned padded=0;padded<2;padded++) { @autoreleasepool {
        const NSUInteger width=padded?52:128,height=padded?37:64;
        const size_t bpp=fi<2?4:8,row=(width*bpp+63)&~(size_t)63;
        IOSurfaceRef surface=IOSurfaceCreate((__bridge CFDictionaryRef)@{(id)kIOSurfaceWidth:@(width),(id)kIOSurfaceHeight:@(height),
            (id)kIOSurfaceBytesPerElement:@(bpp),(id)kIOSurfaceBytesPerRow:@(row),(id)kIOSurfaceAllocSize:@(row*height),
            (id)kIOSurfacePixelFormat:@(surfaceFormats[fi])});
        if (!surface) { fprintf(stderr,"IOSurfaceCreate failed\n"); return 2; }
        IOSurfaceLock(surface,0,NULL); memset(IOSurfaceGetBaseAddress(surface),0x55,row*height); IOSurfaceUnlock(surface,0,NULL);
        MTLTextureDescriptor *desc=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:formats[fi] width:width height:height mipmapped:NO];
        desc.storageMode=managed?MTLStorageModeManaged:MTLStorageModeShared; desc.usage=MTLTextureUsageShaderRead;
        // the first wrapper is read-only; the one drawn into comes after it, with the usage under test
        id<MTLTexture> first=[dev newTextureWithDescriptor:desc iosurface:surface plane:0];
        desc.usage=usages[ui];
        id<MTLTexture> target=[dev newTextureWithDescriptor:desc iosurface:surface plane:0];
        MTLRenderPipelineDescriptor *pd=[MTLRenderPipelineDescriptor new];
        pd.vertexFunction=[lib newFunctionWithName:@"v"]; pd.fragmentFunction=[lib newFunctionWithName:@"f"];
        pd.colorAttachments[0].pixelFormat=formats[fi];
        MTLTextureDescriptor *auxDesc=[MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatR16Float width:width height:height mipmapped:NO];
        auxDesc.storageMode=MTLStorageModePrivate; auxDesc.usage=MTLTextureUsageRenderTarget;
        id<MTLTexture> aux=[dev newTextureWithDescriptor:auxDesc];
        pd.colorAttachments[1].pixelFormat=mixed?MTLPixelFormatR16Float:MTLPixelFormatInvalid; pd.colorAttachments[1].writeMask=MTLColorWriteMaskNone;
        id<MTLRenderPipelineState> pipe=[dev newRenderPipelineStateWithDescriptor:pd error:&error];
        if (!first||!target||!aux||!pipe) { fprintf(stderr,"setup: %s\n",error.description.UTF8String); return 2; }
        const unsigned char *vram=field(target,"_vramOn"); void *const *linear=field(target,"_surfaceLinear");
        const int direct=vram&&*vram&&!(linear&&*linear), tiled=vram&&*vram&&linear&&*linear;
        uint8_t *read=malloc(row*height); unsigned bad=0;
        if (!read) return 2;
        for (unsigned frame=1;frame<=3;frame++) { @autoreleasepool {
            MTLRenderPassDescriptor *pass=[MTLRenderPassDescriptor renderPassDescriptor];
            pass.colorAttachments[0].texture=target;
            pass.colorAttachments[0].loadAction=MTLLoadActionClear;
            pass.colorAttachments[0].storeAction=MTLStoreActionStore; pass.colorAttachments[0].clearColor=MTLClearColorMake(0,0,0,0);
            pass.colorAttachments[1].texture=mixed?aux:nil; pass.colorAttachments[1].loadAction=MTLLoadActionClear;
            pass.colorAttachments[1].storeAction=MTLStoreActionDontCare;
            id<MTLCommandBuffer> cb=[queue commandBuffer];
            id<MTLRenderCommandEncoder> enc=[cb renderCommandEncoderWithDescriptor:pass];
            [enc setRenderPipelineState:pipe]; [enc setFragmentBytes:&frame length:sizeof frame atIndex:0];
            [enc setScissorRect:(MTLScissorRect){0,0,width/2,height}];
            [enc drawPrimitives:MTLPrimitiveTypeTriangle vertexStart:0 vertexCount:3]; [enc endEncoding];
            [cb commit]; [cb waitUntilCompleted];
            if (cb.status!=MTLCommandBufferStatusCompleted) { fprintf(stderr,"command: %s\n",cb.error.description.UTF8String); return 2; }
            // a Shared wrapper made now reads what was published into the surface, not the Managed wrapper's own image
            MTLTextureDescriptor *observer=[desc copy]; observer.storageMode=MTLStorageModeShared; observer.usage=MTLTextureUsageShaderRead;
            id<MTLTexture> external=[dev newTextureWithDescriptor:observer iosurface:surface plane:0];
            if (!external) { fprintf(stderr,"observer wrapper failed\n"); return 2; }
            [external getBytes:read bytesPerRow:row fromRegion:MTLRegionMake2D(0,0,width,height) mipmapLevel:0];
            bad+=verify(read,row,width,height,formats[fi],frame);
            [first getBytes:read bytesPerRow:row fromRegion:MTLRegionMake2D(0,0,width,height) mipmapLevel:0];
            bad+=verify(read,row,width,height,formats[fi],frame);
            if (IOSurfaceLock(surface,kIOSurfaceLockReadOnly,NULL)) { fprintf(stderr,"IOSurfaceLock failed\n"); return 2; }
            bad+=verify(IOSurfaceGetBaseAddress(surface),IOSurfaceGetBytesPerRow(surface),width,height,formats[fi],frame);
            IOSurfaceUnlock(surface,kIOSurfaceLockReadOnly,NULL);
        }}
        printf("format=%lu usage=0x%lx managed=%u mixed=%u padded=%u backing=%s frames=3 errors=%u\n",(unsigned long)formats[fi],
               (unsigned long)usages[ui],managed,mixed,padded,tiled?"tiled-over-vram":direct?"vram":"private",bad);
        failures+=!!bad; cases++; free(read); CFRelease(surface);
    }}
    printf("%s: %u cases, %u failures\n",failures?"FAIL":"PASS",cases,failures);
    return failures?1:0;
}}
