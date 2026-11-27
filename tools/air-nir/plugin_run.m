// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
//
// plugin_run.m: the kernels of tools/air-nir/metal through the Metal plugin's own compute path, without installing it.
//
// usage: plugin_run PLUGIN NVK.dylib AIRDIR OUTDIR [TEST...]
//
// PLUGIN is a development build of the plugin (build/build_plugin_dev.sh) in a bundle folder of its own whose MacOS
// folder also holds the installed bundle's air-runtime and translator (read only, by link); NVK.dylib a build of NVK
// (build_nvk.sh's out dir). AIRDIR holds each test's <air>.metallib. The plugin is opened in this process and its
// NVMTLDevice made over the NVAccel service, as the card programs of tools/ do with a selected driver (the system's
// default device is never asked for, so the installed plugin is not loaded); the device opens the NVK named
// (NVMTL_ICD_LIB). Its caches go to OUTDIR (NVMTL_AIRCACHE: the AIR cache, and the shader cache beside it), Mesa's disk
// cache is off, and its log is OUTDIR/driver.log. NVMTL_AIR_NIR in this program's environment picks the road (unset:
// AIR to NIR first; 0: SPIR-V only); PLUGIN_RUN_CACHE=<an earlier OUTDIR> runs with that run's caches instead. A plugin
// from before the road (no _road in its pipeline states) is run as all SPIR-V.
//
// For each test (airrun_tests.h, shared with airrun.c): the library from its metallib, the function (with its
// function-constant values), the pipeline state (the road it took is read from the pipeline state's _road), the
// buffers, textures and samplers the setup makes, one dispatch through a compute encoder (indirect for grid2d_indirect),
// the written textures copied back, and the outputs checked against the CPU's reference. Then the same dispatch 21 more
// times, one after the other in one command buffer and on copies of its buffers in video memory, for its GPU time: the
// plugin's per-dispatch timestamp queries (NVMTL_DISPATCH_TIME, OUTDIR/nvmtl-disptime-<pid>.log). Every command buffer is waited for at most 2 s, and NVRM's four fault counters are
// read around every test: if one rises, or a wait runs out, no more GPU work is done (exit 3).
//
// A test that airrun requires the frontend to refuse passes here when its pipeline took the SPIR-V road (or the plugin
// made no pipeline of it). grid2d_base is not run: Metal has no dispatch base (the plugin sets a base itself, for the
// partial threadgroups of dispatchThreads). PLUGIN_RUN_CORRUPT=1 changes a byte of every output before it is checked:
// each test that dispatches must then fail.
//
// Output: one line per test on stdout, and OUTDIR/results.tsv: test, road (nir, spirv, none), outcome, milliseconds to
// make the library, the function and the pipeline state, the dispatch's GPU time in microseconds (median and max of the
// timed runs) and their count, the first mismatch.
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <IOKit/IOKitLib.h>
#import <objc/message.h>
#import <objc/runtime.h>
#include <dlfcn.h>
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/sysctl.h>
#include <unistd.h>
#include <vulkan/vulkan_core.h>   // the format, view-type and sampler enums the tests name

#include "nvmtl_air_nir_abi.h"

#define TIMED_RUNS 21

static id<MTLDevice> dev;
static id<MTLCommandQueue> queue;

/* ---- what airrun_tests.h needs ---------------------------------------------------------------------------------- */

struct buf {
   id<MTLBuffer> b;
   void *map;
   size_t size;
};

struct timg {
   id<MTLTexture> tex;
   struct buf stage;   /* level after level, each with all its layers (or depth slices) */
   VkFormat fmt;
   VkImageViewType vtype;
   uint32_t w, h, d, layers, mips, texel;
   bool storage, depth, is_buffer;
};

struct test;
struct run {
   const struct test *t;
   struct buf slot[NVMTL_AIR_NIR_MAX_BUFFERS];
   struct timg tex[16];
   id<MTLSamplerState> samp[32];
   unsigned threads;
   char why[256];
};

