# What NVIDIA's Windows driver offers, and what this driver offers on macOS

The original project's 2.9 audit, brought up to this fork's release 2 (3.0.20), for GTX 16 / RTX 20 to RTX 50 cards on
macOS 15 and 26. Each row names the Windows feature, the state in this tree (file, switch or document), and what is
missing. "Blocked" always carries the reason. Media rows are detailed in [WINDOWS-PARITY-MEDIA.md](WINDOWS-PARITY-MEDIA.md);
card coverage in [CARD-SUPPORT.md](CARD-SUPPORT.md). A row that is built is not a claim that it was run: this fork has
run on one RTX 5050 with macOS 26.7.3 only ([STATUS.md](STATUS.md)), and a gap that "needs" a part (an HDR display, a
notebook, a second card) means a run on a machine that has it, which nobody has reported to this fork yet.

The user-facing settings among the boot arguments and `nvrm610.conf` lines (19 in its catalogue) are listed, validated
and changed safely by `tools/nullmoth_settings.py` (`list`, `get`, `set`, `--selftest`). It never touches a boot argument
or conf line it was not asked about, and writes conf settings only into the block the installer keeps across updates.
[SWITCHES.md](SWITCHES.md) lists every name the driver reads.

## Hardware coverage

| Item | State | Notes |
|---|---|---|
| Device table | 251 ids, `SUPPORTED_IDS` in `package/install.sh` | Proven against NVIDIA's 620.70.06 list by `tools/check_device_table.py <ogkm README.md>`: every id is in NVIDIA's list, every id left out (48) is a headless data-centre or mining part, every id falls in exactly one generation row. 2.9 added A40 (2235) and L40 (26B5), the two data-centre cards with DisplayPort outputs. |
| Turing to Blackwell, desktop, laptop, Max-Q, workstation, T-series, MX with Turing | all present | GTX 16, RTX 20, RTX 30 (GA10x), RTX 40, RTX 50, RTX A, RTX Ada, RTX PRO Blackwell, Quadro RTX, T400 to T1200, MX450/MX550 (Turing), MX570 (GA107) |
| Unknown future id | still installs | the installer accepts any device id from 1E00 up (Turing and newer), whether or not its table names it |
| Per-generation paths | one row per generation in `plugin/nvmtl_dlss.c` (DLSS architecture), SM targets in `plugin/NVMTLVendorCompiler.m`, video library choice in `docs/WINDOWS-PARITY-MEDIA.md` | the device-table check proves every id reaches exactly one generation row |
| Pre-Turing | out of scope | no GSP firmware; see CARD-SUPPORT.md |
| eGPU over Thunderbolt | supported with limits | the BAR bridge-window code handles a card behind a downstream port (`kexts/NVRM/NVRM.cpp`, an RTX 3070 eGPU case); hot-unplug runs the device-removed path (`docs/NVRMFB-HOTPLUG-LIFECYCLE.md`, device-lost recovery in the plugin); `tools/nvmtl_gpus.py` marks a card as external. The tunnel carries PCIe x4 at most, so a link below x16 is normal there, not a fault. Not proven on hardware. |
| PCIe generation and link width | reported by `tools/nvmtl_gpus.py` | reads `IOPCIExpressLinkStatus` against `IOPCIExpressLinkCapabilities`; a narrower link is flagged LOW, a lower speed at full width is reported as idle step-down (cards drop their link speed at idle). `--selftest` has 10 named arms. |
| Multi-GPU boxes | off by default (`nvmultigpu=1`) | every NVIDIA card can be accelerated, one start-up at a time, refused cards are offered again; a process drives one card (the primary, or `NVMTL_CARD` per application). Several cards in one process are not built. [MULTI-GPU-DESIGN.md](MULTI-GPU-DESIGN.md) has the status and proof; a second card was not available to test with. |

## Display

