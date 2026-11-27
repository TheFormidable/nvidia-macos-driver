# What a real Mac has, and what a Mac with this driver has

This compares a real Apple-silicon or Intel Mac with an OpenCore PC or Intel Mac that runs macOS 15 Sequoia or macOS 26
Tahoe with this driver (NVIDIA GTX 16 and RTX 20 to 50), as of this fork's release 2 (3.0.20). Each row names a feature,
what this driver gives, and the gap. NVIDIA's own Windows features are in [WINDOWS-PARITY.md](WINDOWS-PARITY.md) and
[WINDOWS-PARITY-MEDIA.md](WINDOWS-PARITY-MEDIA.md).

It started as the original project's 2.9 audit, which also covered its EFI builder and Mac app, its ARM apps
translator and its NMX drivers for AMD and Intel GPUs, Wi-Fi and NPUs. None of those is part of this fork, so their rows
are left out; what does not depend on the GPU driver is summed up in one table below. This fork has run on one RTX 5050
with macOS 26.7.3 ([STATUS.md](STATUS.md)): "Have" describes what is implemented, not a certification of Apple
hardware parity or of every OS build.

How to read the status column:

| Status | Meaning |
|---|---|
| Have | in the released stack and expected to work; the file that provides it is named |
| Built, off | written and tested offline, behind a switch that is off until a Mac that has the hardware confirms it |
| Partial | some of it works; the missing part is named |
| Not built | buildable from public sources and not yet written; the next step is named |
| Blocked | cannot be made real on a PC; the reason is one of: needs Apple silicon, needs the T2 or a Secure Enclave, Apple's own gate, a licence, or a missing physical interface |

"Real Mac" statements come from Apple's published requirements and from what macOS itself reports on a real Mac; where a
requirement is not something we could confirm, the row says so. Nothing on this page is a promise about a specific
machine.

## Measuring it on your own Mac

`tools/mac_parity_probe.sh` prints one line per feature, taken from macOS's own reports (`system_profiler`, `fdesetup`,
`pmset`, `csrutil`, `ifconfig`). Run it on a real Mac and on the Mac with the driver and compare: a row that is `yes` on
the real Mac and `no` on the other is a gap, with the evidence beside it (`gpu.metal`, `gpu.metal4`, `wifi.awdl0`,
`security.filevault_usable` and the others). `tools/test_mac_parity_probe.py` checks the probe against fixtures laid out
the way a real Mac prints them.

The two limits that shape most rows:

1. A PC has no T2 chip and no Secure Enclave. Anything Apple gates on them (Touch ID, Apple Pay with the Secure
   Element, Activation Lock, offline Find My, iPhone Mirroring, hardware FairPlay 4K) cannot be made real.
2. Continuity (AirDrop, Handoff, Universal Control, Sidecar over Wi-Fi, AirPlay to Mac, Continuity Camera, iPhone
   Mirroring, Instant Hotspot, Auto Unlock) runs over Apple Wireless Direct Link, which macOS exposes as the `awdl0`
   interface. Only a Wi-Fi driver written for Apple's IO80211 family raises it. The features stand or fall with the Wi-Fi
   card and its driver, not with the GPU.

## Graphics and Metal

