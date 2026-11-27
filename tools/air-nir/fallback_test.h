// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
//
// fallback_test.h: the AIR-to-NIR road's per-process fallback on GPU faults (plugin/NVMTLAirNir.m nvmtl_air_nir_gpu_fault)
// through the plugin's own paths, with a fault NVK's test hook injects (NVK_FAULT_INJECT=mmu:<n>, plugin_run sets it for
// "fallback"). Run by name only, never by "render" or "compute": it faults this process's channel once, on purpose.
//
//   fallback       a render pipeline and a compute pipeline are made on the road; the blit of r_rleblit8 is drawn in
//                  command buffers of their own until the injected fault drops one (one channel rebuild, chan_recovered
//                  and chan_dropped +1 each, nothing else); then the plugin must have said the road is off, the road's
//                  render pipeline must draw as its SPIR-V twin and the compute pipeline dispatch as its twin (both right),
//                  a new pipeline must take the SPIR-V road, and the fallback list in OUTDIR/spvcache must name plugin_run
//   fallback_next  a second process with the first one's caches (PLUGIN_RUN_CACHE): the road is off from the start,
//                  because the list names it, and the blit is drawn right on the SPIR-V road
//
// Included by plugin_run.m after render_tests.h.

static bool
fb_log_has(const char *needle)
{
   char p[1024];
   snprintf(p, sizeof p, "%s/driver.log", out_dir);
   NSString *s = [NSString stringWithContentsOfFile:@(p) encoding:NSUTF8StringEncoding error:NULL];
   return s && [s rangeOfString:@(needle)].location != NSNotFound;
}

static int
fb_road(id ps)
{
   return ps && rroad_ivar ? *((const uint8_t *)(__bridge void *)ps + ivar_getOffset(rroad_ivar)) : -1;
}

/* one draw of t with ps into a fresh target, read back into r->back[0]; *dropped: the command buffer did not run (the
 * injected fault), which is allowed once (*fault_budget) and must raise chan_recovered and chan_dropped by one each */
static bool
fb_draw(const struct rtest *t, struct rrun *r, id<MTLRenderPipelineState> ps, int *fault_budget, bool *dropped, char *why, size_t wn)
{ @autoreleasepool {
   *dropped = false;
   id<MTLTexture> col[RMAXCOL] = { rtarget(t->col[0]) };
   id<MTLCommandBuffer> cb = [queue commandBuffer];
   rencode(cb, t, r, ps, nil, col, nil, 1);
   id<MTLBlitCommandEncoder> be = [cb blitCommandEncoder];
   [be copyFromTexture:col[0] sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0, 0, 0) sourceSize:MTLSizeMake(RW, RH, 1)
              toBuffer:r->back[0] destinationOffset:0 destinationBytesPerRow:(NSUInteger)RW * rbpp(t->col[0])
destinationBytesPerImage:(NSUInteger)RW * RH * rbpp(t->col[0])];
   [be endEncoding];
   int64_t before[NCOUNTERS], after[NCOUNTERS];
   read_counters(before);
   dispatch_semaphore_t done = dispatch_semaphore_create(0);
   [cb addCompletedHandler:^(id<MTLCommandBuffer> c) { (void)c; dispatch_semaphore_signal(done); }];
   [cb commit];
   const long timed_out = dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 2 * NSEC_PER_SEC));
   usleep(20000);   /* NVRM's counters follow the rebuild */
   read_counters(after);
   [col[0] release];
   const bool rose = memcmp(before, after, sizeof before) != 0;
   const bool one_fault = after[0] == before[0] && after[2] == before[2] && after[1] == before[1] + 1 && after[3] == before[3] + 1;
   if (timed_out || (rose && !(one_fault && *fault_budget > 0))) {
      printf("%-16s STOP     %s; no more GPU work\n", "fallback", timed_out ? "the command buffer did not complete in 2 s" : "a fault counter rose more than the injected fault");
      print_counters("before", before);
      print_counters("after", after);
      _exit(3);
   }
   if (rose) {
      (*fault_budget)--;
      *dropped = true;
      return true;
   }
   if (cb.status != MTLCommandBufferStatusCompleted) {
      snprintf(why, wn, "command buffer status %ld with no fault counted: %s", (long)cb.status, cb.error.localizedDescription.UTF8String ?: "?");
      return false;
   }
   if (!t->check(r)) {
      snprintf(why, wn, "%s", r->why);
      return false;
   }
   return true;
} }