| Windows feature | State | Gap |
|---|---|---|
| HDR10 | boot-arg `nvfbhdr=1` (a setting, `kexts/NVRMFB/fb/nvrm-fb.cpp`, `tools/test_fb_hdr.py`) | needs an HDR display |
| 10 / 12-bit output | `nvfb10bit=1`, `nvfbbpc` 6/8/10/12 | none |
| Colour format and range | `nvfbformat` (RGB, 4:2:2, 4:4:4), `nvfbrange` | none |
| Digital vibrance | `nvfbvib` 0 to 200, live through `debug.nvrmfb_color_*` | none |
| Variable refresh (G-Sync Compatible, Adaptive-Sync) | `nvfbvrr=1` | G-Sync with a hardware module needs NVIDIA's closed module protocol: blocked |
| DSR / DLDSR | `nvfbdsr=1`, `nvfbhidpi=1` give desktops above the panel's size scaled down by the head scaler | DLDSR's neural filter is a closed NVIDIA model: blocked; the plain scaler path is built |
| Scaling modes | head scaler, `nvfbrotscale=1` for rotated heads | none |
| Rotation | supported, scaled modes opt-in as above | none |
| Surround / multi-monitor | `nvfbsurround=<mask>` with hot-plug and audio (`tools/test_fb_surround_wiring.py`) | none |
| Laptop panel brightness | NVKMS path, eDP AUX path (`nvfbedpbl`) | none |
| HDMI/DP audio | ELD mapping (`docs/DISPLAY-AUDIO-MAPPING.md`) | none |
| Control panel | `tools/nullmoth_settings.py` (since 2.9): one catalogue of 19 settings with validation | a command line only: this fork ships no graphical panel (the original's Mac app had one over the same catalogue) |

## Video and compute

| Windows feature | State | Gap |
|---|---|---|
| NVENC / NVDEC, AV1, VP9 | per generation in WINDOWS-PARITY-MEDIA.md | Blackwell decode class and 4:2:2 need a Blackwell card |
| RTX Video Super Resolution / HDR | none | closed NVIDIA models: blocked |
| CUDA | **blocked.** libcuda and the CUDA driver API are NVIDIA's closed code (licence 2.2/2.3 forbids modifying or disassembling it) and there is no open CUDA runtime for NVK. | The open route is not CUDA: Vulkan compute on NVK (offered to applications by the runtime and the loader below) and OpenCL on NVK (Rusticl, not built here). |
| OpenCL | not in this tree (the original project kept it in a separate branch) | OpenCL on NVK (Rusticl) is not built here |
| DirectML-equivalent | Metal Performance Shaders and Core ML run on the Metal path | none |
| Vulkan for applications | **built:** `package/vulkan-for-apps.sh --install` writes a loader manifest for the driver's NVK library, off by default (`tools/test_vulkan_for_apps.sh`, 12 arms). `nvk/nvk-metal-wsi.patch` adds `VK_EXT_metal_surface` windows (copy-based present into a `CAMetalLayer`). NVK reports Vulkan 1.4.363 (its Mesa 26.3 base; the owner waits for Mesa 27 before rebasing, [NVK-BASE-AND-HEADERS.md](NVK-BASE-AND-HEADERS.md)) | Window presentation through this path is proven to build only; the copy path is slower than the frontend below. The Khronos loader v1.4.367 (Apache-2.0) is in the package as `Library/NullMoth/vulkan/libvulkan.1.dylib` and is installed with `--with-loader`. |
| Vulkan runtime for games (`tools/nvk-bridge`) | **built and installed with the driver:** `/Library/GPUBundles/nvmtl/minecraft`, NVK with each frame copied on the GPU into the window; FIFO, IMMEDIATE and MAILBOX, the last two showing the newest frame without holding the application to the display (3.0.9, 3.0.11; `NVK_BRIDGE_PRESENT_NEWEST=0` goes back). An application selects it when it is launched ([INSTALL.md](../tools/nvk-bridge/INSTALL.md)) | Seen with Minecraft 26.3 on one RTX 5050 (up to 1000 fps on 3.0.9, read by its owner); no capture after 3.0.9, and no run of an application that asks for MAILBOX is recorded. |
| OpenGL | Zink on NVK via GLD on macOS 15.5+, on by default, with Apple's OpenGL-on-Metal fallback (`nvzink=0`). A swap runs one frame ahead (3.0.7), with the swap interval at 0 shows the newest frame without waiting for the display (3.0.11), and a `glBufferSubData` that would wait goes through a staging buffer (3.0.16) | Runs on one RTX 5050 with macOS 26.7.3 since 3.0.6 (490 fps in Minecraft without mods on 3.0.11, read by its owner); on another card or macOS build validate GL_RENDERER after installation. See [ZINK-OPENGL.md](ZINK-OPENGL.md). |

## Gaming

| Windows feature | State | Gap |
|---|---|---|
| DLSS Super Resolution, Frame Generation, Ray Reconstruction | `plugin/nvmtl_dlss.c`, `plugin/NVMTLMetalFXDLSS.m`, `dlss/`, on every RTX generation once the files are in place. Since 3.0.13 the installer fetches the DLSS files by default (`NULLMOTH_DLSS=0` skips; 3.0.19 fixed the check that had skipped the step) | the architecture mapping is measured on an RTX 5060 only (the original's 2.9); the Turing, Ampere and Ada rows are unmeasured; no game result on this fork's card is recorded |
| Max frame rate limiter | **built:** `NVMTL_FPS_LIMIT` (`plugin/nvmtl_fps_limit.h`, `tools/test_fps_limit.c`, 17 arms). One number or `Game=60,*=0`, 10 to 1000, never WindowServer. Kept across updates. | applied on the application's present call; proven offline only, behaviour on a real game needs a run |
| Low-latency mode | `NVMTL_GSUB_INFLIGHT` (1 to 32, queue depth) | NVIDIA Reflex needs the game's own SDK calls and NVIDIA's closed runtime: blocked |
| Per-application profiles | built-in profiles by executable and bundle id (`plugin/nvmtl_app_profile.h`); a user's own settings block in `nvrm610.conf`; per-application GPU on hybrid laptops (`nvmtl-gpu-policy.txt`); per-application frame limits | a user-defined per-application profile for arbitrary knobs is not built; the frame limit is the one per-application user setting |
| Shader cache size control | `NVMTL_SPVCACHE_MAX_MB` (default 2048; 0 turns trimming off) | none |
| Hidden-surface culling (the GPU's ZCULL unit) | on outside WindowServer since 3.0.17 (`nvk/nvk-zcull.patch`; `NVK_ZCULL=0` goes back) | what it gives in a game is not measured |
| Ray tracing | the translator's own BVH path; NVK's ray queries on the RT cores are compiled in and off unless `NVK_HWRT_QUERY=1` ([HWRT.md](HWRT.md)) | not measured on this fork's card |

## Power, thermal, system

| Windows feature | State | Gap |
|---|---|---|
| Idle power states | NVK burst boost, RM standby; ASPM L1 on request (nvrmaspm=1), the link otherwise stays as the firmware set it | measured idle clocks need hardware |
| Laptop dGPU power-off | opt-in D3cold monitor (`nvrtd3=1`, `nvrtd3res=`) | needs a hybrid laptop to prove |
| Power limit, clock/voltage offsets, fan curve | no general driver write API; GPU Caps reads selected capabilities | NVMetrics separately tested supervised RM manual fan control on one RTX 5050; not a generic fan curve or overclocking interface. See [NVMETRICS.md](NVMETRICS.md). |
| Notebook power budget (Dynamic Boost's TGP step) | opt-in `nvrmtgp=1`: the GPU firmware is told to run at the platform limit while on mains power and released on battery, live switch `debug.nvrm_tgp_enable`, state in `debug.nvrm_tgp_*` and `nvmtl-smi` (`kexts/NVRM/nvrm_power.cpp`, policy `nvrm_power_policy.h`) | needs a notebook to measure the enforced limit; the CPU/GPU power split (NVPCF) is `nvrmnvpcf=1` and the platform request handler (GPS) is `nvrmgps=1`, both served from the firmware's own ACPI methods (`nvrm_dsm_route.h`) |
| Run-time GPU boost switch | opt-in `sudo env NULLMOTH_GPU_BOOST=1 ./install.sh` installs `nvmtl-boost` as a LaunchDaemon; `package/boost_daemon.sh set on\|off` edits `/Library/NullMoth/gpuboost` and the daemon follows within two seconds (`tools/nvmtl-boost`, `tools/test_boost_daemon.py`) | needs a GPU run to measure the clocks |
| Hybrid laptop whose panel is on the integrated GPU | opt-in `nvfbigpu=1`: the framebuffer starts as a proxy head for the firmware console and the accelerator copies frames into it (`kexts/NVRMFB/nvrm_igpu_proxy.h`) | needs a BIOS Hybrid notebook to prove the copy path; see `docs/HYBRID-LAPTOPS.md` |
| Telemetry dashboard | `tools/nvmtl-smi` reads RUSD; [NVMetrics](NVMETRICS.md) supplies the separate graphical app and uses read-only GPU Caps | 15 verified / 150 unknown records on the tested GPU; no universal write authorization |
| Live boost policy | NVRM selector 13, root-controlled; NVK refreshes policy at lease renewal | separate from fan mode and clock offsets; idle applications may retain a hold until submitting again or exiting |
| Crash recovery (TDR equivalent) | a GPU fault on one channel rebuilds that channel (3.0, [CHANNEL-RECOVERY.md](CHANNEL-RECOVERY.md); `NVK_CHANNEL_RECOVER=0` goes back) and the plugin recovers from a lost device; the boot guard turns the driver off after two starts in a row that never lit the display, and the installer's fallback job puts the previous version back ([HOW-IT-WORKS.md](HOW-IT-WORKS.md), section 5) | none |
| Driver control panel | the command line above | no graphical panel in this fork |

## How to prove these locally

```
python3 tools/check_device_table.py <open-gpu-kernel-modules 620.70.06 README.md>
python3 tools/nvmtl_gpus.py --selftest
python3 tools/nullmoth_settings.py --selftest
cc -I plugin tools/test_fps_limit.c -o /tmp/t && /tmp/t
bash tools/test_vulkan_for_apps.sh
python3 tools/test_install_keeps_my_settings.py
```