static struct buf
buf_make(size_t size)
{
   struct buf b = { .size = size };
   if (size > (16u << 20)) {
      fprintf(stderr, "plugin_run: a buffer of %zu bytes is over the 16 MB limit\n", size);
      exit(2);
   }
   b.b = [dev newBufferWithLength:size ? size : 16 options:MTLResourceStorageModeShared];
   if (!b.b) {
      fprintf(stderr, "plugin_run: newBufferWithLength:%zu failed\n", size);
      exit(2);
   }
   b.map = b.b.contents;
   memset(b.map, 0, size);
   return b;
}

static void *
slot(struct run *r, unsigned i, size_t bytes)
{
   r->slot[i] = buf_make(bytes);
   return r->slot[i].map;
}

static uint32_t
mip_size(uint32_t v, uint32_t m)
{
   return v >> m ? v >> m : 1;
}

static size_t
tex_level_offset(const struct timg *x, uint32_t m)
{
   size_t off = 0;
   for (uint32_t k = 0; k < m; k++)
      off += (size_t)mip_size(x->w, k) * mip_size(x->h, k) * mip_size(x->d, k) * x->layers * x->texel;
   return off;
}

static void *
tex_at(struct run *r, unsigned i, uint32_t m, uint32_t x, uint32_t y, uint32_t zl)
{
   struct timg *t = &r->tex[i];
   uint32_t w = mip_size(t->w, m), h = mip_size(t->h, m);
   return (uint8_t *)t->stage.map + tex_level_offset(t, m) + (((size_t)zl * h + y) * w + x) * t->texel;
}

static MTLPixelFormat
mtl_format(VkFormat f)
{
   switch (f) {
   case VK_FORMAT_R8G8B8A8_UNORM: return MTLPixelFormatRGBA8Unorm;
   case VK_FORMAT_R8_UNORM: return MTLPixelFormatR8Unorm;
   case VK_FORMAT_R16G16B16A16_SFLOAT: return MTLPixelFormatRGBA16Float;
   case VK_FORMAT_R32_SFLOAT: return MTLPixelFormatR32Float;
   case VK_FORMAT_R32_UINT: return MTLPixelFormatR32Uint;
   case VK_FORMAT_D32_SFLOAT: return MTLPixelFormatDepth32Float;
   default:
      fprintf(stderr, "plugin_run: Vulkan format %d has no Metal format here\n", (int)f);
      exit(2);
   }
}

static void
tex_image(struct run *r, unsigned i, VkImageViewType vt, VkFormat fmt, uint32_t texel, uint32_t w, uint32_t h, uint32_t d,
          uint32_t layers, uint32_t mips, bool storage)
{
   struct timg *t = &r->tex[i];
   memset(t, 0, sizeof *t);
   t->fmt = fmt;
   t->vtype = vt;
   t->w = w;
   t->h = h;
   t->d = d;
   t->layers = layers;
   t->mips = mips;
   t->texel = texel;
   t->storage = storage;
   t->depth = fmt == VK_FORMAT_D32_SFLOAT;
   MTLTextureDescriptor *td = [MTLTextureDescriptor new];
   switch (vt) {
   case VK_IMAGE_VIEW_TYPE_2D: td.textureType = MTLTextureType2D; break;
   case VK_IMAGE_VIEW_TYPE_2D_ARRAY: td.textureType = MTLTextureType2DArray; td.arrayLength = layers; break;
   case VK_IMAGE_VIEW_TYPE_3D: td.textureType = MTLTextureType3D; td.depth = d; break;
   case VK_IMAGE_VIEW_TYPE_CUBE: td.textureType = MTLTextureTypeCube; break;
   default:
      fprintf(stderr, "plugin_run: view type %d has no Metal texture type here\n", (int)vt);
      exit(2);
   }
   td.pixelFormat = mtl_format(fmt);
   td.width = w;
   td.height = h;
   td.mipmapLevelCount = mips;
   td.usage = MTLTextureUsageShaderRead | (storage ? MTLTextureUsageShaderWrite : 0);
   td.storageMode = MTLStorageModePrivate;
   t->tex = [dev newTextureWithDescriptor:td];
   [td release];
   if (!t->tex) {
      fprintf(stderr, "plugin_run: newTextureWithDescriptor failed (texture %u)\n", i);
      exit(2);
   }
   t->stage = buf_make(tex_level_offset(t, mips));
}