/* conv_edge dispatched with cps; its out[0] for the NaN input must be 0 (the fixed conversion on either road) */
static bool
fb_dispatch(id<MTLComputePipelineState> cps, char *why, size_t wn)
{ @autoreleasepool {
   const float in[64] = { NAN, 1.5f, -2.5f };
   id<MTLBuffer> bi = [dev newBufferWithBytes:in length:sizeof in options:MTLResourceStorageModeShared];
   id<MTLBuffer> bo = [dev newBufferWithLength:64 * 6 * 4 options:MTLResourceStorageModeShared];
   memset(bo.contents, 0xab, bo.length);
   id<MTLCommandBuffer> cb = [queue commandBuffer];
   id<MTLComputeCommandEncoder> ce = [cb computeCommandEncoder];
   [ce setComputePipelineState:cps];
   [ce setBuffer:bi offset:0 atIndex:0];
   [ce setBuffer:bo offset:0 atIndex:1];
   [ce dispatchThreads:MTLSizeMake(64, 1, 1) threadsPerThreadgroup:MTLSizeMake(64, 1, 1)];
   [ce endEncoding];
   bool ok = submit(cb, "fallback", why, wn);
   const uint32_t *o = bo.contents;
   if (ok && (o[0] != 0 || o[6] != 1 || o[12] != (uint32_t)-2)) {
      snprintf(why, wn, "conv_edge after the fault: out[0] 0x%08x, out[6] 0x%08x, out[12] 0x%08x (want 0, 1, 0xfffffffe)", o[0], o[6], o[12]);
      ok = false;
   }
   [bi release];
   [bo release];
   return ok;
} }

static enum outcome
fb_result(const char *name, bool ok, const char *why)
{
   printf("%-16s %s %s\n", name, ok ? "PASS    " : "FAIL    ", ok ? "" : why);
   fprintf(rtsv, "%s\t-\t%s\t\t\t\t\t0\t%s\tthe per-process fallback\n", name, ok ? "PASS" : "FAIL", ok ? "" : why);
   fflush(stdout);
   fflush(rtsv);
   return ok ? PASS : FAIL;
}

