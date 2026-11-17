# Changelog

## 3.0.2

**Not released.** This section describes the branch being prepared as 3.0.2, a patch on top of `fable` at b1b09c1b. That
commit holds the 3.0.1 changes below, merged and not released either (their last commit is 44091839), and the card
checks (`tools/card_selftest.py`). A package built from 44091839 has been installed on one Mac with an RTX 5050 (macOS
26.7.3); the fault below was found on its first boot there. Nothing committed after b1b09c1b has run on a card or on a
Mac: it was compiled and its host tests were run on Linux only (below, "What ran where").

### Fixes and changes

- **WindowServer's main thread no longer waits for a shader variant the warm-up has not started.** On the first boot
  after that package was installed, the userspace watchdog killed WindowServer 130 s after it started (bug_type 409,
  its main thread 40 s without a check-in); the WindowServer started next came up normally. The package was the first
  whose Metal plugin used this build's translator (3.0.1, "Metal apps get this build's shader translator"), so every
  cached function-constant variant was translated again. In all 12 samples of the watchdog's spindump the main thread
  was in `newFunctionWithName:constantValues:error:`, waiting, for up to 60 s, for the function-constant warm-up of
  2.9 to translate one variant, while four of the warm-up's threads took turns on its two slots: the variant had not
  started, it was queued behind others. (The warm-up exists because that main thread, made to translate dozens of
  Core Animation's variants one after another, 1.3 to 2 s each, was killed by the same watchdog.) Now a variant the
  warm-up has not started is taken out of its queue by the thread that asks for it, which translates it as any miss
  (the same cache file, the same records and logs; the log counts it as "TOOK OVER"), and the warm-up never translates
  it. A variant the warm-up is translating is waited for, for at most 5 s (`NVMTL_FCWARM_JOIN_MS`, a constant in
  `plugin/NVMTLLibraryLoad.m`; it was 60 s, and the longest translation measured is 3.2 s), and then translated by the
  caller, as before. Two workers take the queued variants in order, so the warm-up runs at most two translations at a
  time on at most two threads; it used to give its queue one block per recorded variant, up to 1024, each of which
  first waited for one of the two slots on a thread of the queue (four of them in the spindump). `NVMTL_FCWARM_OFF=1`
  still turns the warm-up off, and its records (`.fcwarm.v1`) and the cache files are as in 3.0.1. Not measured on a
  card.

### The ways back

None new. `NVMTL_FCWARM_OFF=1` in `nvrm610.conf` turns the whole warm-up off, as before; then every variant is
translated by the thread that asks for it, as before 2.9.

### What ran where

On Linux only (x86_64, clang 18 with GNUstep's Objective-C runtime and Foundation and libdispatch); nothing of 3.0.2 has
been built on a Mac or run on a card.

- `tools/test_fcwarm.py` compiles the warm-up and the cache code out of `plugin/NVMTLLibraryLoad.m` against a
  `libnvmtl_translate` built on Linux from the translator sources of b1b09c1b. At b1b09c1b its harness did not compile
  (the cache code it cuts out of the plugin had moved); it builds again. Core Animation's fixed_frag shader is not in
  the tree, so the AIR fixture `translator/translator/tests/fixtures/gl/texture_env_three_sites_behind_a_switch.ll`
  (three Boolean function constants) stood in for it, with `--pad-ms 300` so that each of its misses is recorded: 22
  of 22 checks passed in six runs, one of them with every processor busy. Against the warm-up of b1b09c1b (its
  dispatch groups boxed in `NSValue`, as Linux's libdispatch objects are not Objective-C objects) 6 of the 22 fail, in
  each of two runs: the bound (60 s), the queued variant (the caller waited 5.7 s for the queue, the background
  translated it, nothing was taken over), the slow worker (waited for 13 s) and the threads (4).
- The same harness built with ThreadSanitizer, in five scenes (a queued variant taken over, a running one joined, one
  that outlasts the bound, five asked for in a row, two one after the other): no report in the warm-up's code. Its
  reports are in GNUstep's runtime and Foundation, in hand-offs through Linux's libdispatch, which ThreadSanitizer does
  not see there, and in `nvmtl_fc_spec_on` (since 2.9), which keeps `NVMTL_FC_SPEC` in a static the first two
  translations can set at the same time, both to the same value. The warm-up of b1b09c1b gives the same reports, and
  more of the second kind.
- The plugin's Objective-C compiled with `-fsyntax-only` against a stand-in macOS SDK, at the SDK level of macOS 15.5
  and at that of Xcode 26.5: no new diagnostic. Its C and the C and C++ host tests, compiled for x86_64 macOS: the same
  diagnostics as at b1b09c1b (two C++ host tests do not link here at either). The host tests that read
  `NVMTLLibraryLoad.m` or `CHANGELOG.md` pass as they do at b1b09c1b, `tools/test_card_selftest.py` among them;
  `test_aircache_dir.py` and `test_spvcache_dir.py` fail at both, the same checks, because they need a folder the test's
  user cannot write and they ran as root.
- `tools/test_build_sh.py` (build.sh against stand-in tools, under bash 5.2, not macOS's bash 3.2): 393 of 393 checks
  passed, as at b1b09c1b; the one that differs says that a package records 3.0.2, CHANGELOG.md's first heading.

## 3.0.1

**Not released.** This section describes the branch being prepared as 3.0.1, on top of the 3.0 branch below (`fable` at
9f85871b). A package built from b80056ef, before the fix in "Metal apps get this build's shader translator" below, has
been installed on one Mac with an RTX 5050 (macOS 26.7.3), and card checks were run on it there; the fault that entry
fixes was found from them. Nothing committed after b80056ef has run on a card or on a Mac: it was compiled and its host
tests were run on Linux only.

### Fixes and changes

- **Idle mode switches while apps run.** NVK's boost lease took its shape from `NVRM_610_BOOST` once, when a process
  opened the device, so NVMetrics' idle mode (`NVRM_610_BOOST=0` in `nvrm610.conf`) reached only the apps started after
  it, and switching it off left the open ones without boost until they restarted. NVRM.kext now holds a boost policy of
  three values: undecided (every boot starts there, and each process follows `NVRM_610_BOOST` as in 3.0), idle mode and
  normal. A root process sets it through a new user-client selector (13); there is no writable sysctl, and
  `debug.nvrm_boost_policy` shows it read-only, with the number of changes in `debug.nvrm_boost_policy_sets`. Each
  Vulkan process reads it at most once a second, just before it would renew its lease (`nvk/nvk-boost-policy.patch`):
  in idle mode it asks for nothing and clears what it still has, in normal it boosts per burst again (or holds, with
  `NVRM_610_BOOST=hold`). A process that holds and submits nothing is not reached: it keeps its hold until it submits
  again or exits. A hold is cleared as it was asked (with the CUDA bit) and then as 3.0 cleared it, since which of the
  two the firmware honours is not in the open sources, and one renewal runs at a time in a process, also when an RM
  call waits through a sleep. The selector takes no gate and makes no RM call, so it answers during a sleep too; a 3.0
  kext does not have it, and NVK then keeps `NVRM_610_BOOST` as before. Boosts of other RM clients, such as NVMetrics'
  own Automático/Máximo or the optional `nvmtl-boost` daemon, are theirs and are not touched. NVMetrics switches both
  the file (for after a reboot) and the policy in one password prompt (`nvmetrics-agent policy idle|normal`).
- **`NVRM_610_BOOST` in `nvrm610.conf` reaches every Vulkan app.** NVK's own reader matched only 2.9's short spelling
  `BOOST=` in the file, and only when its first appearance started a line, so a Vulkan app without the Metal plugin
  (the plugin copies the file into the environment) ignored the `NVRM_610_BOOST=0` line NVMetrics writes. The full
  name is now read first (the last such line wins), then the short one, without printing anything.
- **A refused boost is asked again after 30 s.** 3.0 stopped asking for the rest of the process after one refusal, so a
  refusal while the GPU came back from sleep left that app without boost until it was restarted.
- **New blocks of video memory are cleared by the copy engine.** 3.0 stopped filling a granted block with zeros while
  the RM's own clearing is believed, but only until the GPU's first standby (a system sleep): from then on, and for good
  once a block read as not zero, every new block was filled from the processor through BAR1 again, at about 330 MB/s, 27
  ms for the 9 MB of each captured frame of a screen share, with the display gate held when the block was made on the
  caller's thread (the worker that makes blocks ahead filled them after letting go of the gate). Such a block is now
  cleared by the GPU's copy engine: NVRMFB asks NVRM (`nvVidmemClear`), and NVRM runs the RM's own memset on the block
  (`kexts/NVRM/nvrm_pageoff.c`, the way the page-off copy already runs) with the API and GPU locks held for the clear
  only. NVRM never waits for a standby there: with the GPU not running it answers at once and the block is filled from
  the processor as before, as it is where the RM made no copy-engine utility of its own (the clear makes none; the
  page-off still makes one when it first runs, as in 3.0, and the clear then uses it). The engine's end is waited for a
  quarter of a second at most, by the clock. A reading of the engine's progress that cannot be right (more finished than
  was ever sent, which is what a failed read of its semaphore gives) is not believed, here and in the page-off's own
  waits, where it would have ended every later wait of NVRM on the engine at once until the RM made its copy-engine
  utility again at the next sleep. The same 65 probes that check the RM's clearing then check the engine's; a block that
  does not read as zero is filled from the processor, and so is every block for the rest of the boot. Work whose end was
  not seen leaves its block allocated until restart, never handed out or freed, so that late zeros cannot land in
  someone else's memory. `debug.nvrmfb_vram_ce_clear` says whether the clear is on (1), off by boot argument (0) or
  stood down (2); `debug.nvrmfb_vram_ce_cleared`, `_ce_us`, `_ce_fallbacks` and `_ce_kept` count it, and the old
  `_filled` / `_fill_us` count what was still filled from the processor. `kexts/NVRMFB/fb/nvrm-vram-clear.h` holds the
  rules, `tools/test_vram_clear.c` every answer against them. The cause of the 330 MB/s itself is not established: the
  RM asks for a write-combined mapping, and by the macOS 26 sources NVRM's way of making it gets one.
- **Asynchronous texture uploads are on by default outside WindowServer.** `replaceRegion` submitted its copy and waited
  for the GPU before it returned, so an app that uploads many textures per frame stalled once per upload. The
  asynchronous path (its own staging buffer and command buffer, finished before the image is overwritten, read back or
  freed: `plugin/nvmtl_tex_upload.h`) had already run by default since 2.6.3 for CPU-written surface uploads outside
  WindowServer (`NVMTL_ASYNC_SURFACE_UPLOAD`); with `NVMTL_ASYNC_TEX_UPLOAD` it now runs for every region upload outside
  WindowServer, whose uploads stay synchronous. `=1` still turns it on anywhere. What waits is limited: at most 256
  uploads and 256 MiB of staging memory per process, and past either an upload waits for its copy as in 3.0 (the first
  one is logged), so an app that uploads many textures once and never reads them back does not keep a staging copy of
  each. The limits are judged without a lock, so threads uploading at the same moment can pass them by one upload each.
  An upload into a texture over an IOSurface is finished before the surface is published, because for a surface in the
  family's video memory publishing copies nothing and another process could read the old pixels as the new ones. Two
  threads uploading into one texture at once no longer lose the bookkeeping of one upload. Known and left as it was:
  after the GPU channel was rebuilt, an upload it dropped is made again the next time the texture is finished, over any
  copy or blit made into the texture in between (3.0's synchronous upload logged the failure and did not repeat it).
- **Shaders of a game are translated ahead.** In a game (the games category or `LSSupportsGameMode` in its `Info.plist`,
  the test macOS's own Game Mode uses), every entry point of a new shader library is translated by a small pool and
  stored in the shader cache (`NVMTL_PRETRANSLATE`, which only the Cyberpunk profile set before), so the first run of a
  level does not stop for each new shader. Other processes are left as they were; `=1` still turns it on in any process.
  The pool's threads run at utility priority, no longer background: a game's thread that needs a shader the pool is
  translating waits for it on a condition variable, which lends the pool no priority. At most 512 shaders wait in the
  pool's queue; one past that is counted as skipped and translated when it is first asked for, as without the pool.
  Neither of these two was measured on a card yet; the rules are in `plugin/nvmtl_switch_defaults.h`,
  `plugin/nvmtl_tex_upload.h` and `plugin/nvmtl_pretranslate.h`, and `tools/test_switch_defaults.c`,
  `tools/test_tex_upload.c`, `tools/test_pretranslate.c` and `tools/test_plugin_defaults_wiring.py` hold them.
- **WindowServer does not upload a surface whose pages are all zero.** While the screen is shared, WindowServer's
  main thread spent about 16 ms of each captured frame in the Metal plugin, by spindump samples taken before 3.0's
  fast grants. 4.2 ms of it were the surface-in of the new capture surface, which nobody had written: its CPU pages
  were imported, copied into the video memory the kernel had just granted, and waited for. On a surface's first
  surface-in the plugin now reads its pages under the same read-only lock the upload takes (`NVMTL_SURFIN_ZERO`,
  `plugin/nvmtl_zero_scan.h`); when every byte is zero it clears the surface's video memory with a GPU fill ahead of
  the next command buffer and does not upload. Only single-plane surfaces in the family's video memory that no other
  process has drawn into are taken this way, and no VRAM tag is published for them, so other processes upload as
  before; a fill that does not run (its prologue failed or rode a dropped submission) makes the surface decide again at
  its next use, though the command buffers already submitted by then read the video memory as it was, as after a
  failed upload in 3.0. When the prologue's End fails, WindowServer now also marks the managed uploads it carried to be
  made again (they were lost there before); other processes keep 3.0's handling. It is off with
  `NVMTL_PRESUBMIT_PROLOGUE=0`, whose fills would not be ordered against other threads' submits. A later change of the
  surface is uploaded at once, without a second scan. The scan itself costs from
  about 0.5 ms (pages resident) to about 3 ms (pages faulted in), so the saving is an estimate of 1.2 to 3.7 ms per
  captured frame from those samples, NOT measured; the census line "surface-in zero census" counts scans, zero
  answers, their mean time and the full scans that ended non-zero. `tools/test_zero_scan.c`,
  `tools/test_ws_capture.c`, `tools/test_surfin_zero.py` and `tools/test_surfin_zero_plugin.py` hold it.
- **WindowServer frees a dead texture's video-memory import on a thread of its own.** When Core Animation released a
  texture over a surface in the family's video memory and no submitted work could still read it, the RM frees of its
  import ran on WindowServer's main thread: about 1.2 ms of each captured frame in the same samples. Such a free now
  goes to a serial queue (`NVMTL_WS_FREE_OFFMAIN`, `plugin/nvmtl_ws_capture.h`); only imports of the family's video
  memory move, at most 64 MB of them wait at a time (past that a free runs where it did), and the death is recorded
  for fault reports before the free is queued. A free that waited for a submit already ran this way, after the surface
  was released. The free still takes the RM's per-client gate on that thread, so part of the saving can come back as
  waits there, and with the change above on fewer of these frees find no work in flight (the rest are freed later on
  the completion thread, as before), so the two savings overlap; it is an estimate from samples taken before 3.0's
  fast grants, NOT measured.
  `tools/test_ws_capture.c` and `tools/test_surfin_zero_plugin.py` hold it.
- **NVK's own mapped memory that the processor only writes stays in video memory outside WindowServer.** The shader
  heap, the descriptor tables and sets and the other device-local memory NVK itself maps were kept in system RAM,
  uncached on the GPU side, so the GPU fetched shader code and descriptors over PCIe; `NVK_LOCAL_MAP_SYSMEM=0` kept them
  in video memory behind BAR1, and only the Cyberpunk profile set it (its comment says about twice the frame rate there;
  no record of that measurement is in the tree). Video memory is now the default in every process except WindowServer,
  where BAR1 covers the whole VRAM, for the part of that memory that is not shared with another process and that the
  processor does not read back while the app runs: shader code, the image and sampler tables, the zero page and indirect
  execution sets (`nvk/nvk-local-map-vram.patch`). What it reads back keeps system RAM: descriptor pools (a descriptor
  copy reads its source set, the Metal plugin copies unchanged descriptors on every draw, a read through BAR1 is a trip
  to the card, and the plugin's pool alone is 21 MB per process), USERD and the event heap. WindowServer keeps system
  RAM: 3.0 made the placement opt-in after its channel stalled with it. That stall has the shape of a lost doorbell that
  was fixed later, but nobody has shown on a card that the two are the same, so this is the change of 3.0.1 to suspect
  first if an app freezes: `NVK_LOCAL_MAP_SYSMEM=1` (the settings tool's "Keep CPU-written memory in system RAM", on)
  keeps all of it in system RAM in every process, as 3.0 did. `=0` means what it meant (everything in video memory,
  WindowServer included), and `NVK_BAR_VRAM=0` also keeps the new default off. When the video memory is full, or BAR1
  has no room for the map, an allocation the default placed is made in system RAM as before instead of failing. Since
  USERD keeps system RAM, the read of GPPut before a doorbell no longer goes through BAR1, where it used to hold the
  doorbell back until everything written through BAR1 ahead of it had landed; that was the order of the one placement
  that ran on a card (`=0`). Each device therefore keeps one page behind BAR1 (64 KiB of video memory with big pages)
  and reads a word of it before it writes GPPut for a doorbell, so shader code and table entries written just before a
  submission are in the video memory before the GPU can see the work. NVIDIA's own driver does the same where GPPut is
  in system RAM. The read orders what has left the processor's write-combining buffers, so a shader upload and a sync of
  memory the default placed now drain them when they are done. Memory an app maps itself is behind BAR1 in system-RAM
  mode too, as in 3.0, and gets this read only under the default. That the GPU could otherwise fetch them too early was
  never seen on a card; the read costs one trip to the card per doorbell. If the RM's standby fails at a sleep, what the
  default placed is lost with the rest of the video memory, in every process and not only in WindowServer. Not measured
  on a card; `tools/test_nvk_local_map.py` compiles the rule, the placement and the fallback and holds every case.
- **NVK's table of CPU mappings has a lock.** On the NVRM back end every CPU map of a Vulkan memory object adds its
  address to one hash table of the device, and every unmap or free removes it, with no lock: two threads mapping or
  freeing two different memories at once could corrupt the heap. Each insert, search and removal now holds a mutex of
  the device that is taken around the table alone, and an unmap takes its entry out before the kernel unmaps, so a new
  mapping given the same address cannot lose its entry (`nvk/nvk-nvrm-fixes.patch`). A device whose table could not
  be created no longer closes the process's standard input on its way out.
- **`VK_EXT_map_memory_placed` is no longer offered on NVRM.** The NVRM back end ignored the address the application
  asked for and could not map over a reserved range, but the extension was advertised, and Zink turns on client-pointer
  maps when it sees it. It is now offered only where the back end can place a map (Linux nouveau keeps it).
- **Three settings read once.** `NVRM_FLUSH_TIMEOUT_MS` was read on every synchronous flush, and
  `NVK_SYSMEM_GPU_UNCACHED` and `NVK_BAR_VRAM` on every memory allocation, each time with a scan of `nvrm610.conf` and a
  line on stderr when the file set them. They are now read once per process; their values mean what they did.
- **Upstream NVK fixes from Mesa main.** `nvk/nvk-mesa-main-2.patch` carries eight Mesa main commits, unchanged:
  push descriptor sets are uploaded only when they changed and only to their layout's size (they were uploaded again
  at every draw), the single color-write mode is used when every target has the same mask, image format queries take
  stencil usage into account, the swapchain private-data mutex is initialised on macOS too, attachment locations
  of secondary command buffers are read from the right structure, and the range of a map that is synchronised is
  computed correctly (no effect on NVRM, whose maps are coherent). Mesa's NAK compiler changes since are not taken.
  These four entries were compiled on Linux only; nothing in them has run on a card.
- **Metal apps get this build's shader translator.** The Metal plugin opens the `libnvmtl_translate.dylib` beside it
  in `NVMTLDriver.bundle` whenever there is one, and the one in `nvmtl/` only when there is none. Release 2.9, the
  package `./build.sh package` starts from, carries its translator beside the plugin, and the packages built from the
  3.0 branch, and those built from this branch up to b80056ef (which record version 3.0.1 like the fixed ones), put
  this build's in `nvmtl/` only: Metal apps translated with release 2.9's translator, so none of the translator's
  repairs since 2.9 reached them. The package now puts this build's translator beside the plugin too, and the package
  step stops when the two copies are not the same build. Shaders cached with 2.9's translator are translated again
  once: the name of each shader cache entry carries the SHA-256 of the translator file that made it.

### The ways back

One for each change that has a switch, as in 3.0 below; each restores the 3.0 behaviour of that change only. The
changes above that are not in the table have none. One exception: in Cyberpunk 2077 the built-in profile already set
`NVK_LOCAL_MAP_SYSMEM=0` and `NVMTL_PRETRANSLATE=1` in 3.0, and a line in your own block of the file overrides the
profile too, so `NVK_LOCAL_MAP_SYSMEM=1` or `NVMTL_PRETRANSLATE=0` there turns off what 3.0 had on in that game.

| Change | Way back | Kind |
|---|---|---|
| Copy-engine clear of new blocks of video memory | `nvfbceclear=0` | boot argument |
| Asynchronous texture uploads outside WindowServer | `NVMTL_ASYNC_TEX_UPLOAD=0` | `nvrm610.conf` |
| Shaders of a game translated ahead | `NVMTL_PRETRANSLATE=0` | `nvrm610.conf` |
| NVK's own write-only mapped memory in video memory outside WindowServer | `NVK_LOCAL_MAP_SYSMEM=1` | `nvrm610.conf` |
| WindowServer clears an all-zero surface on the GPU instead of uploading it | `NVMTL_SURFIN_ZERO=0` | `nvrm610.conf` |
| WindowServer frees dead video-memory imports on a thread of its own | `NVMTL_WS_FREE_OFFMAIN=0` | `nvrm610.conf` |

## 3.0

**Not released.** This section describes the branch being prepared as 3.0. No test of it on an NVIDIA card has been
reported yet. At commit 3933ff6, `./build.sh` built all 17 components of the driver with no warning on one Intel
Mac (12 threads, macOS 26.7, Xcode 26.5): the kernel extensions (NVRM, NVAccel for macOS 15 and 26, NVRMFB, NVRMAGDC),
NVK, the Metal plugin, the shader translator, the video library and the other components; it then staged a package over
the 2.9 release package there, and the host tests passed there (303 passed, 0 failed, 3 skipped, 9 not run, of 315). At
commit ee7f332 it built the 17 components and staged the package there again; the host tests were not run there at that
commit. At commit aa27c73 it built the 17 components with no warning, staged the package and ran the host tests
there (305 passed, 0 failed, 3 skipped, 9 not run, of 317). It had built that Mac's build tools from an empty working
directory at commit 650db45. At an earlier state of the branch that macOS's `kmutil` found every one of
the 506 kernel symbols NVRM imports, and 291 of the 310 host tests passed there; the seven that failed have been fixed
since. On GitHub's Intel runner (macos-15-intel: 4 processors, macOS 15, Xcode 26.3 with the 26.2 SDK), at commit
3933ff6, the workflow `full-build` built all 17 components and staged the package (its test step failed there because
its clone had no history, which is corrected after 3933ff6), and in the workflow `ci` the host tests passed
(303 passed, 0 failed, 3 skipped, 9 not run, of 315). At commit ee7f332 every step of the workflow `full-build`
succeeded there for the first time: the build, the package, the host tests (304 passed, 0 failed, 3 skipped, 9 not
run, of 316) and its twelve checks of what was built. In the workflow `ci` at that commit the host tests passed with
the same numbers on that runner and, for the first time whole, on GitHub's Apple-silicon runner (macos-15). At commit
95811c2 the kernel extensions built on GitHub's Intel and Apple-silicon runners had the same bytes; no whole build has
run on an Apple-silicon Mac. A package built from aa27c73 on the Intel Mac has been installed over 2.9 on one machine
with an RTX 5050, which started with it twice without the installer's fallback job putting 2.9 back. Nothing committed
after ee7f332 has run on GitHub, and nothing committed after aa27c73 has been built or tested on a Mac: the changes made
after it, among those below, were compiled and their host tests run on Linux only (those that need a Mac have not run).
The check list for a first run on a card is [docs/TESTING-ON-HARDWARE.md](docs/TESTING-ON-HARDWARE.md).

Nothing new in this release has to be switched on: every change below is active as installed. Each risky change that
alters how the driver runs has one way back to the 2.9 behaviour, read once (a boot argument when the driver starts,
a line of `/Library/GPUBundles/nvmtl/nvrm610.conf` when a process starts). These are listed under "The ways back" below
and in [docs/SWITCHES.md](docs/SWITCHES.md), and are there to tell whether a new path is behind a problem, not to be
left set. Several default-on changes have no switch of their own: the interrupt path's closing at sleep, shutdown and
stop; the adapter-teardown order and the idle power-down hold; the per-GPU interrupt mark and the real CPU count; the
unbounded kernel-mapping table; the nano timers apart from their cancel wait; the event connection's close; the
utilization sampler's pause after a slow reading; the framebuffer's scanout floor on a BAR1 of 512 MB or more, its
derived refresh rates checked with NVKMS, its owed connect interrupt and its keeping of an override's
scale-resolutions; the hybrid panel framebuffers' start order; NVK's uncached command and upload stream; the plugin's
fence clock, its refusal of a copy into a multisample image, its kept visible-link answers, its kept grid-only scans,
its one VRAM mark per surface plane per commit, its done tags written before its holds close, WindowServer's early
start of the two-plane round trip, its reading of the macOS version and its keeping of a device it
refuses before Metal's own init, the usage its IOSurface drawables report, its no-op scheduled-present handler for
drawables an application made itself, its half-float mip levels made on the GPU, the storage view of textures of
Unknown usage, its A8 attachment views and the tiled stand-ins of linear attachments in framebuffer-fetch passes; the
shader translator's repairs below; and the kernel extensions staying loaded once started. The way back from these is to
reinstall 2.9 (and, for the power paths, the boot arguments `-nvrmnopm` and `-nvrmnoshutdown`).

### Fixes and changes

#### Desktop, windows and screen capture
- The kernel now knows which process is still drawing into which surface, on which GPU channel and up to which point
  (the surface fence, selector 12 of NVRM's user client). A reader of a surface (WindowServer compositing a window, a
  capture, NVAccel flipping or copying a frame) sleeps until the GPU has really finished the writer's work and is
  woken by the GPU's interrupt, instead of polling a mark that gave up after 10 ms or 100 ms. It stops waiting when
  the writer exits, when its channel faults or when the GPU goes to sleep. Every wait is bounded (2 s for GPU work,
  100 ms for a writer that is stuck on its CPU), and a process whose holds are given up three times goes back to
  2.9's marks and tags. A process that exits or is killed releases the surfaces it was drawing before the driver
  frees its channels. `sysctl debug | grep nvrm_fence` shows what it does. Way back: boot argument `nvrmfence=0`.
  See [docs/SURFACE-FENCE.md](docs/SURFACE-FENCE.md).
- Metal plugin: IOSurface synchronisation between processes goes through that fence: a command buffer that writes a
  surface holds it in NVRM until its work is done, and a reader in another process sleeps until then instead of
  polling a tag for at most 10 ms (and reading a half-drawn frame when the GPU needed longer). WindowServer's swap
  waits for the same holds. A process whose kernel, NVK or channel cannot take part keeps the 2.9 behaviour, per
  command buffer, and a reader that the kernel sends away before the write is known to be complete still gives the
  writer the 10 ms that 2.9 gave it.
- NVK registers its channels with the surface fence and gives the Metal plugin the calls to open, arm, close and wait
  for surface holds. With an older kext, or with `nvrmfence=0`, nothing changes. Waits on the copy-engine (upload)
  queue can sleep in the kernel instead of polling.
- The accelerator waits for WindowServer's GPU work through the surface fence: a flip, a copied frame or a screenshot
  of a window now waits until the GPU has really finished it (up to 2 s, instead of giving up after 100 ms and showing
  a half-drawn frame), and stops waiting at once when the drawing process exits. A wait made while holding the
  accelerator lock keeps the old 100 ms bound. With `nvrmfence=0` the 2.9 behaviour is back.
- A screenshot, a screen recording or any CPU read of a window that WindowServer's GPU is still drawing no longer
  holds the accelerator lock while it waits for the GPU, so the rest of WindowServer keeps running meanwhile. This
  existed in 2.9 behind `debug.nvaccel_pageoff_unlocked_wait=1` (that sysctl is now read-only and reads 1); its
  defects were fixed first: when another thread copied the same surface while this one waited, the copy is not made
  a second time over what was written since, and a copy that came before the GPU had finished is made again instead
  of being taken for the result; a failure to take the lock back is handled; a reference that turns out to be the
  last one is kept instead of freed under the caller, and nothing is read through such an object; a page-off reached
  from the driver's own locked code keeps the lock. If anything unexpected is seen the path turns itself off for the
  rest of the boot (`debug.nvaccel_pageoff_unlocked_standdown` = 1, one log line `page-off: unlocked wait OFF for
  this boot (...)`). Way back: boot argument `nvaccel_pageoff_locked=1`.
- Two-plane (NV12) surfaces, such as screen-capture and video frames, can now live in VRAM one plane at a time: the
  accelerator pages each plane back into its own place in the surface. The plugin uses this only after a round trip
  on a private surface came back right on the card, once per process. Way back: boot argument
  `nvaccel_surf_planes=0`.
- The accelerator now checks, before it attaches, that the accelerator family classes it builds on have the size
  this driver was built for. If a macOS update changed one, the accelerator does not attach and the desktop runs
  without acceleration instead of crashing; the kernel log names the class
  (`NVAccel: Apple class layout refused: ...`). It also reports how many virtual functions those classes have
  (`nvaccel-layout-vtables`); that is a report and never a reason to decline.
- NVRMFB and NVRMAGDC compare the Apple classes they are built on with the running macOS before they start. NVRMFB
  compares the size of IOFramebuffer, IOGraphicsDevice and IODisplayParameterHandler; NVRMAGDC the size of
  AppleGraphicsDeviceControl. On a different size the kext does not start (NVRMFB) or does not create its
  device-control object (NVRMAGDC, `debug.nvrmfb_agdc` = -6) and says which class differs, instead of running on a
  misplaced base. When NVRMFB refuses, NVRM does not start the card and the driver shows nothing on it, since every
  framebuffer of the driver is built on those classes; that is not counted as a failed start. With the OpenCore setup
  of README.md, which excludes Apple's IONDRVSupport, no desktop comes up on the card; where IONDRVSupport is not
  excluded, Apple's firmware framebuffer takes the card and shows a slow desktop without acceleration when the
  firmware's picture still lies inside the card's memory window (otherwise the screen stays dark there too). After an
  install over an earlier driver, while the installer's fallback job is still there (until the first start after the
  one in which it found the display lit), the job finds the refusal at that start, keeps it (the class and both sizes)
  in `/Library/NullMoth/lastgood.log` and, while the Mac still runs the macOS build the install was made on, puts the
  earlier driver back and restarts (where another GPU shows a display and a user is logged in by then, the restart is
  left to that user; a display on the refused card, which only Apple's firmware framebuffer can draw, does not hold it
  back, even after an automatic login). The verdicts are on the GPU's PCI device as `nvrmfb-layout`, `nvagdc-layout`
  and `nvaccel-layout`. NVRMFB also publishes how many virtual functions IOFramebuffer and IODisplayParameterHandler
  have in the running macOS (`nvrmfb-layout-vtables`); that is a report and never a reason to refuse. No result of
  the three checks on a Mac has been reported yet, and the sizes of these classes in macOS 26.7 have not been reported:
  each check decides when its kext starts. Way back for all three checks: boot argument `nvlayout=0`.
- WindowServer marks each surface plane as newest in video memory once per commit, not once for every part of the
  command buffer and every texture that writes it. Each mark is a call into the accelerator that takes its lock, and a
  video playing in a window made many of them per frame.
- A screen share through the system's picker (ScreenCaptureKit: Google Meet in any browser, Discord) no longer
  costs WindowServer a fill of 9 MB from the processor and a wait for the display on every captured frame. Reported:
  while a share runs, the desktop and a game lose most of their frame rate (Minecraft Java from 199 to 40 frames a
  second, to 20-30 with OpenGL), although the other side of the call sees a smooth picture. Measured on that Mac
  (RTX 5050, one 1920x1080 display at 100 Hz, this branch at aa27c73): WindowServer falls from 100 frames a second
  to 20, and the game runs at twice WindowServer's rate. For every captured frame WindowServer makes a new surface
  of 9,043,968 bytes of video memory and destroys it in the same frame. Of the 50 ms each frame took on its main
  thread, 27.4 ms were the framebuffer filling the new block with zeros from the processor, through BAR1 (about
  330 MB/s), and 6.0 ms were the wait for the display gate, which a flip holds while it waits for its latch. Two
  changes in the framebuffer (`kexts/NVRMFB/fb/nvrm-vram-pool.h`), with one way back for both, the boot argument
  `nvfbvramfast=0`:
  - The fill is left out while the RM's own clearing is believed. The RM clears video memory when it is freed,
    refuses to allocate while its scrubber is not valid and reports this as a capability of the device
    (`NV0080_CTRL_FB_CAPS_VIDMEM_ALLOCS_ARE_CLEARED`); NVRM asks once, when the adapter is up. The framebuffer
    reads 65 places of every block it does not fill. The first block that is not zero there, and the first standby
    of the GPU (a system sleep, or an idle power-down where `nvrtd3=1` allows them: the slot loses power, and
    nothing in the RM's open sources clears the memory that was free), each end the belief until the next restart:
    from then on every block is filled as in 2.9, and the blocks that were ready go back unused. After a standby
    the same 65 places are still read before the fill and only counted
    (`debug.nvrmfb_vram_wake_clean`, `debug.nvrmfb_vram_wake_dirty`), so that a card can show whether its memory
    comes back clean. That the memory is clean at boot is the RM's word and NVIDIA's comment, not something the
    open sources show for this card; the 65 places are the only check on it.
  - A size that is granted three times in a row and handed back twice in a row, each within a second of the one
    before, is in churn. For such a size a worker thread keeps up to three blocks ready (64 MB in all, or two blocks
    of a larger size, never more than an eighth of the BAR1 budget; only on a BAR1 over 512 MB), and a grant takes
    one without waiting for the display gate. Every block is still a new allocation with its own cookie, made by the
    same code with the gate held, one per hold; a block that has to be filled is filled by the worker after it let
    go of the gate. Ready blocks go back within a second and a half of the last grant or release of their size,
    before a grant is refused for lack of room, after a standby (none made before it is handed out) and when the
    framebuffer stops. The worker runs as an ordinary kernel thread, below the calls that carry flips and vblanks,
    and calls into the display code only after it has seen the GPU up: a block it holds when a standby begins waits
    and goes back after the wake. Releases are unchanged.
  About 15 to 16 ms of other work per captured frame stay on WindowServer's main thread in that measurement (the
  upload of the new surface, NVK's and the RM's objects for it, the composite), so the most these two changes can
  give there is about 60 frames a second in WindowServer where there were 20 (50 if WindowServer then waits for
  every other refresh of the 100 Hz display), and less after the Mac has slept (the worker then fills each block
  itself, about 27 ms each, so it cannot make one for every frame). Compiled and host-tested on Linux only
  (`tools/test_vram_pool.c`); not yet run on a card.
- Three smaller changes for the same report were written before that measurement, from the code alone. The
  measurement shows they are not what costs the frame rate on that Mac, whose capture surfaces have one plane; they
  stay because each is right for two-plane (NV12) capture and video surfaces, where each surface cost several waits
  in a row:
  - Drawn through private images (always in 2.9; in this release until the round trip below has passed): the two
    planes are copied back to the surface together, under one lock and with one wait for the GPU, where each plane
    took the lock and waited by itself; and since the lock is taken once, the plane copied first no longer looks
    stale and is no longer uploaded again at its next use, which was a third wait, on the compositor's thread. A
    plane whose copy fails is reported by itself and the other one stands. Way back: `NVMTL_SURFOUT_PLANES=0` in
    `nvrm610.conf`.
  - With the planes in VRAM: marking one plane newest moves the surface's one seed, which left the tag of the plane
    marked first one seed behind, so at its next use WindowServer locked the surface (a copy of both planes back to
    system memory in the kernel) and uploaded that plane again, once per captured frame. The other planes' tags now
    follow a mark that took and around which the seed moved by exactly one. That move is the mark's own only where
    every mark moves the seed by one, which is macOS's doing and not this driver's, so the round trip below now reads
    the seed around each of its marks (one of them a mark of video memory that is already newest), and the tags
    follow only in a process whose round trip saw every mark move it by exactly one (the log says which:
    `planar surfaces: each of the self-test's ... marks moved the surface's seed by exactly one`). Elsewhere nothing
    follows, as before. Way back: `NVMTL_PLANE_FOLLOW=0` in `nvrm610.conf`.
  - WindowServer starts the round trip that allows planes in VRAM five seconds after its device is made, not at its
    first two-plane surface: every surface it wrapped before the verdict stayed on private images for as long as it
    was kept, so a share that was the first to bring such surfaces ran on them, and a round trip started while a
    game kept the GPU busy could miss its 2 s bound and leave WindowServer on private images until the next login.
  Compiled and host-tested on Linux only; not yet run on a card, so what macOS does to the seed on a mark is not
  known (the round trip's log line says it). Reading `sysctl debug | grep -E 'nvaccel|nvrm_fence|nvrmfb_vram'`
  twice, ten seconds apart, during a share shows what is left (the counters below among them).
- A monitor that sends a placeholder 1920x1080 identification while it is still waking (after some warm restarts) and
  its real one a few seconds later gets its own resolutions on a card whose BAR1 is 512 MB or more, as long as its
  largest desktop needs no more memory than a 3840x2160 one: there the framebuffer reserves the memory a display scans
  out of with a floor of a 3840x2160 desktop at start, and a mode list that arrives later is measured against the
  memory really allocated (a larger one keeps the first identification's limits until a cold start). Before, the
  reservation followed the first identification and the monitor stayed a 1920x1080 television until a cold start.
  Below 512 MB of BAR1 the floor is not applied, because there it would take a large part of the accelerator's surface
  budget; without `nvfbhidpi` each lit head on a larger BAR1 reserves about 24 MB more video memory for a 1920x1080
  monitor.
- Refresh rates derived from a panel mode of 240 Hz or more (its half and its quarter) are offered only when NVKMS
  accepts their timing; a refused one is logged and left out. Before, a panel that takes only its own timings showed
  the rate as selected and stayed at the previous one.
- A display connected after start-up no longer stays dark when its connect interrupt comes due before macOS has
  registered the handler for it: the interrupt is kept owed and scheduled again 100 ms after the handler is
  registered.
- A display override's own HiDPI sizes (its scale-resolutions) are kept until the display is rotated: the framebuffer
  takes back only the scaled sizes it offered itself for a rotated display.
- Metal plugin: the source fork reports Spotlight's minified half-float application icons missing, because Core
  Animation leaves the lower levels of R16Float, RG16Float and RGBA16Float textures unwritten. The plugin now makes
  those levels with the GPU's filtered blits. From the HighDelay/nvidia-macos-driver-nvk fork, which reports it on an
  RTX 2060 SUPER, macOS 26.7.1; built and checked on Linux only here.
- Metal plugin: the source fork reports stale layers in native applications' icons and in the Applications grid. A
  shader-writable wrapper in Managed storage of a single-plane surface in the family's VRAM is now drawn, sampled and
  read by framebuffer fetch through a tiled image of its own, which is copied into the linear VRAM import before the
  surface is marked and copied back from it when another writer made the VRAM newer. With framebuffer fetch on (every
  process but WindowServer), a pass that has a linear colour attachment beside a tiled, unaligned or depth attachment
  draws the linear one through a tiled stand-in, at the cost of one more image and two GPU copies per such pass whether
  or not a pipeline of the pass reads the attachment; that cost has not been measured. From the
  HighDelay/nvidia-macos-driver-nvk fork, which reports fresh native icons and the Applications grid drawn correctly on
  an RTX 2060 SUPER, macOS 26.7.1; built and checked on Linux only here. Way back for the tiled image:
  `NVMTL_MANAGED_SURFACE_TILED=0` in `nvrm610.conf`; the stand-ins have no switch of their own (`NVMTL_NO_FBFETCH=1`
  turns framebuffer fetch off, and them with it).
- Metal plugin: a texture of Unknown usage gets a storage view when it is made, so shader writes through its mip and
  slice views land; the first format queries of several threads at once no longer make textures without their storage
  view; A8 colour attachments are attached through identity views, and a framebuffer-fetch read of one returns Metal's
  (0,0,0,r) instead of (r,0,0,1). A linear alias of a buffer takes the format features of DRM modifier 0 when NVK lists
  that modifier for the format, which can allow render-target use that plain LINEAR tiling's features do not; a format
  for which NVK lists no modifier (B10G11R11_UFLOAT and E5B9G9R9_UFLOAT among them) keeps plain LINEAR tiling's
  features. From the HighDelay/nvidia-macos-driver-nvk fork; built and checked on Linux only here.

#### Applications and games
- An application tells readers that a surface is finished (its done tag, then the close of its surface fence hold)
  before it reports the command buffer as scheduled and before it hands its drawables over, not after: WindowServer
  compositing a frame that was just presented found it marked as still being drawn and slept until the
  application's completion thread got that far. The done tag is also written before the hold is closed, where a
  reader arriving between the two polled the tag. Way back for the first: `NVMTL_BUSY_DONE_EARLY=0` in
  `nvrm610.conf`; the order of tag and close has none.
- Applications no longer poll through long GPU waits: after 1 ms a waiting thread sleeps on the GPU's completion
  event, as WindowServer already did, and a thread whose waits run longer than about 2 ms sleeps almost at once;
  waits shorter than 1 ms are unchanged. A process whose wake-ups do not arrive goes back to polling by itself; an
  event that merely arrives late (the GPU shows a completion just before its event) does not count towards that, so
  a game or compositor that waits at a steady cadence keeps sleeping. Going back to polling is not for the life of
  the process, because wake-ups can stop for a moment and come back: each of its threads tries the event again after a
  pause (256 of its waits, doubled after each failed try up to 65536), and the first wake that arrives puts the whole
  process back on the event. Only a kext that cannot wait at
  all keeps a process polling for good. Way back: `NVRM_EVENT_WAIT=0` in
  `nvrm610.conf`. Like 2.9's `0`, it makes every process poll, WindowServer included, which costs WindowServer more
  CPU than both 2.9's default and this release's. See [docs/EVENT-WAIT.md](docs/EVENT-WAIT.md).
- A GPU fault on one channel no longer ends the application's Metal device. When a channel faults with an error that
  ends only that channel, or makes no progress for 5 s in WindowServer (60 s in any other process), NVK builds a new
  channel, sends the channel-wide state again, replays the submissions that had not started and drops the one that was
  running; a stalled channel's doorbell is rung again first. A fault on any copy engine is rebuilt like this too: on
  an RTX 50 (Blackwell) card the channel NVK uses for uploads runs on copy engine 4, whose fault the hardware reports
  as Xid 71. A copy channel replays all of its work instead of dropping any, and one that faults again inside the work
  its rebuild replayed ends the device at once. A fault the GPU cannot recover from this way (firmware, bus, memory
  errors, a failed wake, an error that is not on the list of those that end only one channel, a fourth rebuild of one
  channel within 30 s) ends the device exactly as in 2.9. A Mac put to sleep with a game or WindowServer waiting on
  the GPU no longer loses the device or rebuilds a channel that was only stopped for the sleep: the kext answers NVK's
  channel note without waiting for the GPU's resume and reports the GPU as suspended until it lets RM calls through
  again, neither the thread that submits to a channel nor its doorbell ring makes an RM call or escape while it holds
  the channel's lock (only the recovery sequence calls RM with the lock held, besides the test hook when
  `NVK_FAULT_INJECT` arms it; for a channel that merely stands still it starts only once the kext no longer reports the
  GPU suspended, or after 360 s), and a stall seen while the GPU is suspended restarts the no-progress budget instead of
  rebuilding, at every sleep, for up to 360 s of awake time per sleep. A wait that stands still behind another channel
  that hangs without a fault (the graphics channel behind the upload channel, for example) has that channel rebuilt too,
  within the same limits; before, depending on timing, the application could lose its device instead, with the line "the
  channel it waits on is still progressing". Way back: `NVK_CHANNEL_RECOVER=0` in `nvrm610.conf`. See
  [docs/CHANNEL-RECOVERY.md](docs/CHANNEL-RECOVERY.md).
- After such a recovery the Metal command buffer whose work was dropped completes with an error instead of as success,
  and the process keeps its device; the plugin no longer waits for, and counts, the completion record such a command
  buffer can never write (eight of those used to switch command-pool reuse off for the process). A device loss that
  is not recovered now names its kind (firmware, bus, failed wake, ECC) in the log. Way back for the error report:
  `NVMTL_DROP_REPORT=0` in `nvrm610.conf` (the command buffer is reported as completed).
  When a rebuild drops a command buffer that carried uploads of managed buffers (the plugin's pre-submit prologue),
  those ranges are uploaded again at the next submission instead of staying unwritten on the GPU, and a line in the
  system log says so for the first such drop of a process and every 256th (`vk: after a GPU fault, U managed uploads
  and Z zero fills rode a dropped command buffer ...`). The GPU zero fills of new buffers that rode it (Private buffers
  and, by default, the VRAM copy of a new managed buffer) are not made again: such a buffer holds what the VRAM held
  wherever nothing was written or uploaded since.
- NVK's own warnings and errors no longer go to a file in `/private/tmp` from every Metal process. An installed
  plugin writes them to `nvmtl.log` only for a process started with `NVMTL_PROCESS_ENV=1`; from every other process,
  WindowServer included, NVK's errors and the two channel-recovery lines (`nvrm channel slot N rebuilt after ...` and
  `nvrm channel slot N not rebuilt: ...`) reach the system log with the prefix `NullMoth:`, while its other warnings
  stay out of it.
- In a program that uses NVK's Vulkan driver without the Metal plugin, a setting written twice in
  `/Library/GPUBundles/nvmtl/nvrm610.conf` now takes its last line, and a comment that names a setting is ignored: the
  rule a Metal process already followed and the one the documents describe. NVK's reader stays stricter than the
  plugin's: it reads only the first 8191 bytes of the file, where the plugin reads it whole, it takes a line only when
  the line begins with the name and its `=` with no blank before either, and the value is everything up to the end of
  the line, blanks included, up to 63 bytes. A line written `NAME=value` from the first column reads the same in both.
  This now holds for NVRM_ASYNC, NVK_DIAG, NVK_QUEUE_COUNT and NVMTL_GDQ_DQ too, which took their first line in such a
  program (for the settings NVK reads from the file itself; the eight `NVRM_610_` items keep the older rule there).
- The Metal plugin's fence wait (the one that gives a fence up as lost after ten budgets: 50 s in WindowServer) no
  longer counts time asleep, so a frame that was pending at sleep is not declared lost at wake.
- A texture the application releases while the GPU may still use it is now freed only once no command buffer that
  could still read it is open or in flight (the image grace, before only in the Cyberpunk 2077 profile). A late read
  is then a stale read of a dead texture instead of a GPU fault that takes the channel down. Way back:
  `NVMTL_IMAGE_GRACE=0` in `nvrm610.conf` (released textures are freed at once, as 2.9 did by default).
- Fixed: with the image grace on (the Cyberpunk 2077 profile turned it on), released textures were never freed and
  memory grew for the whole session. The grace counted some command buffers as begun and never as finished (the
  managed-upload prologue, the counter reset, submits that failed early), so the point it waited for moved further
  away every frame. It also could free a texture too early when a command buffer begun later was submitted before one
  that was open when the texture died. Both are gone: each command buffer is counted as finished exactly once, and a
  texture waits exactly for the command buffers open when it died.
- NVK shader compiler: the fixes Mesa made after 26.3.0-rc3 are built in. Mesa 26.3.0's fix for RTX 50 (a register
  could be read before its uniform load had finished) and its NIR fixes, plus eight NAK commits from Mesa main:
  barrier handling, RTX 50 matrix-instruction latencies, instruction scheduling, and flattening of small branches.
  2.9 kept them behind `NVK_EXTRA_PATCHES=nvk-upstream-nak`, a build option that did not build. Shaders are compiled
  again once after the update. When building from source, `NVK_SKIP_PATCHES` leaves one of the three patches out to
  find the cause of a rendering fault (see [docs/NVK-BASE-AND-HEADERS.md](docs/NVK-BASE-AND-HEADERS.md)).
- A copy from a buffer or from host memory into a multisample image is refused before it reaches Vulkan, and the first
  eight are named in the log: it is not valid Vulkan, and the copy engine raises an error for it that the GPU firmware
  may not recover from.
- System Settings > Displays could hang for seconds when it opened: the translator parsed a function's whole shader
  text again each time newFunctionWithName:constantValues: asked whether its visible functions must be linked first.
  The plugin now keeps the translator's definite answer per shader text; an answer read from the translator's error
  text is never kept.
- Programs built with an SDK older than macOS 11, which the kernel tells the system version 10.16, are no longer
  refused the NVIDIA device on macOS 15 and 26: the plugin then reads the version from kern.osrelease. A device the
  plugin refuses before Metal's own initialisation (the version check, or another card under `nvmultigpu=1`) no
  longer crashes the program: the uninitialised object is kept instead of being freed into Metal's dealloc.
- NVK writes its command and upload stream into plain system memory instead of memory the GPU caches. A small push
  written behind the previous one in the same cache line could be fetched by the GPU as the line was before the
  write, and the channel stopped with an invalid pushbuffer entry (Xid 32). On 2.9 this could take the GPU from a
  process at login (a transparent Dock, missing icons, a lock screen that crashed in a loop); with this release's
  channel recovery the same fault rebuilds the channel and drops the submission that was running (on the upload
  channel that work is replayed, and a second fault there ends the device). The command pool's own memory is
  unchanged and still follows `NVK_CMDPOOL_GPU_UNCACHED`.
- OpenGL applications run on NVK through Mesa's Zink on macOS 15.5 and later. The accelerator names
  `NVKZinkGLDriver.bundle` as its OpenGL driver (2.9 named Apple's AppleMetalOpenGLRenderer, and before macOS 15.5 it
  still does). The bundle forwards every GLD entry to Apple's renderer and hands each new OpenGL context to a CGL
  adapter, which points the context's dispatch table at Zink (Mesa 26.3.0) running on a private copy of this build's
  NVK. A context with an AppKit view presents through its CAMetalLayer, its frames copied on the GPU into the IOSurface
  of the layer's drawable; a context without a view, or whose layer cannot be used, presents through an IOSurface that
  NVK imports. No frame goes through a CPU copy. The dispatch table is generated when the driver is built, from the
  macOS SDK's public OpenGL headers. The adapter also reads and writes through offsets in GLEngine's private context
  data that the source fork recorded on macOS 15.8.1 (24H32) and 26.7.1 (25G241); before any such read or write it
  checks that the memory is shaped as recorded. That check runs before the process's Zink screen is made, so a refused
  layout opens no NVK device. A context stays on Apple's renderer when that check fails (the first refusal in a process
  is written to its standard error), when Zink cannot start in the process, when the adapter is missing, and in OBS:
  there OpenGL always stays Apple's and a screen capture asks for BGRA8 instead of RGBA16F (RGBA16F can fail before the
  first frame on this driver). A Zink context stays allocated while it is current in Mesa on any thread: a context that
  CGL destroys while it is still Mesa's current context on another thread is freed at that thread's next bind. When the
  installed AppleMetalOpenGLRenderer lacks a GLD entry the bundle forwards, the bundle points that entry at a stub that
  returns an error and forwards the others, unless it is gldGetVersion, gldCreateContext or gldDestroyContext, which the
  bundle calls itself (or Apple's renderer cannot be opened at all): then the bundle offers no OpenGL for the card, and
  a start with `nvzink=0` gives Apple's renderer back. From the HighDelay/nvidia-macos-driver-nvk fork; built and
  checked on Linux only, it has not run on a Mac. Way back: boot argument `nvzink=0` (Apple's OpenGL for every
  application, as in 2.9). See [docs/ZINK-OPENGL.md](docs/ZINK-OPENGL.md).
- Minecraft 26.3, MPV and other Vulkan applications that draw into a window can render with NVK instead of MoltenVK: the
  package installs a native Vulkan runtime at `/Library/GPUBundles/nvmtl/minecraft`, which renders with a private copy
  of this build's NVK and copies each frame on the GPU into the window's CAMetalLayer drawable. Nothing selects it by
  default; an application chooses it when it is launched. Minecraft takes the JVM argument
  `-Dorg.lwjgl.vulkan.libname=/Library/GPUBundles/nvmtl/minecraft/lib/libnvk_minecraft.dylib` and Vulkan in its graphics
  settings; MPV and other applications that use the Vulkan loader take
  `VK_DRIVER_FILES=/Library/GPUBundles/nvmtl/minecraft/nvk_bridge_icd.json` in their environment. Applications that do
  not ask for it are unchanged. Limits: one window per swapchain, B8G8R8A8 images (SRGB and UNORM), FIFO and immediate
  present modes, no two identical NVIDIA GPUs, and for every frame a CPU wait and two kernel calls that take the
  accelerator's lock (not measured on a card). The package carries the two guides beside `install.sh` as
  `MINECRAFT-NVK.md` and `NVK-APPLICATIONS.md`. From the HighDelay/nvidia-macos-driver-nvk fork, which reports Minecraft
  26.3 rendering through it on an RTX 2060 SUPER, macOS 26.7.1, with its own NVK build; this tree's runtime was built
  and checked on Linux only. See [tools/nvk-bridge/INSTALL.md](tools/nvk-bridge/INSTALL.md) and
  [tools/nvk-bridge/APPLICATIONS.md](tools/nvk-bridge/APPLICATIONS.md).
- Photo Booth's camera preview shows, upright. QuartzCore wraps a drawable that is not framebuffer-only with a usage of
  ShaderRead or Unknown, and Core Image refuses a destination without ShaderWrite, so the preview stayed black. An
  application's single-plane IOSurface wrapper that is not framebuffer-only now asks for a storage view before the
  family's VRAM is imported and reports ShaderWrite only when it has one; a writable wrapper whose linear import lost
  the storage view or the colour attachment gets a private image instead. WindowServer's surfaces keep the usage they
  asked for. In Photo Booth only, the image Core Image renders into the preview's drawables is reflected within the
  rendered bounds: the source fork reports that Photo Booth's call writes the rows bottom first. From the
  HighDelay/nvidia-macos-driver-nvk fork, which reports the preview visible and upright on an RTX 2060 SUPER, macOS
  26.7.1, with a USB webcam; built and checked on Linux only here. Way back for the reflection:
  `NVMTL_PHOTOBOOTH_FLIP=0` in `nvrm610.conf`; the usage rule has none. See
  [docs/PHOTOBOOTH-PREVIEW.md](docs/PHOTOBOOTH-PREVIEW.md).
- A drawable an application made itself (not a CAMetalDrawable) that answers `addPresentScheduledHandler:` is given an
  empty handler before it is presented. The source fork reports that Parallels' AsyncDrawable calls that handler from
  -present without checking it, so a command buffer that registered none crashed it there. Neither the source fork nor
  this tree has run it in a Parallels virtual machine; built and checked on Linux only here.
- Shader translator: an argument buffer's array of textures, and a single texture buffer in one, is read through the
  bindless heap, each element by its own handle, so a shader that picks a texture by an index known only when it runs
  gets that texture; declared as one array binding it had one descriptor, and a robust driver clamps the index to
  element 0. Only an array of texture buffers keeps its binding. The developer switch `NVMTL_NO_TIER2_FIXED=1`, which
  turns off the fixed-field argument-buffer path as a whole, keeps the descriptor path for these too.
- Shader translator: Safari's WebGPU shaders keep a texture the optimizer copied through a 64-bit integer, read the
  sample they ask for from a multisampled texture, and get a descriptor for a texture they only query for its size or
  sample count. The source fork reports that a WebGPU game in Safari drew its scene after these repairs, on an RTX 2060
  SUPER, macOS 26.7.1; it has not run here. See [docs/SAFARI-WEBGPU-TEXTURES.md](docs/SAFARI-WEBGPU-TEXTURES.md).
- Shader translator: `array<sampler, 1>` is a sampler array, every element of a sampler array keeps its own run-time
  sampler state, a sampler picked among 2 to 16 in a retry translates (the bound was 2 to 8), GL's ivec2 uniform read
  through a vec4 slot translates, and inlining a helper keeps a constant-expression argument (a vector splat, for
  example) whole.
- Shader translator: a texture or sampler kept in a field of a local object reaches its sample call; address-space
  parsing is strict; `array_ref<void>` table reads and stores of a device pointer into a local translate; the RGB10A2
  packing residual is computed without contraction.
- Shader translator: buffer-address lowering keeps the descriptor path for a shader whose address-table pointer
  reaches a call, a phi or a return, instead of changing types under it. For a buffer that keeps its descriptor it
  finds every chain of pointers first and then checks every use wherever the use stands in the function, so a loop
  that picks the buffer by a phi's back-edge value no longer leaves it on a descriptor the phi cannot use.
- Shader translator: static-initializer folding builds its token map once per function, and the plugin's sampler
  entry point shares one parse per translation.
- The shader translator's repairs above come from the HighDelay/nvidia-macos-driver-nvk fork and were built and checked
  on Linux only here ([docs/TRANSLATOR-VALIDATION.md](docs/TRANSLATOR-VALIDATION.md) lists the checks and where each
  ran).

#### Video
- RTX 50: H.264 video in Safari, QuickTime and FaceTime (green picture). The decoder from the vendored codec library is
  now used in the system decode service by default, after the card has decoded two built-in test streams bit for bit;
  if that test fails or cannot run, nothing changes and one line in the system log says why. The plugin loads on
  RTX 50 only a build of the library that carries this test; an older build is refused there. 2.9 listed this under
  "Experimental" (opt-in). Way back: `NVMTL_NVENCVT=0` in `nvrm610.conf`. Not yet confirmed on a card.
- Codec library: the card's generation is now read from the GPU itself. The library took the first NVIDIA PCI function
  with a device ID it knew, and the HDMI audio function of an RTX 40 and of most RTX 50 cards has an ID in the RTX 30
  range; where the system listed that function before the GPU, an RTX 40 could lose the library's encoders and its
  H.264 and HEVC decoders, and an RTX 50 could be treated as an RTX 30. Found by reading the code; nobody has reported
  the symptom.
- The codec library's build compares the NVIDIA structures it uses with the kernel module's version and stops on a
  difference.
- The plugin keeps the result of a compute kernel's grid-only scan per library text and kernel name, so a pipeline
  built for each converted frame (VideoToolbox's pixel transfer, a camera preview) no longer scans the whole module
  text again.
- A camera preview that stayed black: textures in the packed 4:2:2 video formats (BGRG422, GBGR422), in which
  CoreVideo hands camera frames to Metal, are made by default, as Apple's drivers make them. A camera frame whose rows
  would end past its surface's memory is skipped with a log line instead of being read, and before a frame is read the
  plugin waits, for a bounded time as for other IOSurface textures, for another process's GPU write of it or for
  WindowServer's. Way back: `NVMTL_YUV422=0` in `nvrm610.conf` (4:2:2 textures are refused, as in 2.9). Not yet
  confirmed on a card.

#### Kernel (NVRM)
- The RM's background work runs on one worker per GPU, in order, as on Linux, instead of one thread per item.
  Stopping the driver, shutting down and going to sleep wait for that work with a time limit. RM work items marked to
  be dropped during an unload are dropped only while a GPU is being taken down or put to sleep, as on Linux; before,
  they were also dropped while the GPU was waking up, so some work the RM queued around a wake never ran. Way back:
  boot argument `nvrmworkq=0`.
- The driver tells the RM the truth when it calls in from a context that must not sleep: the RM then takes its own
  non-blocking branches, small allocations come from a pool, and locks are tried before the blocking call. Way back:
  boot argument `nvrmatomic=0`.
- The RM's bottom half runs on a thread of its own per GPU, as on Linux, so a bottom half that waits for the GPU lock
  no longer delays the next interrupt. A requested bottom half is always run, also when the GPU stops at the same
  moment. Way back: boot argument `nvrmbhthread=0`.
- The RM's 1 Hz timer runs (bus presence check, NVENC session statistics). It pauses around sleep, RTD3 and shutdown,
  and is stopped and let go before the GPU's state is freed. Way back: boot argument `nvrmrctimer=0`.
- RM nano timers can no longer run their callback after the timer was destroyed (use after free), and timers longer
  than 71 minutes no longer fire early. Way back for the new wait only: boot argument `nvrmnanowait=0`.
- Sleep, shutdown and the stop of a GPU close the interrupt path and wait, bounded, until it is quiet. The stop and
  the full shutdown (`nvrmshutdown=full`) take the adapter down in Linux's order, `rm_disable_adapter` first; the
  waits that may take 30 s when a GPU is stopped are 2 s at shutdown, and when the system's 8 s run out the interrupt
  path is closed anyway.
- Two GPUs no longer corrupt each other's interrupt-context mark in the RM. The RM sees the real number of CPUs.
- The start-up waits for the displays, the arming of the accelerator and the boot raster measurement end as soon as
  what they wait for is there, instead of at the next whole second, and never later than before. Each phase is in
  `debug.nvrm_boot_*` and in the `nvrm-boot-timing` property. With `nvrmsettle=0` the bring-up can no longer begin
  before the driver's start has finished (it waits at most 5 s). On a card that deliberately settles for more than 5 s
  during start-up, lighting its display by hand in that window no longer counts the start against the safety that
  turns the driver off after two starts that never lit a display. Way back: boot argument `-nvrmoldwaits`.
- With Intel VT-d active (AppleVTD), the GPU is given bus addresses mapped through the DMA mapper, as the Linux driver
  does with an IOMMU, instead of physical addresses that the mapper translates to the wrong place. Every mapping is
  read back from the mapper before the GPU sees it, and the GPU is not started at all if a two-page self-test does not
  read back (that is not counted as a failed start). The accelerator's copies of GPU memory to the CPU go through a
  buffer mapped the same way. Without VT-d nothing changes. `sysctl debug.nvrm_dma_mode` and the `nvrm-dma` property
  show which case a machine is in; `nvrmdma=0` refuses to start the GPU behind a mapper (diagnostic). No run with a
  mapper on real hardware has been reported yet: see [docs/DMA-IOMMU.md](docs/DMA-IOMMU.md).
- The event wait reports why each wait returned and counts it (`debug.nvrm_evt_*`); a thread turned away because 256
  threads already sleep is told so instead of looking woken. Closing a process no longer races an RM event post into
  the freed connection: the close waits for a post in flight (as the Linux driver does) and keeps the connection
  rather than freeing it if the post never finishes.
- A user client that was never opened completely (its attach or start failed) no longer leaves a stale entry in
  NVRM's table of clients; later lookups read that entry after the client's memory had been freed.
- The table of the driver's kernel mappings has no size limit any more (it could fill on cards with a large BAR1,
  after which mappings were never released); lookups no longer scan it.
- `nvregdwords=` values reach the RM as written. Before, the function that converts them had no body and every value
  arrived as 0. The log lists each key with the value the RM received, once per boot; an entry with a second `=`
  (which the RM skips) is logged as ignored, and an empty entry (`;;`) no longer ends the list.
- Firmware bug checks and RM assertion failures are logged and counted (`debug.nvrm_bugchecks`,
  `debug.nvrm_rm_asserts`) instead of being ignored; neither stops the machine.
- NVRM counts every Xid the RM logs (`debug.nvrm_xid_count`, `debug.nvrm_xid_last`) and what NVK did about faulted or
  stalled channels (`debug.nvrm_chan_*`). The kext only counts and answers the GPU's power state; no new switch.
- The sleep line says how much VRAM the RM wrote to its save file during standby.
- NVRM.kext keeps itself loaded once a start has got past the framebuffer's class check on a card. Nothing it
  registers (sysctls, its work-queue thread, timers) was ever taken back, and the kernel unloads a driver by itself
  about a minute after its last object is gone (a card the DMA check declined, an unplugged card): the next
  `sysctl -a` then walked memory that was gone. `kmutil unload -b com.nullmoth.NVRM` is now refused with the kext in
  use. Installing, updating and removing the driver are not affected (they take effect at the restart).
- `nvrmctl go 2` after a second pass that failed once its interrupt source was set up is refused, instead of setting
  up a second interrupt source over the first (the first was then never removed and delivered interrupts to freed
  state when the GPU stopped).
- With `nvrtd3=1`, stopping a GPU (unplug) first takes it away from the idle power-down monitor, which could otherwise
  suspend or resume the GPU while it was being torn down, and went on calling into an object that had been freed.
- With `nvrtd3=1`, system sleep and shutdown no longer race the idle power-down monitor. Its polls went on during both
  and ran the GPU's own suspend and resume on another thread: the GPU could be suspended twice at once, resumed while
  the system went to sleep (it then woke marked running on a slot that had lost power, and was not resumed), or have
  its power resource switched off under the system's resume. The system now holds the GPU from the start of its sleep
  until the end of its wake, and for good at shutdown; the monitor's calls do nothing meanwhile, except the power-on
  that the wake itself asks for. The monitor also suspends only a running GPU and resumes or powers off only a
  suspended one, so a poll that a sleep and wake cut in two no longer switches the slot off under a running GPU, and a
  GPU whose wake failed is left alone.
- When NVRM declines a card early (the framebuffer refused this macOS's Apple classes), NVAccel does not hold the
  card's matching for its 30 s wait: it stops at once and logs `NVRM declined this card ... (nvrm-layout: ...)`.
- Only WindowServer, running under a system account (user id below 500, the rule NVRM's client table already uses),
  can take a compositor-class hold in the surface fence or NVAccel's pending marks and so delay another process's
  flips, copied frames and page-offs for up to 2 s at a time. A program that merely names its executable
  "WindowServer" cannot; the real WindowServer is unaffected.
- A surface-fence wait or look hands the writing process's pid and the state of its hold back only to the writer
  itself. Whether a surface (or a given process) has a write pending can be asked by any process.
- A process that fails channel registrations of the surface fence in a loop cannot flood the system log: only the
  first 8 failures since boot reach it; later ones go to the debug output, and `debug.nvrm_fence_map_fail` and
  `debug.nvrm_fence_map_mismatch` count them all.
- A GPU utilization reading that takes longer than 500 ms pauses the once-a-second sampler for 20 s, and three such
  readings in a row stop it for the rest of the boot; each slow reading is logged with its duration. The reading holds
  the RM's locks while it waits for the firmware, and a firmware that answered slowly could keep other processes,
  WindowServer and the shutdown waiting on them every second. The time includes the wait for those locks, so one
  reading held up by a mode set or a channel recovery costs 20 s of GPU usage statistics, not the rest of the boot; a
  fast reading, answered or failed, starts the count again.
- Hybrid laptop panels (NMIntelFB, NMGopFB; `nmhybrid=1` or `2`): each framebuffer sets its dependent ID and index
  before IOFramebuffer::start, as NVRMFB does, so it joins the shared controller at start and the first power change
  no longer meets a missing controller (a page fault at boot), and the accelerator finds the panel's index 4 when the
  panel publishes. A panel surface that cannot be mapped leaves the panel to the firmware framebuffer, and a failed
  IOFramebuffer::start of NMIntelFB undoes its timers, its display-firmware target and its backlight service through
  the same routine stop() uses.

#### Installing and the way back
- `install.sh` installs native OpenGL's `NVKZinkGLDriver.bundle` beside `NVMTLDriver.bundle`, backs it up with the
  other bundles and puts the earlier one back (or none, when there was none) on a rollback and in the fallback job;
  its runtime (`nvmtl/zink`) and the native Vulkan runtime (`nvmtl/minecraft`) go with `nvmtl`. A package without the
  bundle or without `nvmtl/zink` is refused before anything changes, because the accelerator names that bundle as the
  OpenGL driver. `uninstall.sh` removes the bundle with the others. Once the bundle is in place, `install.sh` asks
  macOS's trusted GPU-bundle lookup for it with `nvmtl/zink/gld_bundle_probe` (`tools/gld_bundle_probe.c`, from the
  HighDelay/nvidia-macos-driver-nvk fork), with `NVMTLDriver.bundle` as the control: when the lookup finds
  `NVMTLDriver.bundle` but not the OpenGL bundle, the install stops and rolls back; when it does not find
  `NVMTLDriver.bundle`, the installer says it could not check the OpenGL bundle and goes on. The installer and
  uninstaller of 2.9 and of earlier 3.0 packages do not know the bundle: going back with one of them leaves
  `/Library/GPUBundles/NVKZinkGLDriver.bundle` in place unless this package's `uninstall.sh` runs first.
- The installer's fallback job (it puts the earlier driver back when the new one cannot start) copies the earlier
  kernel collection out of the backup before it changes anything and puts it in place by a rename. Before, it copied
  over the collection in place without checking the copy: one that failed half-way (a full volume) left a cut
  collection and no job to try again; now such a failure changes nothing and the job stays for the next start.
- The fallback job looks for a lit display at each of its looks, whichever of it and the kexts starts first, and
  stays through the start in which it first finds the display lit, so a WindowServer that keeps dying in that start
  still finds the way back; it removes itself at a later start. A start count of two that already includes the
  present start (a card NVRM has claimed) no longer puts the earlier driver back while this start may still light the
  display.
- A second run of `install.sh` before a start that lit the display (to add the DLSS files or the boost switch, or a
  newer build over an untried one) keeps the first run's backup as the way back, with that backup's own macOS build,
  and says so in step 6b. Before, the fallback job would have put back the driver that was on trial.
- The boot-guard job ends an install's trial only on a desktop of this driver: a card NVRM claimed, and either its
  display path reported up (`debug.nvrmfb_guard_cleared`) or a display on another GPU. A desktop with no NVIDIA driver
  running only clears the start count, and a claimed card with nothing lit and no display on another GPU keeps its
  count. Before, any WindowServer that stayed up 45 s ended the trial, so the way back could be gone before the new
  driver had shown a picture.
- A WindowServer that keeps dying while the accelerator refused this macOS's classes (`nvaccel-layout` says
  `refused`) now turns the driver off and restarts once from the login window, as with an armed accelerator, when
  every display hangs off the card NVRM claimed.
- The installer's last lines no longer tell you to undo the install with `uninstall.sh`, which removes the driver and
  restores nothing: they name the folder that holds what was installed before and say to install its own package
  again.
- A rehearsal (`sudo env CHECK=1 ./install.sh`) that stops in step 3 no longer leaves a `/var/tmp/nullmoth.XXXX` folder
  with a copy of every third-party kext behind.
- The uninstaller also removes the hybrid laptop panel drivers (NMIntelFB, its virtual test build NMIntelFBVirtual,
  and NMGopFB) with the rest of the driver. Before, it took one left in `/Library/Extensions` for another product's
  kext and stopped ("the removal collection still lists the NVIDIA driver or selector") before changing anything, so
  the driver could not be removed from a hybrid laptop that had one.
- Another com.nullmoth kext that the uninstaller does not remove (NMHDA, NMKbdBacklight, NMDRM, NMSMN) left in
  `/Library/Extensions` no longer stops the removal with "the removal collection still lists the NVIDIA driver or
  selector": the two checks of the rebuilt collection look for the identifiers of the kexts the uninstaller removes,
  each as a whole name, and a kext of this driver still stops it. The AMD/Intel GPU driver's accelerator (NMXAccel)
  still stops it, now with its own reason: it uses this driver's Metal plugin, which the removal takes away.

#### Switches and reports
- `sysctl debug.nvrm_switches` lists the driver boot arguments a Mac has set beside the install line and marks with a
  `!` the ones an ordinary install does not need (an opt-out, a test hook, a development switch, a name this version
  no longer reads). `sysctl debug.nvrm_switches_nondefault` is 0 on an ordinary install. A Mac that behaves unlike
  others with the same card often still carries a switch from an earlier test; this shows it. Settings in
  `nvrm610.conf` are not part of this list.
- [docs/SWITCHES.md](docs/SWITCHES.md) lists every switch the driver reads (environment names, boot arguments,
  sysctls, flag files), what each does, and what is in effect when it is not set.
- The GPU policy file of a hybrid laptop (`/Library/GPUBundles/nvmtl-gpu-policy.txt`) takes a comment after a rule,
  as the example in [docs/HYBRID-LAPTOPS.md](docs/HYBRID-LAPTOPS.md) shows them. In 2.9 a `#` started a comment only
  at the start of a line, so a file copied from that example set no mode and named no process, and `mode apple` with a
  blank after it set nothing either. Such a file takes effect with this release: copied whole, the example gives
  `mode apple` (the last of its two `mode` lines) and its two process rules.
- Three read-only counters say what the accelerator's lock costs the threads that mark surfaces, a screen share's
  copies among its holders: `debug.nvaccel_surf_mark_lock_waits` (marks and grants that found the lock taken and
  waited), `debug.nvaccel_surf_mark_lock_wait_us` (the time they waited, in total) and
  `debug.nvaccel_surf_mark_lock_wait_max_us` (the longest wait; a value near 100000 means a page-off waited for the
  GPU with the lock held). They change nothing the driver does.
- Read-only counters for the granted blocks of video memory (`sysctl debug | grep nvrmfb_vram`): what the RM
  answered (`_rm_clears`), standby, resume and lost messages so far, one for each framebuffer of the card
  (`_power_events`), blocks not filled and filled (`_fill_skipped`, `_filled`, the time the fills took in
  `_fill_us`), whether a block was not zero although the RM clears (`_dirty`), what new blocks read after a sleep
  (`_wake_clean`, `_wake_dirty`), and the ready blocks: grants served from one and grants of a size in churn that
  found none (`_ready_hits`, `_ready_misses`), blocks that became ready and blocks that went back unused
  (`_ready_made`, `_ready_back`), bytes ready now (`_ready_bytes`). `debug.nvrmfb_vram_fast` shows whether
  `nvfbvramfast=0` is set.

### The ways back

One per change; each restores the 2.9 behaviour of that change only. Boot arguments go on the `boot-args` line and
need a restart. `nvrm610.conf` lines go into your own block at the end of `/Library/GPUBundles/nvmtl/nvrm610.conf`
(between `# --- my settings, kept on update ---` and `# --- end my settings ---`) and take effect when a process
starts: restart the application, or log out and in for WindowServer. Each is reviewed at 3.2: removed if the new path
has proven itself, renewed with a reason otherwise.

| Change | Way back | Kind |
|---|---|---|
| Surface fence (kernel, NVAccel, NVK, plugin) | `nvrmfence=0` | boot argument |
| Page-off wait without the accelerator lock | `nvaccel_pageoff_locked=1` | boot argument |
| Two-plane surfaces in VRAM | `nvaccel_surf_planes=0` | boot argument |
| Apple class layout checks (NVRMFB, NVRMAGDC, NVAccel) | `nvlayout=0` | boot argument |
| Start-up waits and start gate | `-nvrmoldwaits` | boot argument |
| RM work queues | `nvrmworkq=0` | boot argument |
| Answers by context (atomic context) | `nvrmatomic=0` | boot argument |
| Bottom-half thread | `nvrmbhthread=0` | boot argument |
| RC timer | `nvrmrctimer=0` | boot argument |
| Nano timer cancel wait | `nvrmnanowait=0` | boot argument |
| DMA through VT-d | `nvrmdma=0` (with a mapper: the GPU is not started; without one: nothing changes) | boot argument |
| Granted blocks of video memory: no second zero fill, blocks ready for a size in churn | `nvfbvramfast=0` | boot argument |
| Event wait in every process | `NVRM_EVENT_WAIT=0` (every process polls, WindowServer included) | `nvrm610.conf` |
| Channel recovery | `NVK_CHANNEL_RECOVER=0` | `nvrm610.conf` |
| Error report for dropped work | `NVMTL_DROP_REPORT=0` | `nvrm610.conf` |
| Image grace | `NVMTL_IMAGE_GRACE=0` | `nvrm610.conf` |
| H.264 decode on RTX 50 in the decode service | `NVMTL_NVENCVT=0` (the codec library off everywhere) | `nvrm610.conf` |
| 4:2:2 video textures (camera frames) | `NVMTL_YUV422=0` (4:2:2 textures are refused) | `nvrm610.conf` |
| Capture frames on private images: both planes copied back together | `NVMTL_SURFOUT_PLANES=0` | `nvrm610.conf` |
| Capture frames in VRAM: the other plane's tag follows a mark | `NVMTL_PLANE_FOLLOW=0` | `nvrm610.conf` |
| Done tags and holds before scheduled and present | `NVMTL_BUSY_DONE_EARLY=0` | `nvrm610.conf` |
| Photo Booth's preview reflected upright | `NVMTL_PHOTOBOOTH_FLIP=0` | `nvrm610.conf` |
| Shader-writable Managed surface wrappers drawn through a tiled image | `NVMTL_MANAGED_SURFACE_TILED=0` (drawn straight into the linear VRAM import) | `nvrm610.conf` |
| Native OpenGL through Zink on macOS 15.5 and later | `nvzink=0` (Apple's OpenGL over Metal for every application) | boot argument |

`sysctl debug.nvrm_switches` shows which of the boot arguments a Mac carries.

### Not turned on in this release, and why
- `NVMTL_MISALIGNED_BUFFER_COPY` stays off. It binds a copy taken when the command is encoded, so it reads stale bytes
  when an earlier command in the same command buffer, or GPU work still running, wrote that buffer; its write-back on
  completion can overwrite newer data in the same bytes; it costs up to a 4 MB copy per unaligned binding. A stale
  index read where 2.9 read zeros can turn into an out-of-range GPU access. It fixes one read-only case (Photoshop's
  duplicate-layer kernel) and is not safe everywhere.
- `NVMTL_ARGBUF_SAMPLERS` stays off. It changes the code the translator generates for samplers read from argument
  buffers; on by default it changed 3 of 12,446 shaders of the translator's test corpus, and those three have not been
  examined yet.

### Known limits of what is on
- Image grace: a released texture is not freed early when an application reaches its VRAM limit (no release and no
  wait at the memory limit yet). An application that keeps a Metal command buffer uncommitted holds every texture
  released after it until it commits or drops that command buffer, or until 1024 submits have completed (then the
  textures are freed as without the grace, and the log says so). The bindless slot of a released texture still
  follows the older rule (it waits for the submits in flight, not for the command buffers open when it died).
- Event wait: there is no setting that restores exactly 2.9's default (WindowServer sleeping, applications polling).
  To test one application without the event wait, launch it with `NVMTL_PROCESS_ENV=1 NVRM_EVENT_WAIT=0` in its own
  environment.
- With `nvrmfence=0` the driver's surface sync is 2.9's, but the other changes of this release stay on, and NVK still
  opens its idle fence connection in every process (see [docs/SURFACE-FENCE.md](docs/SURFACE-FENCE.md), "The one
  switch").

### For developers
- `./build.sh` builds the whole driver on a Mac with one command: it checks Xcode and the tools, installs what is
  missing after asking (on an Intel Mac the build tools are built from pinned sources into its working directory; on
  Apple silicon they are Homebrew formulas, with the Homebrew of the Mac's own processor, and build.sh itself
  upgrades and removes nothing you have; Python and Rust inside its own working directory),
  fetches every outside source at a pinned commit or checksum (`build/pins.sh`), builds each component as a logged,
  skippable step, and can stage a package from the 2.9 release package (another release, or a file that holds more
  than a release package does, is refused). A package it makes records the version of the
  checkout it was built from (the first heading of this changelog, or `NM_PACKAGE_VERSION`) as the installed version
  that Send logs and the app's update check read, not its base package's, and its headers do not name the account
  that built it. It never uses `sudo` and never installs the driver.
  Needs macOS 15.6 or later and Xcode 26. At commit 3933ff6 it built every component and staged a package with
  Xcode 26.5 on the Intel Mac and with Xcode 26.3 (SDK 26.2, the newest Xcode that runs on macOS 15) on GitHub's
  Intel runner, both named at the top of this section; Xcode 26.0 to 26.2 have not been tried, and on Apple silicon
  it has run only against stand-ins. See
  [docs/BUILDING.md](docs/BUILDING.md) ([en español](docs/BUILDING.es.md)).
- Building the kexts on an Intel Mac, with 2.9's scripts too, made an NVRM.kext that macOS cannot load: NVIDIA's
  makefiles gave the display code an x86-only option because the Mac's own compiler accepted it, and every return
  then jumped to a function that only Linux has. NVIDIA's RM and display code are now compiled with the same commands
  on every Mac, those of the Apple-silicon Mac the releases are built on, and a kext that imports such a function
  stops the build instead of being packaged.
- Building the shader translator stopped on an Intel Mac at its warning ceiling. The release script starts cargo from
  the Rust toolchain's own folder, and the strip that rustc then asks of Rust's `rust-objcopy` aborted for every crate
  it linked: that program finds Rust's libLLVM only when rustup's proxies start it. The script now tells cargo to
  leave that strip out (its own `strip -S` already removed the same debug map), and its check that the dylib names no
  build machine path reads every byte of the file, where macOS's `strings` read only its sections. At commit 3933ff6
  the translator built with no warning on the Intel Mac and built on GitHub's Intel runner.
- The Vulkan loader that `package/vulkan-for-apps.sh` offers to applications is built and installed in a temporary
  folder, and the loader's CMake compiled an `etc` folder inside it into the library as a folder to look in for
  manifests and settings: the library carried the folder's name, looked there on every Mac, and two builds of the same
  source on one Mac had different bytes, with 2.9's script too. The script now gives CMake `-DSYSCONFDIR=/etc`: the
  library still looks in `/etc`, and a library that names the temporary folder stops the build before it is copied
  out. With that option two builds on one Intel Mac had the same bytes.
- With an SDK older than 26.4 (Xcode 26.3 and earlier, the newest a Mac with macOS 15 runs) the plugin's device class
  now declares `supportsPlacementSparse` itself: `MTLDevice` declares it from the 26.4 SDK on, and a build with
  Xcode 26.3 stopped there. At commit 3933ff6 the plugin built with the 26.2 SDK of Xcode 26.3 on GitHub's Intel
  runner. With the 26.4 SDK or a later one nothing changes: that declaration is not compiled, and the plugin built
  with Xcode 26.5 on the Intel Mac had the same bytes before this change (commit 650db45) and after it (3933ff6).
- `tools/switches.tsv` is the registry of every switch, and `tools/test_switch_registry.py` fails when the source
  reads a name it does not have. A switch added from now on cannot be an opt-in or a writable sysctl: the test says
  what it may be instead (an opt-out of a default path with a review release, a test hook that does nothing unset, a
  read-only counter).
- `build/kext_check.sh` compiles every kext source without linking, on a Mac or on Linux, and checks every kernel
  symbol the kexts import against the export lists of macOS 15 and macOS 26. A symbol only Apple's own kexts may link,
  or one only one of the two systems exports, fails the check instead of failing to load on a user's Mac.
- `.github/workflows/ci.yml` runs that check, the host tests and the Mac builds on every push once Actions are enabled
  for the repository.
- `.github/workflows/full-build.yml` runs by hand (one run takes between one and four and a half hours of GitHub's
  Intel Mac): on GitHub's Intel runner it builds the whole driver with `./build.sh` in a new working directory (its
  tools and sources restored from GitHub's cache when it has them), stages a package over the 2.9 release package,
  runs the host tests and checks that nothing was taken from Homebrew. At commit ee7f332 every step of it succeeded.
- `tools/run_all_tests.py` says, after a failed test, when the checkout lacks the history that some of the tests read
  from git (a clone made with `--depth 1`, a clone without the release tags, a tree that is not a git checkout), and
  names the command that brings it.
- NVK's library came out with the same bytes in three builds on the Intel Mac (once with build tools made by hand,
  then with `./build.sh` at commits 650db45 and 3933ff6, which do not differ in NVK's sources or patches), and
  NVRM.kext's binary and the plugin had the same bytes at those two commits. The other kexts, the shader translator
  and the video library were not compared.
- On an Intel Mac `./build.sh deps` no longer needs Homebrew: it builds the missing build tools (ninja, CMake, Python
  3.14 with its OpenSSL, zstd and xz, pkgconf, SPIRV-Tools, glslang, LLVM 22 with clang) from pinned sources with
  `build/toolchain.sh` into the working directory, for macOS 15.0 and later, and checks that each links only macOS's
  libraries and its own. `deps` from an empty working directory took 46.5 minutes on the Intel Mac at commit 650db45
  (building LLVM about 30 of them) and 3 hours 53 minutes on GitHub's Intel runner at commit 96bf32c; it needs 10 GB
  of free space while LLVM builds.
  `NM_TOOLS_FROM` (`auto`, `source`, `homebrew`) chooses; `auto` keeps Homebrew's ready-made builds on Apple silicon,
  a path tried only against a stand-in for Homebrew. On an Intel Mac build.sh does not use Homebrew, even when it is
  installed: with tools from source only the toolchain's tools and your own settings are used, and no `brew` is
  called. With `homebrew` a tool you already have is used as before.
  Homebrew's SPIRV-LLVM-Translator is now taken only with Homebrew's own `llvm`; with any other LLVM the pinned
  translator is built against it, and built again when the LLVM in use changes; a translator built before `deps`
  noted its LLVM is built once more (on the Intel Mac at commit 3933ff6, with every other tool already there, that
  `deps` took 49 seconds).
- Every step of `./build.sh`, its SPIRV-LLVM-Translator build and its pip now get `SDKROOT` set to the macOS SDK of
  the Xcode chosen (your own `SDKROOT` is still not passed on). Before, `SDKROOT` was removed, and Apple's clang then
  searched `/usr/local/include` and `/usr/local/lib`, Homebrew's folders on an Intel Mac, before the SDK: two Intel
  Macs with different things in Homebrew could build different drivers. `./build.sh env` prints it and doctor names it.
  Your `TOOLCHAINS` (it makes xcrun pick another toolchain's compiler) no longer reaches the steps, and neither it nor
  your `SDKROOT` reaches `build.sh` itself: with an `SDKROOT` that named an SDK which was not there, a build stopped
  before its first step. doctor names the way to another SDK: its Xcode, with
  `DEVELOPER_DIR`.
- `./build.sh deps` with Homebrew: a `brew install` that ends with an error only because a file of yours was in the
  way of one of its links (a `python3` from python.org's installer, say) no longer stops it; it warns and goes on.
  A Python formula that `deps` unlinked (so that your shell's `python3` stays the one it was) is unlinked again
  when a later `deps` links it once more as a dependency of another formula. Both have been tried only against a
  stand-in for Homebrew.
- `./build.sh doctor` reads the `VERSION` of a base package given as an unpacked folder and names a release other than
  2.9 at once, instead of the package step stopping on it at the end of the build.
- The git calls of `./build.sh`'s build steps no longer read your global or system git configuration: your hooks, file
  system monitor, commit signing, filters and URL rewrites stay out of them (`fetch` already kept your hooks, file
  system monitor, attributes file and template folder out of its own).
- `./build.sh` builds nineteen components: `nvkbridge` (the native Vulkan runtime, `tools/nvk-bridge/build.sh`, after
  `boost`) and `zink` (native OpenGL, `build/build_zink.sh`, last) are new. Zink is built from Mesa 26.3.0 (commit
  0a6fa60a, pin `NM_PIN_ZINKMESA`), which `fetch` adds to the same Mesa git as NVK's, for macOS 15.0 and later, with a
  call to a newer macOS interface an error. The package step stops when native OpenGL lacks the `nvkbridge`
  component's two libraries, which are its Vulkan driver. Built and checked on Linux only: a darwin cross-build of
  Zink, and syntax-only compiles of the CGL adapter, the GL bundle and the runtime's three Objective-C++ files against
  a stand-in SDK (Zink's two AppKit files were not compiled); neither component has been built on a Mac.
- Native OpenGL's build needs Bison 3.0 or later (macOS's `/usr/bin/bison` is 2.3): `./build.sh deps` builds GNU
  Bison 3.8.2 from its release tarball, SHA-256 checked, on an Intel Mac and installs Homebrew's `bison` on Apple
  silicon. Without one, a build that includes `zink` (`all` and `package` do) stops before its first component's first
  step, naming bison and `./build.sh deps`. Bison's recipe has not run on a Mac or anywhere else.
- New host tests: `tools/test_nvk_bridge.py` (the runtime's interfaces with NVK and the plugin, and on a Mac with
  Vulkan headers its host test built with ASan and UBSan), `tools/test_cgl_dispatch.py` and
  `tools/test_cgl_gld_guard.py` (the dispatch generator's rules and the layout guard's checks, on synthetic input),
  `tools/test_photobooth_preview.py`, `tools/test_surface_drawable_usage.py`, `tools/test_format_capability_init.py`,
  `tools/test_a8_framebuffer_fetch.py` and `tools/test_buffer_tiling_features.py`. Programs that need a Mac with the
  driver (`tools/test_*_gpu.m`, `tools/test_custom_drawable.m`, `tools/test_surface_render_formats.m`,
  `tools/photobooth_surface_probe.m`) say in their header how to build and run them; `tools/run_all_tests.py` does
  not run them, and none has run.
- `NVK_SKIP_PATCHES` for `build/build_nvk.sh` leaves one of the three upstream patches out of an NVK build, to find the
  cause of a rendering fault; such a library says so in its build summary.
- `./build.sh` refuses a working directory, `NULLMOTH_BUILD_ROOT`, `NULLMOTH_RUST_BIN`, `NULLMOTH_CARGO_HOME`,
  `NM_OGKM620` or `NM_OGKM620_PRISTINE` whose path holds a character other than letters (of any alphabet), digits and
  `% + - . / @ _`, and the three release scripts refuse such a path themselves. They put these paths unquoted into the
  commands they hand to the build Mac's shell, where a folder such as `R&D` split a command and could delete a folder
  beside it.
- The commands the plugin, translator and video (nvencvt) release scripts run on the build Mac get the step's
  `SDKROOT`, so Apple's clang and ld no longer search `/usr/local/include` and `/usr/local/lib` (Homebrew's on an
  Intel Mac) before the SDK of the Xcode `build.sh` chose.
- With `NM_MESA_GIT` set, the plugin step stops with the reason when the folder the release plugin is compiled against
  (`<workdir>/build/gpu-src/mesa/include`) has no Vulkan headers, instead of starting and failing on a missing
  `vulkan_core.h` (or compiling against Homebrew's under `/usr/local`).
- What `deps` builds itself (the translator's cmake, the Python environment and pip, cargo's bindgen and cbindgen) no
  longer takes your compiler variables (`CC`, `CXX`, `CFLAGS`, `LDFLAGS` ...), cmake's search paths, `PYTHONPATH` and
  `PYTHONHOME`, the pip options that move or change what it installs (`PIP_TARGET`, `PIP_USER` ...), or `RUSTFLAGS`,
  `RUSTC_WRAPPER` and `CARGO_TARGET_DIR`; every step also loses `PYTHONPATH` and `PYTHONHOME`, and doctor names those
  that are set.
- A download with the wrong SHA-256 is refused with both checksums, the expected one and the one it had. A
  `glslangValidator` or `llvm-config` that fails when asked its version no longer ends `deps`' checks before they list
  what is missing.
- `clean` with a `NULLMOTH_BUILD_ROOT` outside the working directory finishes and names the folders it leaves there,
  instead of stopping halfway. `clean` and `help` work on a system other than macOS too, and the messages say so.
- The git that fetches the pinned sources never asks for a user name on the terminal: a remote that wants one fails
  at once with git's own message.
- After `package`, `./build.sh` prints the three install commands bare and the explanation on a line of its own. Pasted
  into Terminal, whose zsh does not take `#` as a comment, the old trailing `# a dry run: ...` reached `install.sh` as
  arguments, and it refused with "expected --payload directory or no arguments".

## 2.9

2.9 is the last 2.x release for macOS 15 Sequoia and macOS 26 Tahoe. It carries every fix we had for both systems, on GTX 16 and RTX 20, 30, 40, and 50 cards, plus a large set of new features. New features that have not been proven on enough hardware are **off by default** and listed under "Experimental" below: you switch them on one at a time from the 1401 Mac app.

Update from the 1401 Mac app, or download the package below. Restart after installing.

### Fixes

#### Login, desktop, and stability
- Login window on macOS 26 that showed a black screen with only a cursor: the login window's 10-bit frames now reach the panel.
- WindowServer hang after display sleep: a frame with nowhere to go is dropped instead of switching the asynchronous swap off (#470, #353).
- After a GPU loss, apps get a proper "device removed" error instead of a nil command buffer that made WindowServer abort. The log names the cause.
- Login hangs while shaders translate: Core Animation shader variants translate about three times faster (1.6 s to 0.48 s each), new libraries translate in the background once, and recorded variants are warmed into the cache before they are asked for.
- WindowServer keeps its shader cache across restarts (the cache folder is now chosen by a real write, so a sandboxed WindowServer no longer loses it). Settings panes open faster.
- Shader cache entries prove they are whole before they are used.
- Boot guard: the start counter is saved on every change, so a start that panics still counts (#529), and a WindowServer that keeps dying no longer counts as a good desktop.
- A failed mode set at boot no longer kills the framebuffer.
- A stuck GSP fails its call at the first timeout instead of holding the system.
- A texture that owns a view of its image is never moved between memory homes.
- Static samplers are created on the shader's own device (PR #530).

#### Boot, BAR1, and shutdown
- **Shutdown and restart**: the GPU is quiesced and the BAR layout is handed back to firmware at power-off and restart. This is aimed at the boards that would not POST after a macOS shutdown (#488) and at shutdowns that hung on the last frame.
- BAR1 placement is clipped to the limit macOS can map (Legion and other laptops whose BAR1 landed above it), and apertures are staged before any PCI write.
- A BAR missing from the device's memory list is recovered from assigned-addresses or the BAR register (boards without Above 4G / Resizable BAR, #344, #346).
- A firmware BAR1 above 8 GB is resized to 8 GB (16 GB BAR1 stalls, #23, #105, #155). A BAR1 that must grow past 4 GB is placed from the fallback range when other devices already sit above 4 GB.
- The headless deadline starts after the settle delay, and the boot hold is released when no connector has a display (hybrid laptops with the panel on the integrated GPU waited 40 s, #516).
- Bus mastering that firmware leaves on is switched off until the card is started.
- A card the model table lacks is named from its GPU profile instead of showing only a PCI id.
- A40 and L40 added to the device table. The table is checked against NVIDIA's 620.70.06 list.
- Small BAR1 windows: surface requests are rounded to size classes to reduce fragmentation.
- One user-client connection can carry many CPU mappings, which removes a per-mapping connection cost.

#### Displays
- 240 Hz, 300 Hz, and faster internal panels that were black above 60 Hz: the timing range now reaches 2.7 GHz pixel clock and 540 Hz (#429).
- HiDPI at high refresh: the highest refresh rates are kept when macOS lists more timings than fit (180 Hz HiDPI modes, #359), and the head-scaler limit is learned instead of assumed (#360).
- Derived half and quarter refresh rates for 240 Hz and faster panels (#478).
- 4K over DisplayPort-to-HDMI adapters: hardware YUV 4:2:0 for modes RGB cannot carry (general port of PR #459).
- DSC decisions follow the connector's own limit (DisplayPort versus HDMI), which fixes dual-screen flicker on some cards (#77).
- Two identical monitors each get their own registry location, so their colour profiles and identities no longer merge (#509).
- Login-window gamma is applied before login (#413).
- Hot-plug after a boot with no display attached now reaches the idle display ports (#288). A boot that finds no display looks again, 6 times, 3 s apart (#88).
- Mode changes wait a bounded time for the display gate instead of hanging in the kernel.
- Laptop brightness: a start-up request for full brightness no longer overwrites your saved level (#525). Internal panels with no registered backlight get eDP backlight control over DPCD.
- Rotated displays can offer other resolutions (`nvfbrotscale=1`, #138).
- EDID override files and smaller boot rasters are supported (idea from PR #468).

#### Video
- Hardware video encode on RTX 40 (Ada) and RTX 30 (Ampere): NVENC registers on those cards, and in the system decode service. VP9 and AV1 decode added (vendored mickys/nvenc 0.9.5, MIT).
- FFmpeg and other clients that ask for low-latency rate control are accepted (#275).
- Screen Sharing high-performance mode: the encoders and decoders answer the pixel-rate questions the Screen Sharing host and viewer ask.
- HEVC Main 4:4:4 profiles are named for clients that request them (opt-in).
- Hostile-stream fixes in the HEVC, H.264, and AV1 parsers, found by fuzzing.

#### Audio
- HDMI and DisplayPort volume and mute for display audio (opt-in `nmhdavol=1`, #501).
- The audio driver is built from the repository's own sources at release, with its pin table.

#### Apps and shaders
- Roblox and Photoshop compute shaders built with the newest Metal toolchain were refused: the translator now accepts the extra operand AIR 2.9 adds to atomics.
- Apps that build render shaders at start-up and then opened no document (#511): program-scope sampler tables are lowered.
- WebGL terminals that drew no text: texture and sampler reads are kept.
- DaVinci Resolve viewer shader: vectors loaded from struct-wrapped scalar arrays.
- Samplers chosen by a condition or an index are sampled per candidate instead of refused.
- Compute pipelines honour the stage-input descriptor (strides, offsets, formats).
- Pull-model interpolation (`interpolant<T>`), nested argument encoders, threadgroup vector arrays indexed as scalars, pointer-select comparisons.
- OpenGL: binding a texture or sampler no longer formats a string on every draw (#531).
- Compute kernels that write through unaligned buffer offsets get their bytes back (#498, opt-in).
- The accelerator publishes the revision and renderer id fields Apple's accelerators publish (#384, #379).
- Device capability answers (vertex amplification count, 32-bit float filtering, MSAA, texture LOD and others) are decided from what the Vulkan device really reports.

#### Power
- The GPU is boosted per burst of work instead of for the whole life of every app, so it can drop to its idle state between bursts (#522, #500, #314).
- Kernel logging is quieter: per-frame, per-query, and per-client lines appear only when asked for.

### New features
- **NVIDIA 620.70.06 base with a reproducible patch chain**: the kernel parts are built from NVIDIA's pristine open source release with every patch applied by the build, and the build refuses any other tree.
- **NVK on Mesa 26.3.0-rc3.**
- **Frame-rate limiter** per application (`NVMTL_FPS_LIMIT`), and your per-application list survives updates.
- **Driver settings** window in the 1401 Mac app: one catalogue of display and performance settings, with your card's PCIe link and BAR facts. Every change is confirmed.
- **Game Mode** (NullMoth's own): a full-screen game in front raises its own GPU priority and lowers background GPU work; everything is restored when you leave. Off by default, with a menu-bar indicator.
- **Colour controls**: output depth, format, range, and vibrance.
- **Metal features**: Metal 4 counter heaps, GPU-written indirect command buffer ranges, nested argument encoders, GBGR422/BGRG422 textures, framebuffer-fetch emulation for colour-reading pipelines.
- **Vulkan for apps** (opt-in): a Khronos loader built from pinned source, so applications can use the driver's Vulkan directly, including windows through `VK_EXT_metal_surface`.
- **Tools**: `nvmtl-smi` (read-only GPU report), `nvmtl_gpus` (cards, link speed, driver state), `nullmoth_settings` (safe settings editor), `nvmtl-boost` (run-time boost switch).
- **Fan speeds on every desktop**: 1401 gives AMD desktops SMCSuperIO too, with VirtualSMC and its sensor plugins built from source.
- **Ryzen laptops**: an optional power-limit profile (pinned, verified RyzenAdj build with a root-only kernel helper).
- **iPhone and iPad apps**: the Mac app's ARM apps page can take an unencrypted `.ipa` and turn it into a Mac app.

### Experimental (off by default)
The 1401 Mac app has a new pane, **Experimental drivers and features**, with a switch per item and Select all / Deselect all. The 1401 builder now places NullMoth's own EFI drivers that fit your PC into the EFI switched off, and this pane turns each one on or off. Please report what works.

- HDR10 and 30-bit output (`nvfbhdr=1`, `nvfb10bit=1`), variable refresh rate (`nvfbvrr=1`).
- Hardware ray tracing on the RT cores (`NVK_HWRT_QUERY=1`), including an 8-wide encoder and curve geometry.
- Hybrid laptops: render on NVIDIA and present on the integrated GPU's panel (Intel and AMD panel drivers, `nmhybrid`), per-app GPU policy.
- Idle GPU deep sleep on laptops (`nvrtd3=1`) and idle PCIe link power saving (`nvrmaspm=1`).
- Notebook power budget (`nvrmtgp=1`), power cap.
- Keyboard backlight for ASUS, Lenovo, Dell, and MSI laptops (`nmkbl=1`).
- Display audio volume (`nmhdavol=1`).
- DDC/CI monitor control (`nvrmfbddc=1`), surround (several displays as one, `nvfbsurround`), hot-plug polling for cards that send no display-change event (`nvfbhotplugpoll`), keep the frame on screen across mode changes (`nvfbkeepfront=1`).
- Multi-GPU: one card per process (`nvmultigpu=1`).
- Sparse textures, tile shading, vertex amplification, GPU-encoded indirect command buffers, 4:2:2 video textures, one-module function constants.
- Full shutdown of the resource manager at power-off (`nvrmshutdown=full`).
- Driver off on a Resizable BAR card with the firmware screen kept (`nvoffbar=1`).
- H.264 hardware decode in the system decode service on RTX 50 (green video in Safari; opt-in while we test it).

### Security
- A user client's token table grows as needed instead of pinning about 1.8 GB of kernel memory on first use.
- A use-after-free window in the accelerator's page-off wait is closed.
- DisplayPort AUX and EDID writes from user space are range-checked.
- Every file the driver writes under a fixed name is created without following links. Diagnostic switch files in `/tmp` count only when root made them.
- The GPU policy file and the experimental-driver source folder are honoured only when root owns them and nobody else can write them.
- The 1401 Mac app's root actions resolve their package against the app's own pinned list, never from the page.
- Firmware calls added in 2.9 check their own opt-in where they are made.

### Package and licensing
- NVIDIA's GSP firmware is no longer shipped. The installer fetches it from NVIDIA's own driver package (pinned SHA-256), and a 1401 stick carries that package for offline installs.
- The package no longer contains any third-party closed compiler components. Shader decoding uses NullMoth's own out-of-process decoder built from upstream LLVM.
- Every file in the package is listed in `SHA256SUMS`; the installer and the release gate refuse an unlisted file. Every program and library is signed.
- Licence texts and NOTICE credits added for every component and every community idea used. `docs/PROVENANCE.md` says what ships, from where, and under which licence.
- All compiler warnings fixed across kexts, plugin, translator, NVK, and apps, and every build stops if warnings rise.

### 1401 (Mac and Windows apps)
- Arrow Lake: a corrected MADT when firmware lists only x2APIC processors (#252, #64); extra processor devices hidden on HX laptops (#141, #110).
- AMD laptops: ALIB fix built per machine, 0xFF000000 MMIO whitelist, SSDT-PLUG tables off, relocation block when the installer needs it.
- DSDT repair: each deferred Scope is guarded, so macOS no longer drops the whole DSDT on some Lenovo and Dell laptops.
- Coffee Lake-H laptops: the firmware page every reported panic named is reserved (#328, #451).
- Resizable BAR: an installed EFI never resizes the GPU window above 8 GB, and the BIOS guide no longer contradicts itself.
- VMD and unusable internal drives are planned for (BIOS path per maker, reversible Windows preparation, external-drive option) instead of stopping the build.
- Full macOS installer option (verified, resumable download) for PCs where the online Recovery install fails. Finish a new macOS 26 install from Recovery when setup cannot draw (#86).
- Apple ID: every PCI Ethernet controller is marked built-in so it is en0.
- USB map import (your own UTBMap.kext), new serial, own recovery image, and own config.plist options.
- After a macOS update that left the driver parked, the Mac app says so (#484), retries the kernel collection build, and turns the driver back on.
- Updates keep your hidden auxiliary boot entries (#371). Vault is set to Optional (#153).
- Send logs: no more hang on "Waiting for your Mac password" (#369, #373, #525); flicker samples, display traces, WindowServer watchdog samples, and failed shaders are included; a per-frame log line can no longer fill the kernel log (#16).
- Clearer stops in the writer: antivirus removing built files, Windows "Access is denied" on the stick, Apple download failures, installer download errors.
- Stick logs name the board and CPU. The first-login report says whether the accelerator offered its Metal plugin.
- Intel Xe support updated (WhateverXe 0.8.0, 60 GPU ids). Community drivers reviewed against their licences.
- AMD/Intel side: H.264 and HEVC decode through Vulkan video where macOS has no hardware decoder, Mesa 26.3.0-rc3.

### Known limits
- Pre-GTX 16 cards (Maxwell, Pascal, Volta, Kepler) are not supported; the installer says so.
- Experimental items are unproven across cards. Turn on one at a time.
- Cyberpunk 2077 interior lighting on first launch is unchanged from 2.8.

Thanks to WatchAndyTW (DaVinci Resolve viewer shader), backdash756 (YUV 4:2:0 over DisplayPort-to-HDMI, PR #459), GeekAudio (BAR1 placement, EDID overrides), and steve12311 (static samplers, PR #530), and to everyone who sent logs, issues, and pull requests. Donations help keep this going.

## 2.8.1

- Updates no longer fail when GitHub's downloads are down. The Mac app now falls back to a copy of each release on nullmothsystems.com when a GitHub download fails, for both driver updates and its own updates. 1401 for Windows does the same for the driver package it puts on the stick. Every file is checked against the release's SHA-256 list and the NullMoth release signature, wherever it came from, and a file that differs by one byte is refused.
- The driver itself is unchanged from 2.8 (same kernel extensions, Metal plugin and Vulkan driver); only its version number moves so the app and the driver keep the same version.

## 2.8

- NVIDIA 620: the kernel driver now runs NVIDIA's 620.70.06 open kernel modules (was 615.78.08) with their GPU firmware. Two new cards come with it: RTX PRO 5500 Blackwell and RTX Spark N1X (249 device ids in all).
- NVIDIA 620: the CPU mapping hardening from 2.7.2 is carried over to 620's new mapping model. A system-memory mapping is now limited to the length the driver validated, tied to that mapping and its list, and a re-bind or close can no longer leave a mapping behind.
- NVIDIA 620: the display driver leaves the colour format to NVIDIA's modesetting, so a display mode that offers only YCbCr no longer comes up black.
- If /Library is not mounted yet when the driver starts, it loads its GPU firmware from a fallback copy.
- DLSS 4 / 4.5 (optional, RTX 20 series and newer): DLSS Super Resolution runs behind the MetalFX temporal upscaler, so a Mac game that uses MetalFX upscaling gets DLSS on an NVIDIA card. DLSS Frame Generation runs behind the MetalFX frame interpolator and takes its sizes and the game's real motion vectors from the upscaler the game attached. DLSS Ray Reconstruction runs behind the MetalFX denoised upscaler on macOS 26.
- DLSS files: the package carries no NVIDIA DLSS code. With the optional step on, the installer downloads NVIDIA's own files on your Mac, refuses any file whose SHA-256 differs from the pinned one, and compiles the DLSS GPU kernels for your card (about two minutes). Run it with `sudo env NULLMOTH_DLSS=1 ./install.sh`. RTX 20, 30, 40 and 50 series cards are covered. DLSS never runs inside WindowServer, and its data stays in a private per-user folder.
- Metal 4: command queues, command buffers, encoders, argument tables and the Metal 4 compiler work over the driver's Metal 3 objects, and the device reports the Metal 4 GPU family. MetalFX's Metal 4 upscaler and frame interpolator are served by DLSS.
- Games with ray tracing and frame generation no longer run out of video memory and crash: large automatic heaps (256 MB and up) get memory in 64 MB pieces as they are used instead of all at once. A 2 GB heap the game had made held 22 KB of data.
- Freezes in long game sessions are gone: freeing or reusing an image no longer scans every image the app ever made (hash index).
- A texture copy between two size-compatible formats is performed, as Metal allows, instead of being refused.
- Drawing into an attachment whose channel order differs from the pipeline's (BGRA into RGBA) works the way it does on Apple GPUs.
- Resources in a heap stay alive until the GPU work that uses them has finished.
- Less CPU time per draw: parallel render encoders record strided vertex buffers and heaps directly, and a buffer's GPU address is returned without a lock when it is already known.
- Fewer stalls: the Vulkan layer carves command memory from shared 1 MB slabs and reuses it across command pools instead of opening a kernel connection per pool.
- Ray tracing in Cyberpunk 2077: the game offers ray tracing only when the reported working set is above 6145 MB. On a card with at least 7.2 GB of memory the driver answers 6200 MB while ray tracing is available; a smaller card keeps its own answer, so it is never told it holds more than it has.
- Cyberpunk 2077 (ARM apps translation) is told macOS 27 so it offers MetalFX frame generation, which the driver provides on macOS 15 and 26. Measured on an RTX 5060 at 1080p, all ray tracing on, DLSS upscaling and frame generation: 85.84 fps average, 68.18 fps minimum (was a crash, or about 47 fps without ray tracing in 2.7.2). Geekbench 7 Metal on the same card: 116,053.
- Shaders: an atomic on an unindexed local array works, and translation is faster (each shader is parsed once).
- Managed no-copy buffers of every size get a copy in video memory.
- OpenGL: the uniform buffer reads its slot through the handle check.
- If Cyberpunk 2077 had ray tracing or frame generation greyed out or off before, turn Ray Tracing and Frame Generation back on once in its graphics settings: the game remembers its own earlier answer.
- Known issue: some interiors in Cyberpunk 2077 can render too bright on the first launch after a restart. Quit and launch again. A fix is planned for 2.8.1.

## 2.7.2

- Apps built for macOS 26 that read a pipeline's required threadgroup size no longer quit with an "unrecognized selector" error. Compute pipelines report the size they were created with, and a required size also sets the pipeline's maximum threads per threadgroup.
- The depth bounds test works. Before, setting depth bounds on a render encoder did nothing, so pixels that should have been skipped were drawn. Pixels are now skipped when the depth already stored in the depth attachment is outside the bounds, as on Apple GPUs.
- Texture views can be made from a texture view descriptor (macOS 26), with a pixel format, mip levels, slices, and a swizzle.
- Setting a colour attachment map on a Metal 3 render encoder no longer stops the app. As on Apple GPUs, the map has no effect on a Metal 3 pipeline.
- Fragment barycentrics are reported only on GPUs whose Vulkan driver provides them, so an app never builds a pipeline the GPU cannot run. On AMD GCN 3 and 4 the Vulkan driver now uses half-precision arithmetic where the hardware has it.
- The 2.x driver is built for macOS 15 and macOS 26. On any other macOS version it creates no Metal device.
- AMD and Intel GPU driver (test program, opt-in): AMD GPUs no longer stop at start-up after "register mmio size" (a PCI bridge lookup could loop forever). Safe start-up for RDNA 2, 3 and 3.5, GCN 1 to 3, Polaris cards macOS has no driver for, and Vega APUs. AMD GPUs that Apple's own drivers already run are left alone.
- Security: fixed races in the NVIDIA kernel driver that let a logged-in user crash the Mac or corrupt kernel memory by binding, mapping, and closing GPU connections at the same time.
- Security: the display driver no longer publishes kernel addresses where any app can read them.
- Security: GPU profiling and channel priority now need an administrator. They were open to every user.
- Security: a setuid or root process ignores the driver's environment settings, and a release build only loads a library named by one from a root-only folder.

## 2.7.1

- OpenGL apps: setting the same render pipeline again inside an open render pass is skipped, so draw-heavy OpenGL apps spend less CPU per draw.
- ARM apps: the installer keeps /Library/NullMoth readable, so the ARM translator can be reached without the `sudo chmod 711 /Library/NullMoth` workaround.
- Known issue: Cyberpunk 2077 can still render interiors too bright on some launches. Quit and launch the game again.

## 2.7

- ARM apps (opt-in, experimental): an optional translator for Mac apps built only for Apple silicon. It compiles an app's ARM code into Intel (x86-64) code ahead of time, so the app can run on Intel and AMD hackintoshes. Turn it on in 1401 › ARM apps (opt-in); on Windows, tick Also set up ARM apps (opt-in) on a fresh install. Your original app is never changed, and Turn off removes the translator and every translated copy.
- ARM apps works today for command-line tools (lz4, xxhsum, cmake and z3 give byte-identical output to Apple silicon). Apps with windows are experimental; Go, Flutter, Java/Electron and SVE video codecs do not work yet, and games are not supported in 2.7. It needs a CPU with AES instructions and the Xcode Command Line Tools.
- macOS 26 Tahoe has sound. Tahoe ships no HD Audio driver, so PCs on it had no output device at all. The driver package now carries NullMoth's own HD Audio driver and installs it on Tahoe when your PC has an HD Audio controller: onboard Realtek and other codecs (rear and front analog) and NVIDIA HDMI/DisplayPort audio. macOS 15 keeps Apple's own driver and is not touched.
- If Logic Pro, GarageBand, or MainStage says "Selected Driver not found" after updating, open its Settings › Audio and pick your output again. The old device was Apple's, and it no longer exists.
- Kernel panics fixed. The driver kept a saved interrupt state inside a shared lock object, which macOS reported as "element modified after free" and which could end in a panic. The state now stays with the caller, as NVIDIA's code expects.
- Games that run several compute passes at once no longer hit random GPU faults (FAULT_PTE / FAULT_PDE) or show garbage lighting. Concurrent compute work and render passes are now ordered correctly.
- Resources declared with useHeap: and residency sets stay alive until the GPU is done with them. Before, a heap could be freed under work that still used it.
- Freed buffers, images, and views keep their GPU address until the work that was in flight when they died has finished.
- Long sessions no longer run out of GPU connections: the command-pool cache is bounded.
- The memory a game is told it may use leaves a reserve on the card, so big games run out of VRAM less often.
- A shader that uses a heap binding its heap was not created with is refused instead of running into a GPU fault.
- Metal ray tracing: acceleration structures build on the GPU, instance transforms honour the matrix layout the app declares (before, every column-major transform was wrong), builds are thread-safe, and VRAM is checked before a build.
- Bindless argument-buffer heaps carry storage images and texel buffers, so a shader can write a texture it found through a heap.
- 10-bit BGR10A2 textures can be written by compute shaders (HDR and 10-bit output render correctly).
- sRGB images keep their storage use through a compatible UNORM view, so writes to them land.
- A render pipeline that does not match the open render pass restarts the pass with its attachments kept, instead of the draw being dropped.
- Indirect compute dispatches prepare the textures declared with useResource:usage: first.
- Shader translation: make_filled_simdgroup_matrix, barrier scopes, byte-addressed threadgroup memory, pointer phis, device-address arithmetic, and pixel-coordinate sampling now translate correctly.
- Functions that call visible functions translate once their callees are known.
- Application profiles: the driver applies per-application settings and shader fixes for one named application, in that process only, with its own shader cache. Cyberpunk 2077's light clustering now renders correctly at about 40 fps on an RTX 5060.
- GPU compute: convolution kernels use the tensor cores, with a speed gate that keeps Apple's MPS kernel wherever ours is not clearly faster.
- Shader compiler: small per-thread arrays stay in registers, plus better scheduling, conversion, and load handling.
- Compute pipelines skip buffer bounds checks (graphics keep them), and small private buffers and images share VRAM blocks.
- Less CPU per draw and dispatch: binding tables in fixed arrays, fresh system memory is not zeroed twice, and buffers are parked for reuse.
- Diagnostics that cost speed are compiled out of the release build.
- Known issue: Cyberpunk 2077's first launch after a restart can render interiors too bright while the game streams its files in. Quit and launch the game again.

## 2.6.6

- Launchpad and Chrome icons are no longer black. 2.6.5 changed how textures made from an IOSurface with no stated use were set up, and shader writes to them went nowhere (PR #461, thanks Geek).
- Logic Pro no longer crashes when it opens the Dolby Atmos mixer. A texture upload reached the GPU with no image behind it; the copy is now refused, and the log names the texture so the cause can be found.
- QQ: avatars keep their colour (a clear that finished late could wipe a new upload, PR #458, thanks Geek) and SVG toolbar icons draw (the shader translator refused ANGLE shaders with an empty inline-assembly statement, PR #457, thanks GeekAudio).
- Hades menu text draws: the shader translator kept a pointer read through a branch merge (PR #466, thanks mickys).
- Games that ask for rasterization-rate-map coordinates, such as Darkest Dungeon II, get their shaders: the driver reports that it has no rate maps and the translator passes those coordinates through unchanged (PR #469, thanks mickys and Geek).
- A failed buffer-to-texture copy names its format, sizes and region in the log (PR #465, thanks mickys).
- The installer notes when the boot-args have no AMFI argument. Without one, WindowServer refuses the Metal driver and the desktop comes up with no Metal device.
- Tahoe PCs whose integrated GPU drives a screen no longer stop at a black screen with a cursor: a GPU macOS no longer accelerates does not count as the one showing the desktop, so WindowServer starts.
- macOS Sequoia 15.0 to 15.4 can install the driver again. The Vulkan library in 2.6.2 to 2.6.5 was marked as needing 15.5 though nothing in it does.
- Security: the driver package is signed, and the Mac app installs a driver update only with a valid NullMoth signature. An installer rollback no longer reuses a firmware folder another account could have placed in /Users/Shared.

## 2.6.5

- WebGL pages with shadow-casting lights draw their shadows: when a page's shadow helper read its sampler through ANGLE's texture table, the shader translator refused the shader, and a second light's shadow map was read as empty (#449, thanks Luminous418).
- WindowServer does less work per frame: compositor draws no longer rewrite descriptors for the framebuffer-fetch set (PR #439), and command buffers whose completion slot was reused are recognised instead of shutting down the pool (PR #446). Drawables are presented on their own queue, which ends a white-window hang with the Metal HUD on (PR #445). Screen recordings keep their frames in order (PR #438). Thanks GeekAudio (mickys).
- Photo Booth's preview is no longer black (PR #433, thanks daggersinmybackk).
- Vivaldi, Opera and Arc get Metal through ANGLE, found by the libraries the process loads (PR #450, thanks SameerAlSahab).
- Shader translator: depth gather-compare (Civ VI shadows, PR #447), sampler choices across compare chains (PR #448), cube samples through 2D-array views (Stellaris, PR #434), Apple's GL helpers inlined (PR #435). Thanks GeekAudio (mickys).
- Plugin: Apple's ARGB8 texture format for sampling and uploads (Homeworld, PR #440), depth texture types and sizes reported correctly (Northgard, Dome Keeper, PRs #441 and #432), array lengths kept in struct reflection and encoders return their command buffer (Hades, PRs #437 and #436), block-aligned compressed mip copies (Cult of the Lamb, PR #444), uploads ordered after GPU reads (PR #442), and a trace-ID lock conflict removed (Soulstone Survivors, PR #443). Thanks GeekAudio (mickys).

## 2.6.4

- three.js scenes with two or more shadow-casting lights no longer lose their WebGL2 context in Chrome, Brave and other browsers: the shader translator refused a shader's array of samplers (#426, patch by Luminous418). Each sampler in the array now reaches the shader with its own state. Known: WebGL shadow maps still draw the whole shadowed surface dark, with one light or several (it did in 2.6.3 too); that is the next fix.
- Arrays of textures read the right texture: every element past the first read the first one, whether the index was fixed or computed in the shader. Shadow maps, texture atlases and material arrays in games and WebGL pages were affected.
- Machinarium, and other apps whose shaders were built with older Metal tools, draw their scenes: the translator did not recognise a sampler written directly into the shader code, and refused the shader (patch by daggersinmybackk).

## 2.6.3

- Infuse and other players that scale video with Apple's Lanczos filter play again: the shader that filter uses was refused, and the app stopped with "Unable to get MPS kernel lanczosVerticalScale" (#405). Its output now matches a real Mac's pixel for pixel.
- Geekbench's GPU benchmark runs every test, including the machine-learning ones that use depthwise convolution (#377, PR #385, thanks GeekAudio and Geek).
- Unity WebGL games and other web pages whose shaders pass a buffer to a helper function render again (#406). Chrome no longer loses its WebGL2 context on pages that write only to textures (#419, patch by eisda).
- DaVinci Resolve no longer stops at startup on a GTX 1660 Ti: the shader translator read debug notes in Apple's shader code as part of the instructions (PR #418, thanks fayzetwin1).
- llama.cpp gets correct results instead of zeros (#401).
- Dragging windows and copying between apps no longer stalls on cards with a small 256 MB BAR1 window: surfaces keep the memory they were given between copies and are read back directly (#368, #399, patch by mrtzygt). A setting you put in your own block of nvrm610.conf now wins over the shipped value.
- Window dragging no longer stutters, and a crash when a window closed during a copy is fixed (#251, #340, #412). The Dock and Control Center blur is smooth instead of blocky (#363). HiDPI "looks like" sizes appear in Displays (#354). Scrolling in Chromium apps is smoother, and System Settings panes open faster. These come from watchthelight's NvGfx (thanks watchthelight).
- Display rotation in System Settings (#230, #307, #350, #355, PR #398, thanks razvanccr).
- HDMI and DisplayPort audio follows the right display when several are connected, and comes back after a mode change (PR #366, thanks Ersiter).
- A laptop's built-in screen keeps the brightness you set across restarts and power changes (PR #312, thanks Ersiter).
- Activity Monitor shows how busy the NVIDIA GPU is (PR #380, thanks GeekAudio).
- Boards whose firmware gives the GPU no 64-bit window keep BAR1 where the firmware put it, and a BAR1 of 512 MB or more that was not moved can flip (PRs #408 and #409, thanks mischa85).
- Microsoft Edge and other Chromium browsers get Metal through ANGLE by name (PR #404, thanks DoveKi).
- Minecraft shader packs BSL and Complementary work, and OpenGL apps use uniform buffers (PR #343, thanks Dat 'Del4y' Nguyen).
- The shader translator is faster (PR #187, thanks modest-tao).

## 2.6.2

- The same driver as 2.6.0, released with fixes to the 1401 apps for Mac and Windows.

## 2.6.1

- The same driver as 2.6.0, released with the redesigned 1401 apps for Mac and Windows.

## 2.6.0

- An update of the driver takes one restart. macOS shows a notice that the extensions were updated while the installer replaces them, and the installer used to answer it with "allow the extensions, then reboot once more"; the new driver already loads on the first restart (measured: every extension approved, none held back), so on an update it now says one restart is enough. A first install still asks you to allow the extensions once.

## 2.5.5

- Discord videos play again. 2.5.4 stopped registering the hardware video decoder in every sandboxed process, and Discord decodes inside its own sandboxed GPU process, so its videos did not play at all. Only Apple's sandboxed apps (Safari, QuickTime, Preview) now hand decoding to macOS's video decoding service; Chromium and Electron apps decode in their own process as in 2.5.3.
- Remote-desktop apps such as UU Remote show the picture: the hardware H.264 and HEVC encoders marked every keyframe "not a sync frame: false" where Apple's encoders leave the mark out, and apps that look for the mark's absence never found a keyframe on every card generation (#388, #358, thanks a1461750564 and sherves for measuring it). The encoders for the newest cards also load on macOS 15.0 to 15.4 now; they asked for 15.5.
- Screen Sharing and other screen-capture apps show the screen instead of black: a texture made from a buffer could only be viewed as its own format and its sRGB twin, and screen capture views a BGRA surface as RGBA when it scales or converts.
- Brave and Microsoft Edge get Metal through ANGLE the way Chrome does (PR #381, thanks SameerAlSahab; Edge tested by sherves).
- On a card with a small BAR1 window, NVAccel no longer logs every refused allocation: a third of all log lines on an idle desktop (#387, thanks wwwljxw). It logs the first eight and then every 256th with the running count, as the framebuffer does; `debug.nvaccel_vm_refused` keeps the exact total.

## 2.5.4

- Desktops whose only graphics card is the NVIDIA card no longer loop on a black screen when the card's display does not come up: WindowServer used to stop at start with "Failed to create MetalDevice" every ten seconds until the startup guard switched the driver off. It now starts on the firmware's framebuffer, the desktop appears without acceleration, and the first-login report sends the logs that show why the display did not come up.
- Discord gets Metal through ANGLE the way Chrome does (PR #348, thanks lukeitsback).
- Safari, QuickTime, Preview and other sandboxed apps decode H.264 and HEVC through macOS's video decoding service instead of failing inside their sandbox (PR #349, thanks lukeitsback).
- The installer accepts a /Library folder whose access-control list only takes access away, and names any entry that would let another account write there.
- When a kext you added to /Library/Extensions stops macOS from building its kernel collection, the installer names that kext and says how to move it, instead of only "test kernel collection build failed".
- The package carries the licence texts of everything in it (NVIDIA's firmware licence, the open kernel modules' licence and Mesa's), installed to /Library/NullMoth/licenses; the uninstaller removes them.

## 2.5.3

- NVIDIA's driver options given as `nvregdwords=` no longer stop the startup. From 2.5.0 to 2.5.2 any option set this way ended the boot with a kernel panic in the driver; the options now reach NVIDIA's registry, as the 2.5.0 notes describe.
- HDMI and DisplayPort audio, stated precisely: on macOS 15 the driver gives AppleHDA the display's audio link, and the display shows up under Sound > Output. macOS 26 removed AppleHDA, so on macOS 26 this package does not provide display audio by itself.

## 2.5.2

- HDMI and DisplayPort audio: a connected display shows up under Sound > Output with no setup. The driver publishes the display-to-codec link AppleHDA needs, keeps it current through hot-plug and monitor swaps, and lets a machine whose display audio is on another codec pin name it (PR #284, thanks Hi-Jiajun; PR #313, thanks Ersiter).
- Laptops whose NVIDIA bring-up takes longer than 40 seconds keep their desktop: the startup hold waits while the GPU is still coming up, up to 120 seconds (`nvrmholdcap=<seconds>` sets it) (PR #317, thanks watchthelight).
- Machines with several NVIDIA cards can pin the desktop to the card that drives the boot screen, and the choice survives updates (opt-in, see docs/AUTO-PRIMARY-GPU.md; PR #247, thanks is-pan).
- High-refresh displays: `sudo sysctl debug.nvrmfb_flip_latch=2` checks each flip at the next present instead of waiting for it, giving back about half a refresh period per frame (opt-in; #300, thanks xiaoming-crypto).
- The system log is quiet again: the driver logs failed GPU allocations, not every successful one (#242).
- Apps using Vision barcode detection (QQ and others) and Geekbench's Background Blur no longer sit at full CPU for minutes compiling one MPS kernel (#334, #111). That kernel still does not run on the GPU yet.


## 2.5.1

- Apple TV plays video again, and dragging windows no longer kills apps like Telegram: CoreAnimation's extended-range video formats (BGRA10_XR / PBGRAXb and BGR10_XR) are supported; the driver refused them, so video and some window pipelines failed (#309, #280).
- Shader writes to sRGB textures land, encoded as on a real Mac; they were silently dropped, which broke Blender's edit-mode viewport (#321).
- Scaled and HiDPI desktop modes work: a desktop size different from the display's timings is scaled by the GPU's head scaler instead of being refused (PR #273, thanks Leaf-lsgtky and lsCoding666, #221).
- Faster window updates: CPU-drawn surfaces upload from their own pages through the copy engine (PR #296, thanks mickys).
- GPU temperature in iStat and other monitors on cards whose sensor register answers (tested on Ampere; PR #323, thanks GeekAudio).

## 2.5.0

- Built on NVIDIA's 615.78.08 driver (from 610.57.04): NVIDIA's own kernel driver, display code and GSP firmware for every card, with all of NVIDIA's fixes since 610, including the blocky corruption in The Last of Us Part II.
- New cards: NVIDIA T400, T500, T550, T600, T1000 and T1200 (desktop and laptop), and the second RTX 5070 device ID.
- The GPU's identity now comes from the PDI NVIDIA stores in the card's PCIe configuration space, the way NVIDIA's driver reads it; older boards fall back to the previous method.
- NVIDIA's driver options work on macOS: the boot-arg `nvregdwords=Key=Value;Key=Value` is NVIDIA's `NVreg_RegistryDwords`, read by NVIDIA's own parser. `nvregdwords=RmDisableDisplayGlitchPerfLimit=1` is NVIDIA's new lower idle power with several monitors (it can glitch the display briefly when clocks change).
- 12-bit HDMI deep colour: the boot-arg `nvmaxbpc=12` allows it on displays that report it. The default stays at 10 bits, as NVIDIA ships it.

## 2.4.4

- Hardware H.264 and HEVC encode on every GTX 16 card: GTX 1650, 1650 SUPER, 1660, 1660 SUPER and 1660 Ti use the same encoders as RTX 20.
- HEVC encode on GTX 16 and RTX 20 cards keeps going past the second frame; it stopped with an error from frame 2 on.
- Laptops whose screen is on the Intel or AMD GPU offer the NVIDIA Metal device about 8 seconds after startup instead of 60, so apps opened at login see it too.
- WindowServer no longer crashes in the Vulkan driver when a binding holds a handle that cannot be a live object: the slot gets a placeholder and the log names it. Two RTX 3060 machines restarted WindowServer a few seconds after each login on this.
- An app no longer freezes for good when one finished GPU job is missing its completion mark (#322): Terminal and iTerm2 printing fast stopped redrawing and taking input until force-quit.
- Shaders that call functions through a visible or intersection function table load again; they returned nothing since 2.3.6 (PR #318, thanks watchthelight).
- World of Warcraft starts again: since 2.3.6 it showed a black screen and "unable to start up 3D acceleration", and once past that the terrain lighting shader faulted (PR #304, thanks razvanccr).
- Apps built on wgpu (Tern and others) start again: a fragment shader that clears the padding of a local struct failed to translate and the app stopped at launch (#330).

## 2.4.3

- Fix a desktop freeze followed by a kernel panic ("thread_call group 'high' reached max thread cap") a few seconds to a few minutes after logging in (#295). The display driver started a new kernel thread for every vblank and every cursor move while an earlier one was still waiting; each of its callbacks now runs one at a time.
- Metal apps translate a shader they have already seen without hashing it again: the digest kept on the translated text was never reused (PR #294, thanks watchthelight).

## 2.4.2

- Fix the black screen and the startup that stops on the verbose text after updating to 2.4 or 2.4.1. The display driver checks for a part of the display interface that the 2.4 and 2.4.1 core driver was built without, so it refused to start and the screen never moved off the startup text. 2.4.2 builds the core driver with it again; nothing else changes.

## 2.4.1

- Fix a black screen after updating to 2.4. The 2.4 package carried a framebuffer kext built from an outdated copy of its source, missing the display code the earlier releases had; 2.4.1 ships the complete framebuffer with the 2.4 changes.

## 2.4

- Hardware H.264 and HEVC video encode on RTX 20 (Turing) and RTX 30 (Ampere) cards, selected automatically per card; earlier builds used the hardware encoder on RTX 50 only.
- Security hardening: caller values are bounded, each client is serialised, and kernel addresses, registers and firmware stay private.
- macOS 26: the system icons and the glass menus and panels draw correctly again.
- Install from a root-owned copy of the package, with the previous driver kept as an automatic fallback.
- The kernel driver reads the power source (battery or mains) and reports it to the GPU firmware.
- Clear a stale shader cache left by an earlier build, which could show a black screen at login after an update.
- A boot counts as good once the desktop is up.

## 2.3.6

Desktop and windows
- Dragging and scrolling windows: a surface whose video memory had been set aside is put back before its update is
  recorded, so the driver no longer copies the whole window back to system memory on every frame (PR #245, thanks
  HighDelay; #253 measured the same readback on an RTX 5070 Ti at 4K)
- Moving the mouse no longer stalls the compositor: the cursor position is applied on its own thread instead of under
  the system's cursor lock, and OpenGL apps wait for the display's real refresh rate instead of 60 Hz on faster
  panels (thanks pengubow)
- Core Animation passes that read the colour attachment with framebuffer fetch off now read a defined image instead of an
  unbound descriptor, which could put garbage on screen inside WindowServer (PR #270, thanks watchthelight)
- Text and badge masks drawn into A8 targets land in the right lane, and Dock badges show their numbers again (PRs #271
  and #274, thanks watchthelight)

Apps and games
- OpenGL: Minecraft's vertex shaders translate, the draw path no longer allocates per draw, texel-buffer descriptors and
  unchanged bindings are reused, depth bias and per-instance step rates take effect, samplers work inside argument buffers
  (thanks HighDelay)
- Chrome and Chromium use Metal through ANGLE by default, so their GPU process starts; Electron apps stay opt-in with
  NVMTL_ANGLE_METAL=1, and NVMTL_ANGLE_METAL=0 turns it off everywhere (PR #263, thanks Hi-Jiajun)
- Shaders with `[[visible]]` function references that function constants turn off are no longer refused, which lost
  pipelines in the login window, Spotlight, icon services and the Dock
- Metal reports the card's whole video memory as its working set, as a Mac does (8 GB cards said 5.9 GB, 12 GB cards 8.9 GB)

## 2.3.5

Stability
- The login screen no longer stays black for a minute after the display comes up. The driver waited 60 seconds before
  letting the compositor's frames reach the panel, on the assumption that the screen was already showing them another
  way; on macOS 26 it was not, so every boot sat on a black login screen for exactly that minute

## 2.3.4

Pre-release for the early-updates testers; it becomes the general release only after every GPU generation passes the
field gate.

Stability
- Window dragging, Launchpad and games stutter less: each screen update waits at most one refresh period for the
  previous one, instead of a fixed tenth of a second; the wait wakes on the display's own event
- macOS Sequoia screenshots and window captures: the capture ordering fix from 2.3.2 returns, and it now follows what the
  installed kernel driver supports, so a Metal driver and a kernel driver from different versions never mismatch again
- Turing (GTX 16 and RTX 20): the login screen no longer stays black with a cursor when the first frame has not been
  drawn yet; the driver only shows video memory the Metal driver has written
- Turing: the ten-second start wait the installer used to add is now the driver's own default, so a laptop card or a Mac
  whose boot arguments were rewritten gets it too (a value you set yourself still wins)
- Laptops with a hybrid display: the driver no longer copies the boot screen from another card's memory, which could
  stop the Mac during startup. When WhateverGreen is present next to the driver, the installer notes the gfxrst setting it needs
- A Mac reset during the driver's own start-up wait (the ten-second Turing settle, the 100-second wait when the card's
  memory window cannot be moved) no longer counts toward switching the driver off. Two such resets used to leave a
  working Mac on the fallback driver, reported at the next login as "driver loaded: no"
- A GPU that is busy is not a GPU that is gone: the Metal driver keeps waiting for a late frame instead of disabling
  itself for the rest of the process. In WindowServer that disablement meant every later command buffer was refused
  and the session restarted ("Failed to get a MTLCommandBuffer")
- Every kernel driver now carries the package version. A Mac that still runs an older kernel driver after an update
  (the kernel collection was not rebuilt) is named in Send logs and in the "reached the desktop" report instead of
  counting as the new version

Logging (so a report says what happened)
- The kernel driver logs its own version at start
- A full video-memory window logs the first refusals and then every 256th, with one line saying who holds the window
- Spare framebuffer instances (more instances than displays) say so instead of reporting a failure
- A display that stops flipping logs how long the frame had been pending and the flip counters
- Every ten seconds of screen activity, one line per display records the flip rate against the panel rate, missed
  refreshes and skipped frames
- A refused mode set or flip in the kernel names the stage that refused it
- The Metal driver says why it could not begin a command buffer (a lost GPU, a failed allocation), the line
  WindowServer crashes without

## 2.3.3

Stability
- 2.3.2 made some Macs worse: blank or torn windows and a Dock missing its apps on macOS Sequoia, WindowServer
  crashes on some Tahoe desktops. Its display changes (screenshots and the lock screen, the brightness and video-memory
  logging, the Turing flip change) are withdrawn from the package until they pass a staged test with the testers.
  2.3.3 ships the 2.3.1 driver, which has the longest clean record in the field, on both macOS 15 and macOS 26
- macOS Sequoia: installing and updating works again (the 2.3.2 fix is kept: the Metal driver is built for macOS 15)
- After a rollback, the installed version record follows the files that are really installed (kept from 2.3.2)

## 2.3.2

Fixes
- macOS Sequoia: installing or updating to 2.3.0 or 2.3.1 stopped with "runtime payload requires macOS 26.0.0". The
  Metal driver in those packages was built for macOS 26 only; it is built for macOS 15 again, and a release can no
  longer ship anything that needs a newer macOS than 15
- Screenshots, window captures and the lock screen showed a blurred or empty window. macOS reads a window right
  after drawing it, and the driver now finishes the drawing first, as Apple's drivers do
- Send logs now includes how much of the card's video memory window is in use, so a black screen with only a
  cursor can be told apart from a leak
- After an update was rolled back, Send logs still named the new version. The installed version now follows the files
  that are actually installed, so reports say what your Mac is really running
- The Metal driver never hands the GPU a missing texture or sampler; it uses an empty one and logs it (a cause of
  Safari crashes in older versions)
- Laptops with the screen on the NVIDIA GPU: every step of the brightness setup is logged, so brightness that stays
  greyed out can be traced from Send logs

AMD / Intel test program
- A test driver that fails to start no longer panics the Mac: memory still mapped when the start gave up is kept
  until it is unmapped, instead of being freed under the mapping
- Intel 11th and 12th gen graphics (Tiger Lake, Rocket Lake, Alder Lake-S) and DG1 now reach the Intel driver. Two
  drivers list these chips and the device was offered to the wrong one, so neither took it
- When a start fails, its first error lines are kept and reported on the next start, even if the Mac stopped
- The test driver's log names AMD and Intel devices correctly

## 2.3.1

Safer updates
- If an update cannot bring your display up, your Mac no longer stays on a black screen. After two starts in a row
  that never reached the display, the driver turns itself off so macOS still starts, and the driver you had before the
  update is put back automatically with one restart. Nothing stays running once an update has worked
- Releases now also have to pass app checks on the new Metal driver (Terminal text, window capture) and may not do
  worse than the release before

## 2.3.0

Fixes
- Plugging a different monitor into the same port: macOS sees the new monitor (its name, size and modes) instead
  of keeping the one it replaced
- Terminal and other apps that draw text with programmable blending: text no longer comes out missing or garbled
  (GTX 16 and others). Draws that read colour from a pass without every attachment were skipped; they are now drawn,
  and overlapping triangles in one draw read each other's colour in order (thanks pengubow)
- Above 60 Hz: the display and the accelerator woke up once per millisecond to check for a finished flip, which
  users' flicker recordings showed as about 56 flips a second on a 143 Hz panel while windows moved. They now wake on
  the flip itself (the display on macOS 15 and 26, the accelerator on macOS 26)
- When a display's mode set is refused, the driver now tries the mode the firmware is already running before giving
  up, and the log says why the mode was refused (reports of a black screen at login on some RTX 3060 boards)
- Draws the Metal driver had to drop are now in the logs that Send logs collects

AMD / Intel test program
- 1401 can join the AMD and Intel GPU test program (AMD/Intel panel, Join). Testers get the newest signed test build
  of the AMD/Intel driver, and after every restart 1401 sends what the driver did, so a start that hangs or panics
  still reaches us. Leave the program from the same panel at any time

## 2.2.0

This release makes the driver do one thing: drive NVIDIA GPUs. Everything else it used to carry is now its own
product or is gone, which makes the package smaller, leaves less running on your Mac, and leaves less to go wrong.

Leaner
- The package carries the NVIDIA driver only: the AMD/Intel GPU driver, the NPU driver and the audio driver are
  separate products with their own packages and installers (the package drops from about 1 GB)
- The boot logo is gone: the OpenCore logo driver, the boot progress screen and the display driver's code that kept
  the logo on screen. Installing, updating or removing the driver takes an existing logo out of OpenCore
- Three background jobs are gone: a display tracker that streamed the driver's log all the time, a WindowServer
  restart at every boot (a flicker workaround from before the flicker fix in 2.1.0), and a crash-check login item.
  Logs are collected only when you press Send logs. Installing or removing the driver takes out what an earlier
  version left
- The 1401 Mac app moved to the 1401 project; it installs this driver with the package's own installer

Fixes
- A display whose firmware boot timing NVIDIA's display code does not accept (seen on an RTX 3060 at 1920x1080) now
  switches to the validated timing for the same resolution instead of crashing WindowServer at login
- Checksum lists are checked strictly: a malformed line in SHA256SUMS used to be skipped while the check still passed
- install.sh fixes PATH before running anything as root, like uninstall.sh
- Crash reports sent with Send logs keep the library identifiers needed to read them (machine identifiers are still
  removed)

## 2.1.0

Driver
- Each GPU generation (GTX 16, RTX 20, 30, 40 and 50) is now described by the card itself: the driver reads the
  architecture and the newest engine classes from the GPU and publishes them, and the Metal driver picks the video
  decoder and the scan-out model from that instead of a table of device IDs
- Cards with a small BAR1 (Resizable BAR off, or a board that does not offer it): the driver moves BAR1 away from the
  boot screen at its own size, so GTX 16 / Turing cards get the same zero-copy display path as the others. A BAR1 that
  macOS left unassigned is reported instead of failing with no reason
- The desktop no longer waits for the GPU on every frame (#196): each window the compositor draws is marked when the
  GPU finishes it, and the display waits only for that mark
- A BAR1 the firmware already placed large is left as it is, and the old setup comes back if a resize fails
- Gamma tables are applied; HiDPI desktop modes can be turned on with nvfbhidpi
- The installer keeps your own settings block in nvrm610.conf

Metal
- Blender: Eevee renders lit scenes. NVIDIA GPUs cannot write the stencil value from a shader, which Blender's Metal
  backend relies on to sort its lighting; the driver now rebuilds that value by drawing once per stencil bit. Also
  fixed: a light loop that hung the GPU (the first active SIMD lane, and loop values lost when helpers were inlined),
  render passes with no attachments, layered 3D colour targets, framebuffer fetch, samplers held in argument buffers,
  and image atomics in helper functions. Blender's shadow pages no longer crash it
- Indirect compute dispatches read their grid size on the GPU instead of stopping to read it on the CPU
- Hardware video decoders are registered with macOS's decode service (#175)

AMD / Intel test driver
- The log now says which driver started, which PCI drivers registered and which device each one matched, so a GPU
  no driver took can be traced from a user's log

Mac app
- Map my GPU (experimental, opt-in)

## 2.0.4

Mac app
- Prepare this Mac for Tahoe is back: on macOS 15 one button installs or updates the driver (it carries the Tahoe
  driver too), parks it and sets the installer's GPU settings, then opens Software Update. After the update the
  first start sets the driver up for Tahoe and restarts once by itself. Cancel puts everything back, also with a 1401
  stick plugged in next to the internal disk (it changes back the config the preparation changed)
- Intel and AMD laptops and PCs whose own GPU OpenCore switches off (disable-gpu, or -wegnoigpu on Intel) can try the
  AMD / Intel test driver: the app no longer says "no GPU" and stops; turning the test driver on turns that GPU back
  on (also on Core Ultra, where the NPU used to hide it), and turning it off switches it off again exactly as it was

1401 for Windows
- When the stick's disk number is gone at write time, the log now lists the USB disks Windows shows instead

## 2.0.3

Driver
- System sleep and wake (PR #182, thanks danlt150): the GPU's memory is kept across sleep, the PCI setup and
  Resizable BAR are restored on wake, and the display driver does the wake steps macOS expects. Before, the GPU
  lost its state in sleep and the desktop could freeze on wake
- The Metal driver finds a submission's copy buffers once instead of on every check (PR #223, thanks HighDelay)
- The display driver no longer fills the log with the same "hdcs" line many times a second (#216)
- Security (#214): diagnostic sysctls that let any user read physical addresses and memory are gone from normal
  builds (they exist only with the nvrmdiag=1 boot argument, and only root can read them); starting the GPU by
  hand is root-only; the kernel addresses used for debugging are left out of shipped builds

Mac app
- AMD boards (AM4 and AM5) whose OpenCore config rewrites the firmware memory map get DisableVariableWrite, so
  macOS cannot write NVRAM through that map (boards that stopped starting after an install)
- When no NVIDIA card is visible, the app says why: -wegnoegpu or -wegonogpu in boot-args, a disable-gpu property,
  or a laptop BIOS set to integrated graphics only
- Macs with only an AMD or Intel GPU can turn on the experimental AMD / Intel GPU driver; it installs from the
  downloaded driver package, without the NVIDIA driver
- Laptops whose built-in screen is wired to the NVIDIA GPU get an opt-in switch for brightness control

1401 for Windows
- Intel PCs (12th gen and newer) that stop at EB.MM.AKM because no kernel slide fit: rebuilding with the stick
  plugged in reads its startup log and turns on DevirtualiseMmio for the next start
- AMD PCs get the same NVRAM write guard as the Mac app (DisableVariableWrite when the memory map is rewritten)
- A stick whose disk number changes during the write is found again instead of stopping the build; a volume that
  disappears is skipped
- When Apple's download server resets the connection, the stop message says what to try on the network
- When Windows cannot format the stick, its own error is now in the log before the fallback

## 2.0.2

1401 for Windows
- A PC with no network adapter that works in the macOS installer gets its stick written, with a note to connect a
  supported Ethernet or Wi-Fi adapter before installing
- A stick that Windows gives a new disk number just before the erase is found again and written

No changes to the driver or the Mac app since 2.0.0.

## 2.0.1

1401 for Windows
- PCs with an NVIDIA card can build for macOS 26 Tahoe. 2.0.0 offered only macOS 15 for them, so picking
  Tahoe stopped the build

No changes to the driver or the Mac app since 2.0.0.

## 2.0.0

macOS 15 Sequoia and macOS 26 Tahoe.

Security
- Fixes for every finding in the security report by @Mahasvan (#214). Thank you for reporting them to us first.
- The driver's administrator check now reads the caller's real user id; it used to answer yes for everyone
- Copies between NVKMS and a process can no longer reach kernel memory, and the NVKMS entry point is root-only
- First-time GPU bring-up requires root
- A process cannot map memory that belongs to another process
- Unknown client types are refused, the bind call checks its range and always drops a stale mapping, waits on GPU
  events are capped, and one process can no longer fill the client table (slots are kept for the system)
- The kernel-address check refuses to arm on any macOS build it was not made for
- The Mac app verifies a signature on its own updates before unpacking them, runs with the hardened runtime, and
  refuses to run its root scripts from a folder other users can write to

Driver
- GeForce RTX 50 series: compute kernels compiled by NVIDIA's own compiler now run on the card
- Fix the black screen with only a cursor after updating to 1.15 (video memory reserved over the boot screen was handed
  out again), including displays larger than the boot screen's reserved memory, such as 3840x2560 on cards with a
  256 MB memory window
- Fix flicker caused by a reused buffer showing an old frame
- A second monitor keeps its high refresh rates, and panels faster than 240 Hz are paced correctly
- Ultrawide, 4K and HiDPI modes are no longer capped at twice the boot resolution
- Cards with a small memory window: the display's own share is counted, so the desktop stops running out of space;
  a Turing card left at 64 MB is resized to the largest size it offers
- A display connected after startup comes up without a restart (#142, thanks @lsCoding666)
- macOS learns the panel's physical size, so scaling is right instead of assuming 72 dpi (#199, thanks @GeekAudio)
- Laptops with the screen on the Intel GPU: the driver no longer takes the Intel GPU's display slot (#131, #119, thanks
  @TararaDotJoshua), and muxless Turing GPUs get the startup settle they need
- Tahoe: ship the macOS 26 accelerator built from this release (1.15 carried an older one: black screen on Tahoe)
- Fix crashes in Photos and media analysis, in apps that split a command buffer at an event, in Chess, and in apps
  that free Vulkan memory (thanks @Exop63)
- Fix memory running out during Super Resolution and other long machine-learning work
- A texture copied for display stays in step on every submission (thanks @KolliasG7); work is reported as scheduled
  only after IOSurface writes finish (#147, thanks @imyago9); a surface keeps its video memory across page-off (#200,
  thanks @imbrahiam)
- World of Warcraft Classic and other Metal games: cube-map arrays, bounds-checked buffer reads and several shader fixes
  (#171, thanks @razvanccr); Firefox WebRender shaders translate (thanks @advokat11); MSAA sample positions on cards
  without programmable 1x positions (#136, thanks @eisda)
- Faster pipeline builds: built in parallel, one hash per shader module, planner results reused, and one private image
  per IOSurface (#173, #177, #181, #188, #189, thanks @modesttao)
- Chrome, Edge and Electron apps draw on the CPU again by default, which ends the artifacts, stutter and voice-call
  crashes in Discord on several cards; `NVMTL_ANGLE_METAL=1` turns their GPU path on
- Optional asynchronous texture uploads, off by default (`NVMTL_ASYNC_TEX_UPLOAD=1`)
- Multi-GPU display selection workaround documented in docs/MULTI-GPU.md (#197, thanks @is-pan)

Experimental, off unless you turn it on
- AMD and Intel GPU support (NMXGPU) and the NPU driver, for macOS 15 and 26: Setup > "AMD / Intel GPU driver (test)"

Mac app
- Finish a macOS update the update helper never finished, and keep this Mac's own BAR settings afterwards
- "Remove NVIDIA driver" stops with an explanation, instead of failing half-way, when System Integrity Protection is on
- The Remove NVIDIA driver startup entry is always visible, and removing the driver keeps the screen on
- Warn when the Mac starts OpenCore only from the USB stick and the internal disk has none
- Explain a failed config backup on a read-only OpenCore partition, and say which step comes first when Install is
  greyed out
- Send logs copies files before running log queries and includes crash reports under any name

1401 for Windows
- Pick macOS 15 Sequoia or macOS 26 Tahoe before the build; see the 1401 changelog for the full list

Known issues
- Preview not showing pictures and lost window transparency on some macOS 15 setups
- Chrome decodes video on the CPU while its GPU sandbox is on (#175)
- Thunderbolt eGPUs: no display
- Laptops with the screen on the Intel GPU: the built-in screen is not accelerated
- HDMI/DisplayPort audio: the display now reports its audio formats to macOS, but sound over HDMI/DisplayPort is not
  working yet

## 1.15.0

The last release for macOS 15 Sequoia.

Driver
- Fix black screen with only a cursor on GTX 16 and other cards without Resizable BAR (regression in 1.14)
- Fix Unity 6 games hanging on the first frame
- Fix windows and web pages showing old content after a redraw
- Fix high WindowServer CPU and low frame rate at 4K after a few minutes
- Fix slow scrolling (window copies up to 35x faster)
- Fix monitors shown as a 30-inch TV in About This Mac
- Fix 165 Hz mode missing on some monitors (since 1.14.2)
- Retry a display that stays dark after sleep on the next wake
- Fix Chrome, Edge and Electron apps drawing everything on the CPU (no WebGL, slow scrolling); their GPU path is on
  by default again and `NVMTL_ANGLE_METAL=0` turns it off
- Allow Chrome, Edge and Electron apps to use the GPU with their sandbox on
- Fix 5120x1440 and other ultrawide modes missing when the firmware started at a smaller resolution
- Fix green lines in Photos and other VideoToolbox scaling (#179, #180, thanks @modesttao)
- Add T400, T600, T1000 and T-series laptop GPUs (#132, thanks @TararaDotJoshua)

Mac app
- Show download progress in Update driver, which looked stuck at "Downloading the newest driver..."
- Ask for access to USB sticks instead of failing with "no OpenCore config"
- Say when the boot stick is unplugged
- Recover from an interrupted install instead of stopping at "installer guard already exists"
- Download from the NullMoth mirror when GitHub is blocked
- Send logs: always finish, keep the start of each boot, stop re-sending old logs

1401 for Windows
- Fix the build stopping on ACPI tables that declare a method twice (BIOSTAR B650 and similar)

Other
- Grammar fixes in the release notes (#128, #129, thanks @lunar-me)
- Translator test fix (#172, thanks @modesttao)
- Release notes moved into this file; add CONTRIBUTING.md and a pull request template

Known issues
- If Chrome pages show corrupted areas on your card, start it with `NVMTL_ANGLE_METAL=0` and send logs
- Ghost or black areas after windows overlap with a BAR of 4 GB or more (#127)
- Chrome decodes video on the CPU while its GPU sandbox is on: VideoToolbox picks a decoder before the driver loads (#175)
- Thunderbolt eGPUs: no display
- Laptops with the screen on the Intel GPU: the built-in screen is not accelerated
- No HDMI/DisplayPort audio

## 1.14.7
- Stop driver updates from turning the EFI boot logo back on
- Keep the existing OpenCore BAR settings when installing
- Show install, cancel and failure in the updater after the download finishes

## 1.14.6
- Fix Firefox WebGL inside the GPU sandbox
- Turn off the Chromium ANGLE Metal path by default after corruption reports on RTX 30 and 50
- Retry Apple recovery downloads that return HTTP 403

## 1.14.5
- Fix Unity 6 games hanging on the first frame
- Keep Chromium WebGL on the GPU when the sandbox blocks a memory query
- Send logs: give each log query its own time limit

## 1.14.4
- Fix full-screen flicker on machines that copy frames to the screen
- Send logs includes the AMD/Intel test driver

## 1.14.3
- Stop macOS writing to firmware memory on Ryzen 7000/8000/9000 (AM5)

## 1.14.2
- Fix black login window on high refresh rate monitors
- Fix SecurityAgent and Installer crashes on some RTX 40 PCs
- Fix apps quitting on a GPU check (Safari, photo analysis)
- Fix the AMD/Intel test driver firmware download

## 1.14.1
- Fix PCs that stopped starting after 1.14.0
- Fix the boot logo and test driver switches doing nothing

## 1.14.0
- Fix black screen with a cursor about a minute after start-up (#16, #26)
- Fix 4K at 120 Hz and other DSC modes going black (#77)
- Turn the monitor off when the display sleeps (#90)
- Bring a display back after switching inputs (#110)
- Fix black screen after 100 seconds on older Intel boards without Resizable BAR

## 1.13.0
- Fix Turing cards stuck at start-up after updating
- Fix "Remove NVIDIA driver" not removing the driver, or hanging
- Crash reports no longer show crashes from before the update

## 1.12.0
- Use the NVIDIA GPU on laptops whose screen is on the Intel GPU (as a second Metal device)
- Record display state from the first second of each boot for flicker reports
- Fix Live Text crashing in the Vulkan driver

## 1.11.0
- Download Lilu and AMFIPass when they are missing from the EFI
- Fix Firefox crashing at launch (`ipc_control_port_options=0`)
- Fix "STOP back up nvmtl"
- Add USBToolBox when it is missing

## 1.10.0
- Fix AM5 Ryzen PCs stuck at `EB.MM.AKM`
- Drop the DMAR table on Intel Core Ultra
- Reach Apple's recovery server directly when DNS fails

## 1.9.0
- Try one more memory setting on AM5 boards
- Erase stubborn USB sticks with diskpart

## 1.8.0
- Use known-good settings for boards that have booted before
- Fix "Format-Volume: Invalid property" and "WinError 2" when erasing

## 1.7.0
- Pass the board's DSDT to macOS again (was dropped on about 1 in 5 PCs)
- Send logs delivers the flicker capture and OpenCore config

## 1.6.0
- Fix the driver removing itself after login on some boards
- Fix `RmInitAdapter failed` on Ryzen with a custom EFI
- Send logs includes the OpenCore config (serials removed)

## 1.5.0
- Finish the macOS download on networks that cut connections

## 1.4.0
- Clear flicker at login with a compositor restart
- Build on PCs whose ACPI tables crash the disassembler

## 1.3.0
- Fix Send logs freezing the app
- Fix the crash loop on 16 GB RTX 50 cards
- Light up a monitor plugged in after login

## 1.2.0
- Fix "Remove NVIDIA driver" so the driver stays off until removed
- Show the picture with macOS's display driver while ours is off

## 1.1.0
- Fix recycled allocations reaching queued GPU work
- Fail a rejected display request on its own instead of stopping the display

## 1.0.x
First public releases: installer, updater, removal and USB map fixes.
