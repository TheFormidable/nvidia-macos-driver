# Developing the driver

This is the guide for working on the source of TheFormidable's fork (branch `fable`, at 3.0.20, Release 2). To report
a bug, see `CONTRIBUTING.md`. For how the parts fit together, read `docs/HOW-IT-WORKS.md` first; for what has run on a
card, `docs/STATUS.md`.

## How changes get in

Pull requests go to the `fable` branch of <https://github.com/TheFormidable/nvidia-macos-driver>. The fork's `main`
is an untouched mirror of the original NullMoth project and takes no pull requests. A small pull request with a clear
test is reviewed much faster than a big one.

1. Fork the repository and clone your fork **with full history** (`git clone -b fable <your fork>`, without
   `--depth`; `git fetch --unshallow` repairs a shallow clone): the tests' negative controls and the switch registry
   read earlier revisions of the source and fail or skip on a shallow clone.
2. Make one change on its own branch, made from `fable`.
3. Run the host tests (below) on your machine. They must pass before you open the pull request.
4. Open the pull request against `fable` with the template filled in.

## What you need

| Part | Folder | Needs |
|---|---|---|
| Metal plugin (`NVMTLDriver.bundle`) | `plugin/` | Xcode 26 |
| Shader translator (`libnvmtl_translate.dylib`) | `translator/` | Rust (the version `build/pins.sh` pins; `./build.sh deps` installs it) |
| Vulkan back end (NVK and NAK, as patches on Mesa) | `nvk/` | Meson, Ninja, Rust, Mesa at the commit in `nvk/MESA_BASE` |
| Native OpenGL (Mesa's Zink on NVK) | `zink/` | Xcode 26, Mesa 26.3.0 (`build/build_zink.sh`) |
| Vulkan runtime for applications | `tools/nvk-bridge/` | see `tools/nvk-bridge/README.md` |
| Kernel drivers (NVRM, NVRMFB, NVAccel, NVRMAGDC and the smaller ones) | `kexts/` | Xcode 26, NVIDIA's `open-gpu-kernel-modules` at tag `620.70.06` |
| Kits for macOS 10.7 to 14 (experimental) | `build/legacy/`, `package/legacy/`, `gl/` | Apple's SDK and Kernel Debug Kit of each release (`docs/BUILDING.md`, "Building the kits") |
| DLSS support | `dlss/` | see `dlss/README.md` |
| Installer, uninstaller and what goes in the package | `package/` | a shell |
| Build scripts | `build/`, `build.sh` | |
| Tests and developer tools | `tools/` | Python 3.12 or newer, the Xcode command line tools |
| Design notes | `docs/` | |

The host tests need only a Mac with Python and the Xcode command line tools. They need no NVIDIA card.

## Tests

GitHub Actions is currently disabled on the fork: the workflows in `.github/workflows` are kept (and
`tools/test_ci_workflow.py` holds them to their rules), but nothing runs on a push or a pull request. Run the checks
yourself and say in the pull request what you ran:

```bash
python3 tools/run_all_tests.py
```

This runs every host test in `tools/` (Python, C, C++ and shell) and prints the failures with the end of their
output (`-j N` runs N at a time, `--tail N` prints more of a failed test's output). Its last line is the count, for
example `304 passed, 0 failed, 3 skipped, 9 not run, of 316`. A few tests need a built translator or NVIDIA's source
tree; the runner lists them at the end with the reason, and they run at the build step instead. `./build.sh test`
runs the same tests (with the Python of the build's working directory when it has one) and the pin check.

Two more checks need no card:

- `bash build/kext_check.sh` compiles every kext source to an object, on a Mac or on Linux, and checks each import
  against what the macOS 15 and macOS 26 kernels export.
- `python3 tools/switch_registry.py check` holds the source to `tools/switches.tsv`, the list of every switch the
  driver reads.

Each fix comes with a test in `tools/` that fails on the code before the fix (a negative control) and passes after it.
Read two existing ones before writing yours, for example `tools/test_stream_uncached.py` and
`tools/test_nvrmfb_connect_owed.py`.

## Building

`README.md` ("Build from source") lists the build commands in order, and `docs/BUILDING.md` explains every command,
option and component of `./build.sh`. `docs/HOW-IT-WORKS.md` explains what each part is.
`build/build_release_inputs.sh` builds everything a release package is made from; its header lists the tools and
source trees it expects and the environment variables that point at them.

Rebuild both accelerators whenever `nvrm_vram_abi.h` or `nvrm_gpuva_abi.h` changes: an accelerator built against an
older ABI loads, but the framebuffer driver refuses all its memory requests and the desktop stays black.

## Testing on real hardware

- A bad kernel driver stops the Mac from starting. Before you install a build of your own, make sure you can get
  back: keep the package's `uninstall.sh` and the backup folder the installer names, keep an EFI with `-nvoff` ready,
  and read `README.md` ("If something goes wrong") and `docs/TROUBLESHOOTING.md` ("Getting back in").
- Change one component at a time (the plugin, the Vulkan layer, or the kernel drivers), restart, and check that the
  desktop comes up before you change the next.
- Never test on a machine whose data you cannot lose.
- `docs/TESTING-ON-HARDWARE.md` is the checklist for a card, and says what to attach to an issue.
- Kernel changes need a boot on a real card before they are called release-ready. A change may go into `fable`
  before that, with its evidence limits written down, a way back and host tests. Say in the pull request exactly what
  you ran it on (the card, the Mac or PC, the macOS build, the commit) and what you saw, or that no boot test ran.
- A fix is proven on the application or display that showed the problem, not only on a small test program.

## Rules for changes

- No Apple code. No Apple binaries and no files copied from macOS go into this repository. The class layouts read
  from Apple's binaries so that the kexts can be the classes Apple's frameworks expect are listed, with why they are
  kept, in `docs/PROVENANCE.md`.
- No NVIDIA code that NVIDIA has not published as open source. The kernel side builds on NVIDIA's
  `open-gpu-kernel-modules`; GPU firmware is downloaded on the user's machine under NVIDIA's redistribution licence and
  is not stored here.
- Other people's code keeps its licence and its credit. If you add or update third-party code, say where it comes
  from, pin the exact version, and add its licence text to `licenses/` and a line to `NOTICE`. A change taken from a
  pull request to the original project names its author in `CHANGELOG.md`.
- On by default, with one way back. A change that replaces an older path is on for everyone and keeps exactly one
  way back to the old path, written `name=0` (a boot argument, or a line of the settings file
  `/Library/GPUBundles/nvmtl/nvrm610.conf`). Every switch is a row of `tools/switches.tsv`; a new row can only be such
  a way back (with a review version), a test hook, a read-only observer, a setting the settings tool offers or an
  internal name, never an opt-in. No new writable sysctls. Write the row of a new switch by hand; then
  `python3 tools/switch_registry.py finalize` brings the rest of the registry up to the tree and writes
  `docs/SWITCHES.md`.
- General fixes only. A change fixes the Metal, Vulkan, OpenGL or display behaviour that is wrong, for every card the
  code supports. A special case for one application or one machine is not accepted.
- Find the cause first. Say in the pull request what you measured and how it shows the cause. A change that makes a
  symptom go away without a known cause is not accepted.
- A machine that works today must still work. A behaviour change sits behind the condition that identifies the
  broken case.
- No secrets, keys or personal data in code, tests or logs. The signing keys of the optional AMFI route
  (`tools/make_amfi_anchor.sh`) never go into any repository.
- Fail loud. If a step cannot do what it says, it stops and says why.
- Keep comments plain: explain why the code does what it does.

## Sign-off

The original NullMoth project requires a `Signed-off-by:` line on every commit (`git commit -s`, the Developer
Certificate of Origin, <https://developercertificate.org>). This fork does not check for one. Sign off if you mean to
offer the same change to the original project as well.

## Where to start

`docs/STATUS.md`, "Validation priorities", lists what has not been shown yet; `CONTRIBUTING.md` lists where help is
needed. If you own a card or a machine the fork has not run on, a measurement on it is often the most useful thing.
Ask in an issue before starting something large, so two people do not build the same thing.