static enum outcome
run_fallback_test(bool next)
{ @autoreleasepool {
   const char *name = next ? "fallback_next" : "fallback";
   char why[512] = "", path[2048];
   const struct rtest *t = NULL;
   for (unsigned i = 0; i < sizeof rtests / sizeof *rtests; i++)
      if (!strcmp(rtests[i].name, "r_rleblit8"))
         t = &rtests[i];
   snprintf(path, sizeof path, "%s/%s.metallib", air_dir, t->air);
   NSError *e = nil;
   id<MTLLibrary> lib = [dev newLibraryWithURL:[NSURL fileURLWithPath:@(path)] error:&e];
   snprintf(path, sizeof path, "%s/conv_edge.metallib", air_dir);
   id<MTLLibrary> clib = [dev newLibraryWithURL:[NSURL fileURLWithPath:@(path)] error:&e];
   if (!lib || !clib)
      return fb_result(name, false, "no library");
   id<MTLFunction> vf = [lib newFunctionWithName:@(t->vfn)], ff = [lib newFunctionWithName:@(t->ffn)];
   id<MTLFunction> kf = [clib newFunctionWithName:@"conv_edge"];
   MTLRenderPipelineDescriptor *d = [MTLRenderPipelineDescriptor new];
   d.vertexFunction = vf;
   d.fragmentFunction = ff;
   d.colorAttachments[0].pixelFormat = t->col[0];
   id<MTLRenderPipelineState> ps = [dev newRenderPipelineStateWithDescriptor:d error:&e];
   id<MTLComputePipelineState> cps = [dev newComputePipelineStateWithFunction:kf error:&e];
   struct rrun r;
   memset(&r, 0, sizeof r);
   r.t = t;
   t->setup(&r);
   r.back[0] = rbuf(NULL, (size_t)RW * RH * rbpp(t->col[0]));
   bool ok = ps && cps;
   int budget = next ? 0 : 1;
   bool dropped = false;
   if (!ok)
      snprintf(why, sizeof why, "no pipeline: %s", e.localizedDescription.UTF8String ?: "?");
   else if (next) {
      /* the second process: the list turned the road off before its first pipeline */
      if (fb_road(ps) != 0 || fb_road(cps) == 1)
         ok = false, snprintf(why, sizeof why, "the render pipeline took road %d in a process the fallback list names", fb_road(ps));
      else if (!fb_log_has("it is on the fallback list"))
         ok = false, snprintf(why, sizeof why, "the plugin did not say the fallback list turned the road off");
      else if (!fb_draw(t, &r, ps, &budget, &dropped, why, sizeof why) || !fb_dispatch(cps, why, sizeof why))
         ok = false;
   } else if (fb_road(ps) != 1)
      ok = false, snprintf(why, sizeof why, "the blit did not take the road (road %d): nothing to fall back from", fb_road(ps));
   else if (!fb_dispatch(cps, why, sizeof why))
      ok = false;
   else {
      unsigned k = 0;
      for (; k < 24 && ok && !dropped; k++)
         ok = fb_draw(t, &r, ps, &budget, &dropped, why, sizeof why);
      if (ok && !dropped)
         ok = false, snprintf(why, sizeof why, "the injected fault (NVK_FAULT_INJECT=%s) did not come in %u draws", getenv("NVK_FAULT_INJECT") ?: "-", k);
      printf("fallback: the injected fault dropped draw %u\n", k);
      if (ok && !fb_log_has("AIR to NIR off in plugin_run from now on"))
         ok = false, snprintf(why, sizeof why, "the plugin did not turn the road off after the fault");
      if (ok && !fb_draw(t, &r, ps, &budget, &dropped, why, sizeof why))
         ok = false;
      if (ok && !fb_log_has("bound after a GPU fault turned the road off: its SPIR-V twin draws"))
         ok = false, snprintf(why, sizeof why, "the road's pipeline did not draw as its SPIR-V twin");
      if (ok && !fb_dispatch(cps, why, sizeof why))
         ok = false;
      if (ok && !fb_log_has("dispatched after a GPU fault turned the road off: its SPIR-V twin runs it"))
         ok = false, snprintf(why, sizeof why, "the road's compute pipeline did not dispatch as its SPIR-V twin");
      id<MTLRenderPipelineState> ps2 = ok ? [dev newRenderPipelineStateWithDescriptor:d error:&e] : nil;
      if (ok && fb_road(ps2) != 0)
         ok = false, snprintf(why, sizeof why, "a pipeline made after the fault took road %d", fb_road(ps2));
      [ps2 release];
      if (ok) {
         snprintf(path, sizeof path, "%s/spvcache", out_dir);
         NSArray *names = [[NSFileManager defaultManager] contentsOfDirectoryAtPath:@(path) error:NULL];
         NSString *list = nil;
         for (NSString *n in names)
            if ([n hasPrefix:@"airnir-off-"])
               list = [NSString stringWithContentsOfFile:[@(path) stringByAppendingPathComponent:n] encoding:NSUTF8StringEncoding error:NULL];
         if (![list isEqualToString:@"plugin_run\n"])
            ok = false, snprintf(why, sizeof why, "the fallback list in %s holds %s", path, list ? list.UTF8String : "nothing");
      }
   }
   [r.back[0] release];
   for (unsigned i = 0; i < RMAXBUF; i++) {
      [r.vbuf[i] release];
      [r.fbuf[i] release];
   }
   [d release];
   [ps release];
   [cps release];
   [vf release];
   [ff release];
   [kf release];
   [lib release];
   [clib release];
   return fb_result(name, ok, why);
} }

