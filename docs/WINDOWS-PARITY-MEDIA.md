# Media and power: what NVIDIA's Windows driver offers that this driver does not

Each row names the Windows feature, the state in this tree as of release 2 (3.0.20), and what closes it or why it
cannot be closed. This fork has run on one RTX 5050 (Blackwell) with macOS 26.7.3; a gap that needs another generation
needs a card of it. See [STATUS.md](STATUS.md) for the scope of the fork's hardware results, and
[NVMETRICS.md](NVMETRICS.md) for companion runtime findings.

| feature (Windows) | state here | to close, or the hard reason |
|---|---|---|
| H.264 and HEVC decode, all chips from GTX 16 | built-in decoders; vendored NVEncVT adds Ada and Ampere, and on Blackwell H.264 in the decode service (Safari, QuickTime, FaceTime) once the card has passed NVEncVT's decode self-test | Ampere H.264/HEVC through NVEncVT needs a GA10x card to validate; until then the built-in decoder stays. Blackwell: no result of the self-test on this fork's RTX 5050 has been recorded yet; HEVC stays built-in there (NVEncVT's HEVC decoder reads the picture in Turing's plane layout) |
| AV1 decode (RTX 30 and newer), VP9 decode | NVEncVT registers both on Ada and Ampere | Blackwell: NVEncVT's VP9 and AV1 decoders read the picture in Turing's plane layout and are not registered there. A pass of the H.264 self-test on a card shows that the Blackwell channel and decode classes take Ada's method list and that the Blackwell plane layouts are right; then the two decoders can be ported and put behind the same kind of test |
| AV1 encode (Ada, Blackwell) | Ada through NVEncVT; Blackwell none | Blackwell encode setup differs from Ada's; needs a Blackwell card to validate |
| HEVC and H.264 encode, Turing to Blackwell | Turing and Ampere through the built-in libraries, Ada through NVEncVT, Blackwell built-in | Ampere NVEncVT encode not enabled: the vendored setup targets Ada and TU117 |
| 4:2:2 decode and encode (Blackwell) | none | needs a Blackwell card |
| Several simultaneous NVENC sessions on one engine | built-in Turing library refuses a second session | the library is closed; NVEncVT covers TU117 only |
| Low-latency rate control | vendored H.264/HEVC encoders only | the built-in encoders are closed libraries |
| HDR video decode and tone mapping, RTX Video HDR, RTX Video Super Resolution | none | these are Windows driver features built on closed code; not reproducible from public sources |
| GPU idle power states | NVK per-burst boost (2.9) and NVRM's machine-wide boost policy (3.0.1, selector 13); RM's own dynamic power runs in NVRM | measured idle clocks need a run on the card |
| Laptop dGPU power-off (Optimus RTD3 / GC6) | opt-in D3cold monitor (`nvrtd3=1`) | needs ACPI power-resource evaluation and a hybrid laptop hardware test |
| Power limit and clock lock (NVML, nvidia-smi) | none | the public 620 headers carry no control for either; RATED_TDP SET_CONTROL was reported to change nothing, and the PFM controls answer NOT_SUPPORTED to a user client |
| Fan curve, clock and voltage offsets (NVAPI) | no general driver write API; separate NVMetrics guarded manual fan experiment | verified only on the tested RTX 5050 RM record layout; not a universal fan curve, clock or voltage interface |
| Telemetry (power, clocks, temperature, utilisation, throttle reasons, fan) | `tools/nvmtl-smi` and separate NVMetrics dashboard; GPU Caps read-only ABI | RUSD data requires coherent fresh reads; GPU Caps evidence is partial |
| Video engine utilisation counters | `tools/nvmtl-smi` prints what RUSD reports for the video engines | not compared with a known load on a card |

## Firefox

Firefox decodes video in one of its child processes (`plugin-container`) and draws it in another, its GPU process.
On RTX 40 the vendored NVEncVT registers by default in every app that loads the Metal plugin and offers its H.264,
HEVC, VP9 and AV1 decoders; on RTX 30 its VP9 and AV1 decoders. `about:support`, under "Codec Support Information",
shows whether Firefox takes them. The author of pull request 487 of the original project (dreamelite96) reported
hardware decoding of VP9, AV1 and HEVC there on an RTX 4060 Ti (macOS 26.7.2, Firefox 159); this fork has no RTX 30 or
40 to try it on.

From that pull request:

- Firefox's sandboxes refuse `/dev/null`. NVEncVT's connections to the kernel (`vendor/nvenc/src/rm.c`) and the
  plugin's import of a surface's video memory then use the read end of an empty pipe as their descriptor, as NVK's own
  connections already did; without any descriptor the connection fails at once and the plugin logs the import it
  could not make. The import is the one change here that acts on RTX 50 by default: in a Firefox process whose sandbox
  refuses `/dev/null` and that draws with Metal, IOSurface textures now live in the surface's own video memory, as in
  every other process, instead of in private images and their copies. `NVMTL_SURFACE_VRAM_PIPE=0` in the settings
  file puts the import back on a plain `open()`, which fails in such a sandbox, as in 3.0.4 (whether that sandbox
  lets the plugin read the settings file has not been checked).
- In `plugin-container` each decoded picture stays referenced until eight later ones have been handed out, so the
  pool does not write the next picture into one Firefox's GPU process has not taken yet. Other processes release
  each picture at once. `NVENCVT_DEC_HOLD=0` (in the environment, or as a line of the root-owned
  `/usr/local/etc/nvencvt.conf`) turns this off.
- `NVENCVT_LOG=2` logs every VP9 and AV1 frame with NVDEC's status.

On RTX 50 none of this brings VP9 or AV1: NVEncVT does not register those decoders on Blackwell (first table), so
Firefox decodes VP9 and AV1 in software as before. Firefox's H.264 there goes to the driver's own H.264 decoder
(`docs/PROVENANCE.md`, section 2a); NVEncVT decodes H.264 by default only in the decode service, after its
self-test, so the hold above applies on RTX 50 only where `NVMTL_NVENCVT` is `1` or names `plugin-container` and
the self-test has passed in that process. The pull request's launcher, which preloads the library into Firefox with
`DYLD_INSERT_LIBRARIES`, is not included.

Sharing a window from Firefox (and OBS or Zoom, which also read captured frames with the processor) no longer holds
WindowServer behind the copy engine since 3.0.20 (on the RTX 5050 with Firefox 161: 130 waits and 116 ms in ten
seconds, against 301 and 420 ms with 3.0.19; `nvaccel_pageoff_copy_locked=1` goes back). Google Meet still sends 5
frames a second from Firefox and Safari, which it asks of every browser that does not call itself Chrome
([TROUBLESHOOTING.md](TROUBLESHOOTING.md)).