static void *
tex_buffer(struct run *r, unsigned i, VkFormat fmt, uint32_t texel, uint32_t n, bool storage)
{
   struct timg *t = &r->tex[i];
   memset(t, 0, sizeof *t);
   t->fmt = fmt;
   t->texel = texel;
   t->w = n;
   t->h = t->d = t->layers = t->mips = 1;
   t->storage = storage;
   t->is_buffer = true;
   t->stage = buf_make((size_t)n * texel);
   MTLTextureDescriptor *td = [MTLTextureDescriptor textureBufferDescriptorWithPixelFormat:mtl_format(fmt) width:n
                                 resourceOptions:MTLResourceStorageModeShared
                                           usage:MTLTextureUsageShaderRead | (storage ? MTLTextureUsageShaderWrite : 0)];
   t->tex = [t->stage.b newTextureWithDescriptor:td offset:0 bytesPerRow:(NSUInteger)n * texel];
   if (!t->tex) {
      fprintf(stderr, "plugin_run: texture buffer %u failed\n", i);
      exit(2);
   }
   return t->stage.map;
}

static MTLSamplerAddressMode
mtl_address(VkSamplerAddressMode a)
{
   switch (a) {
   case VK_SAMPLER_ADDRESS_MODE_REPEAT: return MTLSamplerAddressModeRepeat;
   case VK_SAMPLER_ADDRESS_MODE_MIRRORED_REPEAT: return MTLSamplerAddressModeMirrorRepeat;
   case VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER: return MTLSamplerAddressModeClampToZero;
   default: return MTLSamplerAddressModeClampToEdge;
   }
}

static void
samp_create(struct run *r, unsigned slot_, VkFilter filter, VkSamplerAddressMode address, VkSamplerMipmapMode mip, float max_lod)
{
   MTLSamplerDescriptor *sd = [MTLSamplerDescriptor new];
   sd.minFilter = sd.magFilter = filter == VK_FILTER_LINEAR ? MTLSamplerMinMagFilterLinear : MTLSamplerMinMagFilterNearest;
   sd.mipFilter = mip == VK_SAMPLER_MIPMAP_MODE_LINEAR ? MTLSamplerMipFilterLinear : MTLSamplerMipFilterNearest;
   sd.sAddressMode = sd.tAddressMode = sd.rAddressMode = mtl_address(address);
   sd.lodMinClamp = 0;
   sd.lodMaxClamp = max_lod;
   sd.normalizedCoordinates = YES;
   r->samp[slot_] = [dev newSamplerStateWithDescriptor:sd];
   [sd release];
}

#include "airrun_tests.h"

/* ---- NVRM's fault counters ------------------------------------------------------------------------------------- */

static const char *const counter_names[] = {
   "debug.nvrm_xid_count", "debug.nvrm_chan_recovered", "debug.nvrm_chan_gave_up", "debug.nvrm_chan_dropped",
};
#define NCOUNTERS 4

static void
read_counters(int64_t *v)
{
   for (unsigned i = 0; i < NCOUNTERS; i++) {
      uint64_t raw = 0;
      size_t len = sizeof raw;
      v[i] = sysctlbyname(counter_names[i], &raw, &len, NULL, 0) ? -1 : len == 4 ? (int64_t)(int32_t)(uint32_t)raw : (int64_t)raw;
   }
}

