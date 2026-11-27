# NVIDIA card support and validation

The driver uses NVIDIA's r620 open RM, HAL, and GSP stack. That stack targets Turing and later
([upstream compatibility](https://github.com/NVIDIA/open-gpu-kernel-modules#compatible-gpus)).
The installer carries 251 distinct display-card PCI device IDs from NVIDIA's 620.70.06 table (`SUPPORTED_IDS` in
[`package/install.sh`](../package/install.sh)). The kernel matches NVIDIA display
and 3D-controller PCI classes, then asks NVIDIA's own supported-device gates to accept the card.
A card being in the table doesn't mean a given laptop works: its display wiring, firmware and BAR
setup still have to cooperate.

| Family | Examples covered by the device table | Run with this fork (not a certification of any card) |
| --- | --- | --- |
| Turing | GTX 1650/1660, RTX 20, TITAN RTX, Quadro T1000/T2000/RTX, NVIDIA T400/T600/T1000, T500/T550/T600/T1200 laptop | None |
| Ampere | RTX 30, RTX A workstation and laptop cards | None |
| Ada | RTX 40, supported RTX workstation and laptop cards | None |
| Blackwell | RTX 50 and supported RTX PRO cards | RTX 5050 (GB207) only, on macOS 26.7.3: the desktop, card checks (3.0.1, 3.0.7), OpenGL games through Zink and Vulkan games through the frontend (3.0.6 to 3.0.12), GPU Caps phases 1 and 2B, and 3.0.20 (2026-11-26), signed with the owner's CA and run with AMFI enforcing |

This fork has run on one card, the RTX 5050, and on one macOS, 26.7.3; macOS 15 and the kits for 10.7 to 14 have not
been tried with it. The original project's 2.9 recorded an RTX 5060 in its automated tests and RTX 5070 and 5080
reported working; those runs were of its builds, not of this fork's. The exact build and test boundaries are in
[STATUS.md](STATUS.md). Device ID coverage does not imply feature parity, nor does a successful boot prove every
graphics or control path. A report from another card is welcome on the
[fork's issue tracker](https://github.com/TheFormidable/nvidia-macos-driver/issues).

### Older cards (GTX 900 and GTX 10 series, TITAN X/Xp/V, Quadro M/P, Kepler)

These cards are not supported, and there is no date for them. The driver is built on NVIDIA's open
kernel modules, which run on the GPU System Processor (GSP). Only Turing and newer cards have one,
so the installer stops on anything older and changes nothing.

There are two ways to drive an older card, and the original project checked both:

- NVIDIA's closed Linux kernel driver (branch 580 for Maxwell, Pascal and Volta; 470 for Kepler).
  Running it on macOS would mean converting and patching NVIDIA's binary, and its licence forbids
  modifying or disassembling it. Neither project ships that without NVIDIA's permission.
- The open nouveau driver. On GTX 900 and newer it can only run the card at its boot clocks, because
  the power firmware has to be signed by NVIDIA, so the card would run at a small fraction of its
  speed. It would also be a separate driver of several months' work.

If you are choosing a card for this driver, pick a GTX 1650 or newer. Mac Pro owners with a Kepler
card (for example a GTX 680) can keep using OpenCore Legacy Patcher's Kepler support on macOS 15
and earlier; that has not been verified on macOS 26 by either project. The kits for macOS 10.7 to 14 in the package
carry the same GSP-based kernel driver built for those releases, so they need a Turing or newer card as well.

Headless compute cards are not promised a macOS desktop. A laptop also needs a display/output route the NVIDIA card can drive; accepting
its PCI ID does not implement an Optimus display mux.

## Startup correction

When BAR placement uses the fallback, automatic GPU startup waits 100 seconds. Previously the
40-second boot-hold deadline ran from driver start, released the hold first, and prevented the
later display-arm operation. The deadline now includes the settling delay and then gives GPU
bring-up 40 seconds. The actual settling delay is exposed as `nvrm-autogo-settle-ms`.

In the original project (2.9), an RTX 5060 passed a guarded boot with a deliberately forced 100-second settling delay, then
passed a second boot with the production 500 ms delay. Both runs armed the display, loaded all
four kexts, and kept WindowServer stable for 60 seconds without a new desktop/kernel crash.
That shows the timing fix works, but it was not a physical small-BAR card. Resizable BAR on
is still the tested setup.

## Desktop presentation

WindowServer submits a display-pipe swap immediately after `commitAndWaitUntilSubmitted`.
The NVIDIA kernel path cannot order that swap after the plugin's NVK fence. Submission alone
therefore allows scanout to read a surface the GPU is still drawing, causing flashing under
cursor movement or continuous redraw. WindowServer now waits for command-buffer completion
before that call returns. Other applications retain the submission-only behavior.

A CPU regression executes the actual method with mocked command buffers, checks both process
paths and error propagation, and proves the previous submission-only method fails the desktop
ordering test. The physical desktop acceptance also requires page flipping and 60 seconds of
stable WindowServer operation with no flip-latch timeouts or new desktop/kernel crashes.

When a card still flashes under cursor movement with that ordering in place, `-nvrmnoflip` keeps
the composited frame on the copy path: NVRM never sets `debug.nvaccel_iop_flip`, so the display
reads a finished copy instead of the surface the GPU may still be writing. It costs one copy per
frame and is the supported way to trade that for a stable picture. NVRM turns zero-copy scan-out
on about 60 seconds after it releases the boot hold (`NVRM::autoGo`), which is why a desktop that
flashes may look clean for the first minute and then start.

## Shader target selection

The vendor compiler is a developer path (`NVMTL_VENDOR_COMPILER`), and the NVIDIAShared.bundle it loads is no
longer in the package ([HOW-IT-WORKS.md](HOW-IT-WORKS.md)). It selects the SM target from the chip name reported by NVK. Unknown
chip names now refuse that optional path and use NVK/NAK instead of guessing `sm_120`. CPU tests
cover Turing, Ampere, Ada, Hopper, Blackwell, absent names, and unknown names. This prevents an
unknown chip from receiving a cubin compiled for a guessed architecture.

## Rendering and application evidence

In the original project's 2.9, on an RTX 5060 with macOS 15.8.1, the diagnostic candidate and signed release
candidate passed:

- Eight texture row-pitch/offset cases: zero incorrect pixels.
- Twelve private/IOSurface render-target cases, five repetitions each: zero incorrect sampled pixels.
- Five MPS Gaussian blur runs: zero incorrect image components.
- A Core Image filter graph: zero incorrect interior image components.

Diagnostic runs produced nonempty per-run logs, zero GPU faults, and an unchanged WindowServer
PID. These tests cover rendering and system image-processing frameworks. They are not a
validation matrix for every application or every listed card.

## Reports that can explain card-specific failures

Open an issue on the [fork's issue tracker](https://github.com/TheFormidable/nvidia-macos-driver/issues) and attach
what [TROUBLESHOOTING.md](TROUBLESHOOTING.md#reporting-a-problem) lists: the version, `sw_vers`, the card's PCI
device id, your boot-args, the whole output of `sudo ./install.sh`, `sysctl debug | grep -e nvrm -e nvaccel`, and the
kernel log and the plugin's `NullMoth:` lines of that boot. A release build of the plugin sends only failures
to the system log, and counts lines such as `vkCreateDevice -> -8` among them even when they do not contain `FAIL`
(`plugin/nvmtl_log_failure.h`). A successful device-table lookup cannot prove a card initialized correctly.

## Regressions

Run `python3 tools/test_card_support.py` on macOS with command line developer tools. It checks
that the installer's `SUPPORTED_IDS` is sorted and unique and that this page's count matches it, then
compiles production snippets for startup timing, SM target selection, release failure classification
and desktop completion ordering. It requires neither GPU access nor a driver installation.
`python3 tools/check_device_table.py <README.md of open-gpu-kernel-modules 620.70.06>` proves the table against
NVIDIA's own list: every id is in it, every id left out is a headless data-centre or mining part, every id falls in
exactly one generation row of `plugin/nvmtl_dlss.c`, and this page's count matches.
