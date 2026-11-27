# NVRMFB display audio mapping

What NVRMFB publishes so that AppleHDA puts a display's audio under Sound > Output, and the single-display setting
`NVRMFBDisplayAudioCodecInfo`. The display-codec association was introduced by
[Hi-Jiajun in PR #284](https://github.com/nullmoth/nvidia-macos-driver/pull/284) and extended by Ersiter in PR #313,
both to the original project (its 2.5.2). How the codec and pin are chosen today (per-connector overrides, automatic
matching through NMHDA, the fallback, several displays) is in [DISPLAY-AUDIO-MAPPING.md](DISPLAY-AUDIO-MAPPING.md);
this page is the part underneath it.

Both pages are about AppleHDA: macOS 15, or a macOS 26 where the user restored it. macOS 26 ships no AppleHDA; there the
installer adds the package's own HD Audio driver, NMXAudio, where an HD Audio controller is present
(`package/install.sh`).

## What is published

The five AppleHDA properties (`connector-type`, `port-number`, `av-signal-type`, `audio-codec-info`, `IODisplayEDID`)
and the shared CEA scan follow PR #284. The extension reads the connector type from NVKMS and copies the owning GPU's
`hda-gfx` value without changing its `OSData` or `OSString` type. Without a chosen codec and pin, NVRMFB keeps its ELD
and `hda-gfx` publication and publishes no effective five-property audio route. The five route properties are published
only for an external display with a chosen codec and pin, an NVKMS HDMI/DisplayPort connector, and at least one CEA
Short Audio Descriptor. Disconnects clear the ELD and all association properties; reconnects and monitor swaps refresh
them.

## `NVRMFBDisplayAudioCodecInfo`

A four-byte `OSData` property on the NVRMFB personality or framebuffer service (codec address, function group, pin,
zero), passed through as `audio-codec-info`; it must identify the codec and pin used by the GPU. For example,
`00 01 05 00` is the RTX 5080 Laptop mapping of PR #313's tests (pin 05). It counts only while one external display is
connected, after a per-connector override and before automatic matching (DISPLAY-AUDIO-MAPPING.md, "Overrides and priority"). A
malformed value publishes no association. When nothing chooses a pin, a single non-MST external display gets
`00 01 04 00` (codec 0, pin 04), the mapping measured on an RTX 5060 desktop in #284.

## Checks

`python3 tools/check_nvrmfb_display_audio_mapping.py` compiled and ran the production connector-type and CEA-scan
helpers in a host fixture and checked the publication and cleanup path. It is not part of `tools/run_all_tests.py`, and
at 3.0.20 it stops at the start: it looks for `publishDisplayAudioProperties()` in `fb/nvrm-fb.cpp`, which has moved to
`fb/nvrm-audio-auto.inc`. The tests of the
current selection are listed in DISPLAY-AUDIO-MAPPING.md. The RTX 5080 Laptop, macOS 26.7, pin 05 playback result is
the PR author's hardware validation; other GPUs, connectors and concurrent displays remain unverified, and this fork
has no display audio result of its own.