static void
print_counters(const char *when, const int64_t *v)
{
   printf("counters %s:", when);
   for (unsigned i = 0; i < NCOUNTERS; i++)
      printf(" %s=%lld", counter_names[i] + 6, (long long)v[i]);
   printf("\n");
   fflush(stdout);
}

/* ---- one test ---------------------------------------------------------------------------------------------------- */

static double
ms_since(uint64_t t0)
{
   return (double)(clock_gettime_nsec_np(CLOCK_UPTIME_RAW) - t0) / 1e6;
}

/* commit and wait at most 2 s; a wait that runs out, a failed command buffer or a counter that rose ends all GPU work */
static bool
submit(id<MTLCommandBuffer> cb, const char *name, char *why, size_t whylen)
{
   int64_t before[NCOUNTERS], after[NCOUNTERS];
   read_counters(before);
   dispatch_semaphore_t done = dispatch_semaphore_create(0);
   [cb addCompletedHandler:^(id<MTLCommandBuffer> c) {
      (void)c;
      dispatch_semaphore_signal(done);
   }];
   [cb commit];
   const long timed_out = dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC));
   read_counters(after);
   bool rose = false;
   for (unsigned i = 0; i < NCOUNTERS; i++)
      rose |= after[i] != before[i];
   if (timed_out || rose) {
      printf("%-16s STOP     %s; no more GPU work\n", name, timed_out ? "the command buffer did not complete in 2 s" : "a fault counter rose");
      print_counters("before", before);
      print_counters("after", after);
      _exit(3);
   }
   if (cb.status != MTLCommandBufferStatusCompleted) {
      snprintf(why, whylen, "command buffer status %ld: %s", (long)cb.status, cb.error.localizedDescription.UTF8String ?: "?");
      return false;
   }
   return true;
}

static void
encode(id<MTLCommandBuffer> cb, const struct test *t, struct run *r, id<MTLComputePipelineState> ps, id<MTLBuffer> ind,
       id<MTLBuffer> const *bufs)
{
   id<MTLComputeCommandEncoder> ce = [cb computeCommandEncoder];
   [ce setComputePipelineState:ps];
   for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++)
      if (r->slot[i].b)
         [ce setBuffer:bufs && bufs[i] ? bufs[i] : r->slot[i].b offset:0 atIndex:i];
   for (unsigned i = 0; i < 16; i++)
      if (r->tex[i].tex)
         [ce setTexture:r->tex[i].tex atIndex:i];
   for (unsigned i = 0; i < 32; i++)
      if (r->samp[i])
         [ce setSamplerState:r->samp[i] atIndex:i];
   for (unsigned i = 0; i < 2; i++)
      if (t->tg_len[i])
         [ce setThreadgroupMemoryLength:t->tg_len[i] atIndex:i];
   MTLSize tg = MTLSizeMake(t->tg[0], t->tg[1], t->tg[2]);
   if (ind)
      [ce dispatchThreadgroupsWithIndirectBuffer:ind indirectBufferOffset:0 threadsPerThreadgroup:tg];
   else
      [ce dispatchThreadgroups:MTLSizeMake(t->groups[0], t->groups[1], t->groups[2]) threadsPerThreadgroup:tg];
   [ce endEncoding];
}

/* the GPU times of a kernel's dispatches in the plugin's timestamp log, after the first (the checked run) */
static unsigned
gpu_times(const char *log, const char *entry, long skip_lines, double *med, double *max)
{
   FILE *f = fopen(log, "r");
   double v[4 * TIMED_RUNS];
   unsigned n = 0, seen = 0;
   char line[512];
   long at = 0;
   while (f && fgets(line, sizeof line, f)) {
      if (at++ < skip_lines)
         continue;
      char name[160];
      double us;
      const char *g = strstr(line, " gpu_us ");
      if (!g || sscanf(line, "cb %*u d %*u %159s", name) != 1 || strcmp(name, entry) || sscanf(g, " gpu_us %lf", &us) != 1)
         continue;
      if (seen++ == 0)
         continue;
      if (n < sizeof v / sizeof *v)
         v[n++] = us;
   }
   if (f)
      fclose(f);
   *med = *max = 0;
   if (!n)
      return 0;
   for (unsigned i = 1; i < n; i++)
      for (unsigned j = i; j && v[j - 1] > v[j]; j--) {
         double x = v[j];
         v[j] = v[j - 1];
         v[j - 1] = x;
      }
   *med = v[n / 2];
   *max = v[n - 1];
   return n;
}