| Feature | Real Mac | With this driver | Status and gap |
|---|---|---|---|
| Metal 3 GPU family, argument buffers tier 2, mesh shaders, function pointers, barycentrics | all supported Macs | NVIDIA plugin (`plugin/`); features in `CHANGELOG.md` 2.7 to 2.8 | Have on NVIDIA. AMD and Intel GPUs are not part of this fork |
| Metal 4 | the Macs Apple lists | the device reports the Metal 4 family and Metal 4 queues, buffers, encoders and compiler run over Metal 3 objects (`CHANGELOG.md` 2.8); `system_profiler` reports Metal 4 on the RTX 5050 with macOS 26.7.3 | Have on NVIDIA |
| MetalFX temporal upscaling and frame interpolation | the Macs Apple lists | served by DLSS Super Resolution and Frame Generation on RTX cards once the installer has fetched NVIDIA's DLSS files, which it does by default since 3.0.13 (`plugin/NVMTLMetalFXDLSS.m`, `dlss/`; `NULLMOTH_DLSS=0` skips) | Partial. GTX 16 has no tensor cores, so there is no DLSS path there; the architecture mapping is measured on an RTX 5060 only (the original's 2.9) |
| MetalFX spatial upscaler on cards without DLSS | same | no source for it in `plugin/` | Not built. A compute-shader upscaler from a permissively licensed algorithm is buildable and needs a card to prove |
| Ray tracing (Metal intersection and acceleration structures) | the Macs Apple lists | the translator's own BVH path; NVK's ray queries on the RT cores are compiled in, off unless `NVK_HWRT_QUERY=1` ([HWRT.md](HWRT.md)) | Partial |
| Game Mode | Apple lists Apple silicon Macs | Apple's daemon on an Intel Mac says no (a fixed answer for Game Mode, and the kernel setting it relies on, `kern.console_mode`, is not on this PC's kernel). The GPU side of a replacement is in the plugin (`plugin/nvmtl_game_priority.h`): with the developer switch `NVMTL_GAME_GPU_PRIORITY=1`, or when the folder `/Library/NullMoth/gamemode` exists, a game (games category, or opted in to Game Mode) raises its own GPU channel group (interleave HIGH, a longer timeslice) and every other ordinary Metal client lowers its own; NVK's `nvmtl_nvk_tsg_reprioritise_v1` re-applies the level on a running process's channel groups (`tools/test_nvk_tsg_reprioritise.py`). The CPU-side daemon and the menu-bar indicator were part of the original's Mac app and are not in this fork | Built, off. Unset `NVRM_TSG_Q0_HIGH` behaves exactly as before; `0` is the interleave-LOW value. Not run on a game or an NVIDIA card |
| Game Porting Toolkit (D3DMetal) | Apple silicon only | none | Blocked: Apple distributes it for Apple silicon and the licence is Apple's. Wine with DXVK or MoltenVK over the Vulkan driver is separate and unproven |
| Multiple displays, hot-plug, high refresh, HiDPI | yes | `nvfbheads`, hot-plug re-read, `nvfbhidpi`, refresh above 60 Hz (`docs/WINDOWS-PARITY.md`, Display) | Have; the test machine has one 1920x1080 display at 100 Hz |
| HDR10 / EDR output | XDR and HDR displays | `nvfbhdr=1`, a setting (`kexts/NVRMFB/fb/nvrm-fb.cpp`, `tools/test_fb_hdr.py`) | Built, off. Needs an HDR display to confirm |
| Display brightness (laptop panel) | yes | NVRMFB backlight path with persistence (`docs/NVRMFB-BACKLIGHT-POWER.md`); on a hybrid laptop the integrated GPU owns the panel | Have where the NVIDIA GPU drives the panel; hybrid: see the next row |
| GPU switching (dual-GPU laptops) | automatic graphics switching | opt-in render offload with the integrated GPU keeping the panel (`docs/HYBRID-LAPTOPS.md`, `nmhybrid`) | Built, off. Needs a hybrid laptop |
| External GPU over Thunderbolt | Intel Macs | BAR bridge-window code for a card behind a downstream port (`docs/WINDOWS-PARITY.md`, Hardware coverage) | Partial |
| OpenGL, OpenCL, Core Image | yes | OpenGL through Zink on NVK by default on macOS 15.5 or later (`nvzink=0`: Apple's OpenGL over Metal); OpenCL is not built here; Core Image on Metal | OpenGL through Zink runs on one RTX 5050 with macOS 26.7.3 since 3.0.6; other cards and macOS builds are unproven. See [ZINK-OPENGL.md](ZINK-OPENGL.md) |
| Vulkan | no native driver (MoltenVK over Metal) | a native Vulkan runtime for applications that select it (`tools/nvk-bridge`, NVK reports Vulkan 1.4.363), and the Khronos loader v1.4.367 offered by `vulkan-for-apps.sh` | Have for applications that select it; seen with Minecraft 26.3 only |

## Media

| Feature | Real Mac | With this driver | Status and gap |
|---|---|---|---|
| H.264 and HEVC hardware decode | all | NVDEC decoders registered with VideoToolbox; Ada and Ampere also through the vendored NVEncVT (`docs/WINDOWS-PARITY-MEDIA.md`) | Have |
| AV1 and VP9 decode | M3 and later (AV1) | Ada and Ampere through NVEncVT; Blackwell needs its own decode class | Partial: no VP9 or AV1 hardware decode on RTX 50 |
| H.264 and HEVC hardware encode through VideoToolbox | all | Turing and Ampere built-in libraries, Ada through NVEncVT, Blackwell built-in; `EnableLowLatencyRateControl` accepted by NVEncVT (`tools/test_nvencvt_lowlatency.py`) | Have for the generations in the media page |
| What Sidecar, AirPlay to Mac and Screen Sharing ask of the encoder | a hardware HEVC or H.264 encoder that accepts real-time, low-latency settings | the encoder list above | Partial. The encoder side is present; whether those three services pick our encoder is only known from a run with an iPad or a second Mac, which the probe's `wifi.awdl0` row must pass first |
| ProRes decode and encode | Apple silicon has media engines; Intel Macs use the CPU | macOS's CPU path, which does not depend on the GPU | Have (Apple's own software path) |
| Dolby Vision, spatial audio with head tracking | depends on the display, speakers and AirPods | depends on display output and the audio driver | Unverified: Apple's requirements per feature were not confirmed here |
| Screen Sharing high-performance mode | Apple documents it for Apple-silicon hosts | The viewer asks the local HEVC decoder for `MaxDecoderPixelRate` (a stock Mac answers 7,365,427,200 pixels per second) and falls back to 60 frames per second without an answer. The host asks its HEVC encoder for `MaxEncoderPixelRate` and sets the profile `HEVC_Main444_AutoLevel`. NVEncVT decoders and encoders answer the rates, and with `NVMTL_NVENC_HEVC_444=1` the encoder lists HEVC Main 4:4:4 and 4:4:4 10 and takes only 4:4:4 input under them | Built for both sides (`tools/test_nvencvt_pixel_rate.py`, `tools/test_nvencvt_hevc444.py`). The 4:4:4 stream itself comes from the encoder's existing 4:4:4 path, whose output has not been decoded by a second Mac. Neither side has run with a second Mac |
| Screen capture by applications (sharing a window in a browser, OBS, Zoom) | yes | captured frames get video memory without a second fill (3.0); a program that reads them with the processor no longer holds WindowServer behind the copy engine (3.0.20) | Have; measured on the RTX 5050 with Firefox 161 (WindowServer's waits fell by 72 %). Google Meet still sends 5 frames a second from Firefox and Safari, as it asks of every browser that does not call itself Chrome |
| FairPlay streaming in Safari and the TV app (up to 1080p) | needs a GPU path macOS accepts | none documented for NVIDIA | Unverified. The measurement is to play protected content and read the decoder report; we do not bypass or redistribute anything |
| FairPlay 4K / HDR streaming | needs the T2 or Apple silicon | none | Blocked: T2 or Secure Enclave |
| Audio output and input (onboard, HDMI and DisplayPort) | yes | on macOS 26, which has no AppleHDA, the installer adds its HD Audio driver (NMXAudio) where an HD Audio controller is present; macOS 15 has Apple's HDA driver | Have. On macOS 26 remove `alcid` and `alcverbs` from boot-args, or the onboard sound stays with AppleALC and does not appear |
| Camera | built-in camera works through the USB video class driver | USB cameras work with macOS's own driver; MIPI cameras have no driver | Partial; not the GPU driver's |

## What does not depend on the GPU driver

These rows depend on the OpenCore setup (SMBIOS, identity, kexts) and on the Wi-Fi and Bluetooth card, not on this
driver. The original project's setup tool generated and checked the identity, chose the Wi-Fi and laptop kexts and
gated the models; this fork does none of that: OpenCore is set by hand (README, *Install*).

| Feature | Real Mac | Status and gap |
|---|---|---|
| iCloud, App Store, iMessage, FaceTime sign-in | valid serial, board serial, hardware UUID, ROM, working NVRAM, en0 built-in | Depends on the identity in your OpenCore config; whether Apple accepts the sign-in is Apple's decision. With the optional AMFI trust chain App Store and iMessage are not qualified yet ([SIGNED-GRAPHICS-EXPERIMENT.md](SIGNED-GRAPHICS-EXPERIMENT.md)) |
| AirDrop, Handoff, Universal Clipboard, AirPlay to Mac, Continuity Camera, phone calls and SMS relay | AWDL + Bluetooth LE | Partial: only with a Wi-Fi card and driver that raise `awdl0` |
| Universal Control, Sidecar | AWDL (or USB), a model Apple allows; Sidecar also a hardware HEVC encoder | Partial; USB works without awdl0. Encoder: see Media |
| Instant Hotspot, Auto Unlock with Apple Watch | Bluetooth LE + Wi-Fi | Partial. Unverified which part of each depends on awdl0 |
| Find My (this Mac) | Apple ID + location; offline finding needs the T2 or Apple silicon | Partial. Offline finding is Blocked: T2 or Secure Enclave |
| iPhone Mirroring, Activation Lock, Touch ID, Apple Pay with the Secure Element | T2, Secure Enclave or Apple silicon | Blocked |
| Apple Intelligence (writing tools, Image Playground, Siri models), Neural Engine | Apple silicon only | Blocked: needs Apple silicon |
| Core ML on the GPU, Metal Performance Shaders, Vision, Live Text | yes | Have for the operators proven so far on the Metal path (`docs/WINDOWS-PARITY.md`, Video and compute); Live Text and Visual Look Up unverified |
| FileVault | APFS volume encryption; no T2 needed | Depends on the EFI (VirtualSMC); `mac_parity_probe.sh` reports `security.filevault_usable` |
| Secure Boot, Apple signing | T2 or Apple silicon | Blocked: T2. The driver needs `SecureBootModel` Disabled and SIP at `0x0A43` (README, *OpenCore settings*) |
| Recovery, Migration Assistant, Time Machine, software updates | built into macOS | macOS's own; the OpenCore picker lists Recovery |
| Battery, Low Power Mode, brightness keys, trackpad gestures, Thunderbolt and USB | SMC, HID, Apple multitouch | Depend on the laptop kexts and USB map of your EFI; not measured here |
| Apps for Apple silicon only, iPhone and iPad apps, Xcode simulators | Apple silicon (Rosetta runs Intel apps there; the reverse does not exist) | The original's ARM apps translator is not part of this fork. Universal apps run their Intel slice |

## Power and input

| Feature | Real Mac | With this driver | Status and gap |
|---|---|---|---|
| Sleep and wake | yes | system sleep and wake with GPU resume, video memory kept (`docs/SLEEP-WAKE.md`) | Have on NVIDIA; the board's BIOS wake sources still matter |
| Power Nap, standby, hibernate | Intel Macs support them | PC firmware often mishandles the wake they cause | Not measured; `mac_parity_probe.sh` reports `power.powernap` and `power.pmset` |
| Keyboard backlight | HID and SMC | `kexts/NMKbdBacklight` (opt-in `nmkbl=1`) serves both routes macOS uses: the SMC key `LKSB` (non-T2 Macs) and a virtual built-in HID keyboard with the vendor element 0xFF00:0x0F, feature report 0xBF and output report 0xB0 (T2-era Macs, which is every Intel Mac macOS 26 supports). It forwards to ASUS WMI, Lenovo IdeaPad/Legion/LOQ (`KBLC`, `HALS`/`SALS`) and ThinkPad (`MLCG`/`MLCS`) | Built, off, compiled against the real SDK headers and host-tested, never run. Of 648 ACPI tables the original project collected, 33 carry the ASUS interface and 98 the Lenovo IdeaPad ones. Dell is built from the public Dell SMBIOS call encodings, with the buffer size read from Dell's descriptor (never run). MSI is built on the 22 firmware strings the public msi-ec table gives an address for, through ACPI's embedded-controller address space; any other firmware is refused (never run). HP and Acer: the firmware changes the light itself and the public Linux drivers ignore the notification, so there is no call to drive. ASUS N-KEY and Razer: user-space setters (`tools/asus`, `tools/razer`) from the public report layouts, never run on such a keyboard |
| NFC | none on a Mac | n/a | n/a |