/* ---- WindowServer's crash marker, run in this process (NVK_FAULT_INJECT=ws:2:plugin_run, a clean "minute" of 2 s)
 *
 *   ws_marker        the road's first pipeline sets the marker in OUTDIR/spvcache; 2 s later it is cleared
 *   ws_marker_crash  the marker is set, and the process ends at once, as a WindowServer that dies on the road would
 *   ws_marker_next   a second process with ws_marker_crash's caches: the marker was found, so the road is off, the
 *                    process is on the fallback list and the marker is gone */

static NSString *
fb_marker(void)
{
   char p[1024];
   snprintf(p, sizeof p, "%s/spvcache", getenv("PLUGIN_RUN_CACHE") ?: out_dir);
   for (NSString *n in [[NSFileManager defaultManager] contentsOfDirectoryAtPath:@(p) error:NULL])
      if ([n hasPrefix:@"airnir-ws-"])
         return [@(p) stringByAppendingPathComponent:n];
   return nil;
}

static enum outcome
run_ws_test(int phase)
{ @autoreleasepool {
   const char *name = phase == 0 ? "ws_marker" : phase == 1 ? "ws_marker_crash" : "ws_marker_next";
   char why[512] = "", path[2048];
   snprintf(path, sizeof path, "%s/conv_edge.metallib", air_dir);
   NSError *e = nil;
   id<MTLLibrary> clib = [dev newLibraryWithURL:[NSURL fileURLWithPath:@(path)] error:&e];
   id<MTLFunction> kf = [clib newFunctionWithName:@"conv_edge"];
   id<MTLComputePipelineState> cps = kf ? [dev newComputePipelineStateWithFunction:kf error:&e] : nil;
   const int road = cps && road_ivar ? *((const uint8_t *)(__bridge void *)cps + ivar_getOffset(road_ivar)) : -1;
   bool ok = cps != nil;
   if (!ok)
      snprintf(why, sizeof why, "no pipeline: %s", e.localizedDescription.UTF8String ?: "?");
   else if (phase < 2) {
      if (road != 1)
         ok = false, snprintf(why, sizeof why, "the kernel did not take the road (road %d)", road);
      else if (!fb_marker())
         ok = false, snprintf(why, sizeof why, "no crash marker after the first pipeline on the road");
      else if (!fb_log_has("the crash marker is set"))
         ok = false, snprintf(why, sizeof why, "the plugin did not say it set the marker");
      else if (phase == 1) {
         printf("%-16s PASS     the marker is set (%s); this process ends now, leaving it\n", name, fb_marker().UTF8String);
         fflush(stdout);
         _exit(0);
      } else {
         ok = fb_dispatch(cps, why, sizeof why);
         sleep(3);
         if (ok && fb_marker())
            ok = false, snprintf(why, sizeof why, "the marker %s was not cleared after a clean 2 s", fb_marker().UTF8String);
         if (ok && !fb_log_has("passed cleanly; the crash marker is cleared"))
            ok = false, snprintf(why, sizeof why, "the plugin did not say it cleared the marker");
      }
   } else {
      if (road != 0)
         ok = false, snprintf(why, sizeof why, "the kernel took road %d after a start that left the marker", road);
      else if (!fb_log_has("its crash marker was left by a start that did not pass a clean minute"))
         ok = false, snprintf(why, sizeof why, "the plugin did not say the marker turned the road off");
      else if (fb_marker())
         ok = false, snprintf(why, sizeof why, "the marker %s is still there", fb_marker().UTF8String);
      else
         ok = fb_dispatch(cps, why, sizeof why);
      if (ok) {
         snprintf(path, sizeof path, "%s/spvcache", getenv("PLUGIN_RUN_CACHE"));
         NSString *list = nil;
         for (NSString *n in [[NSFileManager defaultManager] contentsOfDirectoryAtPath:@(path) error:NULL])
            if ([n hasPrefix:@"airnir-off-"])
               list = [NSString stringWithContentsOfFile:[@(path) stringByAppendingPathComponent:n] encoding:NSUTF8StringEncoding error:NULL];
         if (![list isEqualToString:@"plugin_run\n"])
            ok = false, snprintf(why, sizeof why, "the fallback list holds %s", list ? list.UTF8String : "nothing");
      }
   }
   [cps release];
   [kf release];
   [clib release];
   return fb_result(name, ok, why);
} }