static long
count_lines(const char *path)
{
   FILE *f = fopen(path, "r");
   long n = 0;
   int c;
   while (f && (c = fgetc(f)) != EOF)
      n += c == '\n';
   if (f)
      fclose(f);
   return n;
}

static const char *air_dir, *out_dir;
static bool corrupt;
static char disptime[1024];
static FILE *tsv;
static Ivar road_ivar;

enum outcome { PASS, FAIL, SKIPPED };

static enum outcome
run_test(const struct test *t)
{ @autoreleasepool {
   char path[2048], why[256] = "";
   const char *name = t->name;
   if (t->flags & GRID_BASE) {
      printf("%-16s SKIPPED  Metal has no dispatch base\n", name);
      fprintf(tsv, "%s\tnone\tSKIPPED\t\t\t\t\t\t0\tMetal has no dispatch base\n", name);
      return SKIPPED;
   }
   snprintf(path, sizeof path, "%s/%s.metallib", air_dir, t->air ? t->air : t->name);
   NSError *e = nil;
   uint64_t t0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
   id<MTLLibrary> lib = [dev newLibraryWithURL:[NSURL fileURLWithPath:@(path)] error:&e];
   const double lib_ms = ms_since(t0);
   if (!lib) {
      printf("%-16s FAIL     no library from %s: %s\n", name, path, e.localizedDescription.UTF8String ?: "?");
      fprintf(tsv, "%s\tnone\tFAIL\t\t\t\t\t\t0\tno library\n", name);
      return FAIL;
   }
   t0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
   id<MTLFunction> fn = nil;
   if (t->fc) {
      MTLFunctionConstantValues *cv = [MTLFunctionConstantValues new];
      for (unsigned i = 0; i < t->fc->n; i++)   /* the plugin hands over the bytes; only their width follows the type */
         [cv setConstantValue:t->fc->v[i].bytes type:t->fc->v[i].size == 1 ? MTLDataTypeBool : MTLDataTypeUInt atIndex:t->fc->v[i].index];
      fn = [lib newFunctionWithName:@(t->entry) constantValues:cv error:&e];
      [cv release];
   } else
      fn = [lib newFunctionWithName:@(t->entry)];
   const double fn_ms = ms_since(t0);
   id<MTLComputePipelineState> ps = nil;
   double ps_ms = 0;
   if (fn) {
      t0 = clock_gettime_nsec_np(CLOCK_UPTIME_RAW);
      ps = [dev newComputePipelineStateWithFunction:fn error:&e];
      ps_ms = ms_since(t0);
   }
   const int road = ps && road_ivar ? *((const uint8_t *)(__bridge void *)ps + ivar_getOffset(road_ivar)) : ps ? 0 : -1;
   const char *road_name = road == 1 ? "nir" : road == 0 ? "spirv" : "none";
   if (t->flags & MUST_REFUSE) {
      const bool ok = road != 1;
      printf("%-16s %s road %s (the frontend must refuse it)\n", name, ok ? "PASS    " : "FAIL    ", road_name);
      fprintf(tsv, "%s\t%s\t%s\t%.3f\t%.3f\t%.3f\t\t\t0\t%s\n", name, road_name, ok ? "PASS" : "FAIL", lib_ms, fn_ms, ps_ms,
              ok ? "" : "took the AIR-to-NIR road");
      [ps release]; [fn release]; [lib release];
      return ok ? PASS : FAIL;
   }
   if (!ps) {
      printf("%-16s FAIL     no pipeline: %s\n", name, e.localizedDescription.UTF8String ?: (fn ? "?" : "no function"));
      fprintf(tsv, "%s\tnone\tFAIL\t%.3f\t%.3f\t%.3f\t\t\t0\tno pipeline\n", name, lib_ms, fn_ms, ps_ms);
      [fn release]; [lib release];
      return FAIL;
   }

   struct run r;
   memset(&r, 0, sizeof r);
   r.t = t;
   r.threads = t->tg[0] * t->tg[1] * t->tg[2] * t->groups[0] * t->groups[1] * t->groups[2];
   rng_state = 0x9e3779b9u ^ (uint32_t)strlen(t->name) * 2654435761u;
   t->setup(&r);
   id<MTLBuffer> ind = nil;
   if (t->flags & GRID_INDIRECT) {
      ind = [dev newBufferWithLength:16 options:MTLResourceStorageModeShared];
      uint32_t *g = ind.contents;
      g[0] = t->groups[0];
      g[1] = t->groups[1];
      g[2] = t->groups[2];
   }

   id<MTLCommandBuffer> cb = [queue commandBuffer];
   bool any = false;
   for (unsigned i = 0; i < 16; i++)
      any |= r.tex[i].tex && !r.tex[i].is_buffer;
   if (any) {   /* uploads: each level and slice from its part of the staging copy */
      id<MTLBlitCommandEncoder> be = [cb blitCommandEncoder];
      for (unsigned i = 0; i < 16; i++) {
         struct timg *x = &r.tex[i];
         if (!x->tex || x->is_buffer)
            continue;
         const bool is3d = x->vtype == VK_IMAGE_VIEW_TYPE_3D;
         for (uint32_t m = 0; m < x->mips; m++) {
            const uint32_t w = mip_size(x->w, m), h = mip_size(x->h, m), d = is3d ? mip_size(x->d, m) : 1;
            for (uint32_t s = 0; s < (is3d ? 1 : x->layers); s++)
               [be copyFromBuffer:x->stage.b sourceOffset:tex_level_offset(x, m) + (size_t)s * w * h * x->texel
                  sourceBytesPerRow:(NSUInteger)w * x->texel sourceBytesPerImage:(NSUInteger)w * h * x->texel
                         sourceSize:MTLSizeMake(w, h, d) toTexture:x->tex destinationSlice:s destinationLevel:m
                  destinationOrigin:MTLOriginMake(0, 0, 0)];
         }
      }
      [be endEncoding];
   }
   encode(cb, t, &r, ps, ind, NULL);
   any = false;
   for (unsigned i = 0; i < 16; i++)
      any |= r.tex[i].tex && !r.tex[i].is_buffer && r.tex[i].storage;
   if (any) {   /* written textures: copied back into their staging copy */
      id<MTLBlitCommandEncoder> be = [cb blitCommandEncoder];
      for (unsigned i = 0; i < 16; i++) {
         struct timg *x = &r.tex[i];
         if (!x->tex || x->is_buffer || !x->storage)
            continue;
         const bool is3d = x->vtype == VK_IMAGE_VIEW_TYPE_3D;
         for (uint32_t m = 0; m < x->mips; m++) {
            const uint32_t w = mip_size(x->w, m), h = mip_size(x->h, m), d = is3d ? mip_size(x->d, m) : 1;
            for (uint32_t s = 0; s < (is3d ? 1 : x->layers); s++)
               [be copyFromTexture:x->tex sourceSlice:s sourceLevel:m sourceOrigin:MTLOriginMake(0, 0, 0) sourceSize:MTLSizeMake(w, h, d)
                          toBuffer:x->stage.b destinationOffset:tex_level_offset(x, m) + (size_t)s * w * h * x->texel
             destinationBytesPerRow:(NSUInteger)w * x->texel destinationBytesPerImage:(NSUInteger)w * h * x->texel];
         }
      }
      [be endEncoding];
   }
   const long skip = count_lines(disptime);
   bool ok = submit(cb, name, why, sizeof why);
   if (ok && corrupt) {
      for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++)
         if (r.slot[i].map)
            ((uint8_t *)r.slot[i].map)[3] ^= 0x40;
      for (unsigned i = 0; i < 16; i++)
         if (r.tex[i].stage.map)
            ((uint8_t *)r.tex[i].stage.map)[3] ^= 0x40;
   }
   if (ok) {
      ok = t->check(&r);
      if (!ok)
         snprintf(why, sizeof why, "%s", r.why);
   }
   /* the GPU time of the same dispatch: TIMED_RUNS more, one after the other in one command buffer, so that the GPU
    * does not idle between them */
   if (!corrupt) {
      @autoreleasepool {
         /* the buffers in video memory, copies of the checked run's: the kernel's time, not system memory's latency */
         id<MTLBuffer> priv[NVMTL_AIR_NIR_MAX_BUFFERS] = { nil };
         id<MTLCommandBuffer> tcb = [queue commandBuffer];
         id<MTLBlitCommandEncoder> be = [tcb blitCommandEncoder];
         for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++) {
            if (!r.slot[i].b)
               continue;
            priv[i] = [dev newBufferWithLength:r.slot[i].b.length options:MTLResourceStorageModePrivate];
            if (priv[i])
               [be copyFromBuffer:r.slot[i].b sourceOffset:0 toBuffer:priv[i] destinationOffset:0 size:r.slot[i].b.length];
         }
         [be endEncoding];
         for (unsigned k = 0; k < TIMED_RUNS; k++)
            encode(tcb, t, &r, ps, ind, priv);
         char w2[256];
         (void)submit(tcb, name, w2, sizeof w2);
         for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++)
            [priv[i] release];
      }
   }
   double med = 0, max = 0;
   const unsigned n = corrupt ? 0 : gpu_times(disptime, t->entry, skip, &med, &max);
   printf("%-16s %s road %-5s library %.2f ms, function %.2f ms, pipeline %.2f ms, GPU %.2f us (max %.2f, %u runs)%s%s\n", name,
          ok ? "PASS    " : "FAIL    ", road_name, lib_ms, fn_ms, ps_ms, med, max, n, ok ? "" : "  ", ok ? "" : why);
   fprintf(tsv, "%s\t%s\t%s\t%.3f\t%.3f\t%.3f\t%.2f\t%.2f\t%u\t%s\n", name, road_name, ok ? "PASS" : "FAIL", lib_ms, fn_ms, ps_ms, med,
           max, n, ok ? "" : why);
   fflush(stdout);
   fflush(tsv);
   for (unsigned i = 0; i < NVMTL_AIR_NIR_MAX_BUFFERS; i++)
      [r.slot[i].b release];
   for (unsigned i = 0; i < 16; i++) {
      [r.tex[i].tex release];
      [r.tex[i].stage.b release];
   }
   for (unsigned i = 0; i < 32; i++)
      [r.samp[i] release];
   [ind release];
   [ps release];
   [fn release];
   [lib release];
   return ok ? PASS : FAIL;
} }

@protocol PluginDevice <MTLDevice>
- (instancetype)initWithAcceleratorPort:(mach_port_t)port;
@end

int
main(int argc, char **argv)
{ @autoreleasepool {
   setvbuf(stdout, NULL, _IOLBF, 0);
   if (argc < 5) {
      fprintf(stderr, "usage: plugin_run PLUGIN NVK.dylib AIRDIR OUTDIR [TEST...]\n");
      return 2;
   }
   air_dir = argv[3];
   out_dir = argv[4];
   corrupt = getenv("PLUGIN_RUN_CORRUPT") && getenv("PLUGIN_RUN_CORRUPT")[0] == '1';
   char p[1024];
   setenv("NVMTL_PROCESS_ENV", "1", 1);
   setenv("NVMTL_ENABLE", "1", 1);
   setenv("NVMTL_ICD_LIB", argv[2], 1);
   snprintf(p, sizeof p, "%s/driver.log", out_dir);
   setenv("NVMTL_LOG_PATH", p, 1);
   /* PLUGIN_RUN_CACHE=<dir>: the caches of an earlier run (its OUTDIR), to measure a second run of the same kernels */
   snprintf(p, sizeof p, "%s/aircache", getenv("PLUGIN_RUN_CACHE") ? getenv("PLUGIN_RUN_CACHE") : out_dir);
   setenv("NVMTL_AIRCACHE", p, 1);
   setenv("MESA_SHADER_CACHE_DISABLE", "true", 1);
   setenv("NVMTL_DISPATCH_TIME", "1", 1);
   setenv("TMPDIR", out_dir, 1);
   snprintf(disptime, sizeof disptime, "%s/nvmtl-disptime-%d.log", out_dir, (int)getpid());
   snprintf(p, sizeof p, "%s/results.tsv", out_dir);
   tsv = fopen(p, "w");
   if (!tsv) {
      fprintf(stderr, "plugin_run: cannot write %s\n", p);
      return 2;
   }
   fprintf(tsv, "test\troad\toutcome\tlibrary_ms\tfunction_ms\tpipeline_ms\tgpu_us_median\tgpu_us_max\truns\twhy\n");

   if (objc_lookUpClass("NVMTLDevice")) {
      fprintf(stderr, "plugin_run: a Metal plugin is already loaded in this process\n");
      return 2;
   }
   if (!dlopen(argv[1], RTLD_NOW | RTLD_LOCAL)) {
      fprintf(stderr, "plugin_run: dlopen %s: %s\n", argv[1], dlerror());
      return 2;
   }
   Class cls = objc_lookUpClass("NVMTLDevice");
   Class pscls = objc_lookUpClass("NVMTLComputePipelineState");
   road_ivar = pscls ? class_getInstanceVariable(pscls, "_road") : NULL;   /* a plugin before the road has none: "none" */
   if (!cls) {
      fprintf(stderr, "plugin_run: %s has no NVMTLDevice\n", argv[1]);
      return 2;
   }
   io_service_t svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("NVAccel"));
   if (!svc) {
      fprintf(stderr, "plugin_run: no NVAccel service\n");
      return 2;
   }
   int64_t c0[NCOUNTERS], c1[NCOUNTERS];
   read_counters(c0);
   print_counters("at start", c0);
   dev = [(id<PluginDevice>)[cls alloc] initWithAcceleratorPort:svc];
   IOObjectRelease(svc);
   if (!dev) {
      fprintf(stderr, "plugin_run: the plugin made no device\n");
      return 2;
   }
   queue = [dev newCommandQueue];
   printf("device %s, NVMTL_AIR_NIR=%s, corrupt %d\n", dev.name.UTF8String, getenv("NVMTL_AIR_NIR") ?: "unset", corrupt);

   unsigned pass = 0, fail = 0, skipped = 0;
   for (unsigned i = 0; i < sizeof tests / sizeof *tests; i++) {
      bool want = argc == 5;
      for (int a = 5; a < argc; a++)
         want |= !strcmp(argv[a], tests[i].name);
      if (!want)
         continue;
      switch (run_test(&tests[i])) {
      case PASS: pass++; break;
      case FAIL: fail++; break;
      case SKIPPED: skipped++; break;
      }
   }
   read_counters(c1);
   print_counters("at end", c1);
   printf("pass %u, fail %u, skipped %u\n", pass, fail, skipped);
   fclose(tsv);
   return fail ? 1 : 0;
} }
