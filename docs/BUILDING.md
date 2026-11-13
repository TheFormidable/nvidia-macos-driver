# Building the driver from source

`./build.sh` builds the whole driver on a Mac with one command: it examines the Mac, installs what is missing after
asking, downloads every outside source at a pinned commit or checksum, builds each component in order, and can stage
an installable package. It never uses `sudo` and never installs the driver; at the end it prints the command for that.

En español: [BUILDING.es.md](BUILDING.es.md).

> **Written and tested without a Mac.** `./build.sh` has run on one Mac (Intel, macOS 26.7, Xcode 26.5) only in part:
> `doctor`, `env`, `--dry-run` and `fetch` ran, and it built the plugin, the video library (and the RM on the way) and
> the small tools; the kexts stopped at its check for GNU patch and Python 3.10, which that Mac lacked (the check now
> asks only for what the kexts use: see [Tools each component needs](#tools-each-component-needs)). None of the changes
> made to `build.sh` after that run has run on a Mac yet: the tool check per component, the choice of Homebrew and of
> the tools taken from `PATH`, the layout of the working directory, rm's compile list, `fetch`'s git set-up, the
> stop without Xcode (also for a developer folder that is gone), the `patch` the scripts get (with `-f`) and the `PATH`
> brew is given were tested only against the stand-ins described next. It was written
> for macOS's `/bin/bash` 3.2 and its BSD tools, and tested on Linux against stand-ins for macOS's tools, Xcode,
> Homebrew, rustup, the git remotes and each component's build script (`tools/test_build_sh.py`). On a system other than
> macOS only `doctor`, `fetch`, `test`, `env` and `--dry-run` work. `deps` has not installed anything real, NVK and the
> translator have not been built by it, and no package has been staged with it. What that means for the first run:
> expect a step to stop on something no stand-in imitated (an Xcode or Homebrew detail, a formula name, a compiler
> warning). Run `./build.sh doctor` first, then `./build.sh`; when a step stops, the message names it and gives a report
> file to attach to an issue (see [When a build fails](#when-a-build-fails)). A step that stops never leaves a
> half-built result behind as finished, so after the fix the next run continues from that step. The component scripts
> themselves have been run by hand on the owner's Intel Mac (macOS 26.7, Xcode 26.5) for the kexts and the RM; what that
> showed about a short account name is handled (see the `rm` row of [Troubleshooting](#troubleshooting)).

## Requirements

- A Mac with macOS 15.6 or later, Intel or Apple silicon. Xcode 26 does not run on older systems, so `deps`, `build`,
  `package` and `all` refuse them at the start. Apple silicon is supported as a build host (the release scripts
  default to an Apple-silicon Rust toolchain); every part is built for x86_64, the architecture the driver runs on.
- **Xcode 26 or later**, the full Xcode, selected with `xcode-select`. The command line tools alone are not enough:
  the plugin imports Metal 4 headers (`Metal/MTL4CommandQueue.h`) that only the macOS 26 SDK has. No Kernel Debug Kit
  is needed: no build step reads one. On a Mac with neither Xcode nor the command line tools, every command but
  `help` stops at once with the instructions (`doctor` names Xcode as the one thing missing): macOS's `git`, `make`,
  `python3` and `xcrun` are then stand-ins that offer to install the command line tools, and none of them is run.
  The same stop comes when the developer folder that `DEVELOPER_DIR` or `xcode-select -p` names is not there or has no
  `usr/bin/xcrun` (an Xcode removed or renamed after it was selected): Apple's tools would then all fail.
- A git clone of this repository (not a downloaded archive): the plugin, translator and video library are built from
  git revisions. The path of the clone, of the working directory and of your home folder must not contain a space.
- The network, for `fetch`, for `deps`, and for every NVK build (see [What is not automated](#what-is-not-automated-and-why)).
- Homebrew, for NVK, the Vulkan loader and the translator only (see [Tools each component needs](#tools-each-component-needs)):
  every other component builds with Xcode and what macOS has. When Homebrew is missing, `./build.sh deps` explains how
  to install it and offers to run its official installer, but only after you type `yes` (`--yes` does not count for this).
- Free disk space: not measured on a Mac yet. The NVIDIA trees, Mesa, LLVM from Homebrew and the NVK build together
  take several gigabytes.
- Time, on an **Intel Mac**: Homebrew no longer has ready-made builds (bottles) of these formulas for Intel Macs, so
  `deps` has Homebrew compile each missing one here. LLVM 22 alone takes hours and tens of gigabytes while it builds.
  `deps` says so before it asks. On Apple silicon Homebrew installs ready-made builds.

## Quick start

Clone the repository and the branch this file came from, with its owner and branch name in place of `<owner>` and
`<branch>`. The original project's repository does not have `build.sh`; a clone of it gives a tree without the script.

```bash
git clone -b <branch> https://github.com/<owner>/nvidia-macos-driver.git
cd nvidia-macos-driver
./build.sh doctor
./build.sh
```

`./build.sh doctor` changes nothing of yours (it writes only what [What each command writes](#what-each-command-writes)
lists for it: a temporary file of its own and xcrun's cache): it prints what this Mac has, what is missing and what is too old, and exits 1
when something required is missing. `./build.sh` (the same as `./build.sh all`) runs doctor, installs what is
missing after asking, fetches the pinned sources, builds every component and prints a summary. Run it again after a
change: steps whose inputs did not change are skipped.

To make an installable package you need a release package as its base (why: [Installing the result and going
back](#installing-the-result-and-going-back)):

```bash
./build.sh --base-package ~/Downloads/nullmoth-nvidia-2.9.tar.gz package
```

## What is installed where

The working directory (default `~/nullmoth-build`, option `--workdir`) holds everything `build.sh` makes, downloads
or installs for itself. It must be outside the checkout, and may not be your home folder, `/` or `~/nvmtl-build`
(the original author's folder). `build.sh` marks it with a file `.nullmoth-build-workdir` and deletes only inside a
folder that carries that mark.

| What | Where |
|---|---|
| Homebrew formulas, only the missing ones: `gpatch` (only when no `patch` here does what the scripts need), `ninja`, `cmake`, `pkgconf`, `glslang`, `spirv-tools`, `llvm@22` (or `llvm` while Homebrew's is 22), `python@3.14`, and `spirv-llvm-translator` only while Homebrew's `llvm` is 22 | Homebrew's own prefix (`/opt/homebrew` on Apple silicon, `/usr/local` on Intel). Nothing you already have is upgraded or removed by `build.sh`; Homebrew itself may upgrade an outdated dependency of a formula it installs, and the prompt says so |
| SPIRV-LLVM-Translator v22.1.8 for LLVM 22, built from source when no matching one is found (Homebrew's follows its current LLVM, 23 today) | `<workdir>/tools/spirv-llvm-translator`; its sources in `<workdir>/src`, its log `<workdir>/logs/spirv-llvm-translator.log` |
| Python packages for Mesa (meson 1.12.1, mako, MarkupSafe, PyYAML, packaging), each checked against its SHA-256 | `<workdir>/tools/venv` |
| Rust 1.98.1 with the x86_64-apple-darwin target, bindgen-cli 0.73.2, cbindgen 0.29.4 (rustup-init 1.29.3, SHA-256 checked, run with `--no-modify-path`) | `<workdir>/tools/rustup` (RUSTUP_HOME), `<workdir>/tools/cargo` (CARGO_HOME) |
| Links to exactly the tools the build may use; the first folder on every step's PATH, followed by the Python environment's `bin` (Homebrew's `bin` never is on it) | `<workdir>/tools/bin` |
| `ssh`, `rsync`, `scp` that act on this Mac (see [Components](#components-and-their-order)) | `<workdir>/tools/host` |
| Downloaded sources, verified: NVIDIA's open-gpu-kernel-modules 620.70.06 and 610.57.04, Mesa 26.3.0-rc3, Vulkan-Headers and Vulkan-Loader v1.4.367, open-gpu-doc, RyzenAdj v0.19.0, two WebKit headers | `<workdir>/src`, and `<workdir>/build/...` in the layout the build scripts already expect (see below) |
| Outputs | `<workdir>/out` (kexts in `out/kexts`, NVK in `out/nvk`, and so on) |
| One log per step, one stamp per step, failure reports | `<workdir>/logs`, `<workdir>/stamps`, `<workdir>/reports` |
| The staged package and its tarball | `<workdir>/package/pkgroot`, `<workdir>/package/nullmoth-nvidia-local-<commit>.tar.gz` |

### What each command writes

| Command | Writes |
|---|---|
| `doctor`, `env`, `help`, and every command with `--dry-run` | Nothing of yours. Each makes one temporary file of its own in your temporary folder (`$TMPDIR`), with a scratch folder beside it for the patch test, and removes them when it ends; `xcrun` keeps its lookup cache (`xcrun_db`) in that same per-user temporary folder, as it does whenever any Xcode tool is run. They read the Mac (`sw_vers`, `sysctl`, `xcode-select -p` before anything else, `xcodebuild -version`, `xcrun`, `file` on each tool taken from `PATH`, each chosen tool's `--version`), the checkout (`git status` without refreshing its index; a dry run never records local changes with `git stash create`) and the working directory. They never run `brew info` or `brew install`: a dry run of `deps` names `llvm@22` without asking Homebrew which LLVM formula it has |
| `deps` | Homebrew's prefix, and Homebrew's own cache and logs (`~/Library/Caches/Homebrew`, `~/Library/Logs/Homebrew`) for the formulas it installs, after a yes. Its `brew info` questions keep Homebrew's formula list in `<workdir>/tools/homebrew-cache`; with `--offline` it asks none. Everything else in `<workdir>/tools` and `<workdir>/src` |
| `fetch` | `<workdir>/src`, `<workdir>/build/...` (the layout below) and the translator's crates in `<workdir>/tools/cargo`. Its git runs without your git hooks (`core.hooksPath`), file system monitor and global attributes file, and its `git init` and `git clone` without your template folder (`init.templateDir`) |
| `build`, `all`, `package` | `<workdir>` only, with one exception: when the checkout has uncommitted changes, the plugin, translator and video builds take them as a commit object made with `git stash create`, which is stored in the checkout's `.git` (on no branch; `git gc` removes it in time) |
| `test` | `<workdir>/logs` and, once the NVIDIA tree is fetched, the links in `<workdir>/tools/bin` (the patch chain is applied with the build's own `patch`); the host tests make temporary folders of their own and remove them |
| `clean` | Deletes inside `<workdir>` only |

### The working directory's layout

`build.sh` gives the build scripts `NULLMOTH_BUILD_ROOT=<workdir>`. The scripts that came with 2.9 put their trees in
a folder `build` under it, with names they fix themselves:

| Folder | What | Named by |
|---|---|---|
| `<workdir>/build/ogkm620/open-gpu-kernel-modules-620.70.06` | NVIDIA's 620.70.06 tree as fetched, never changed | build_release_inputs.sh |
| `<workdir>/build/ogkm620/open-gpu-kernel-modules-610.57.04` | NVIDIA's **610.57.04** tree (for NVK). It sits in a folder called `ogkm620` because build_release_inputs.sh puts it there; the name is kept so that the scripts and `build.sh` agree | build_release_inputs.sh |
| `<workdir>/build/rm620/ogkm-620.70.06` | The patched 620.70.06 copy in which RM and modeset are built | build_release_inputs.sh, build_nvencvt.sh |
| `<workdir>/build/gpu-src/mesa` | The Mesa clone | build_release_inputs.sh, build_plugin_*.sh |
| `<workdir>/build/nvencvt-headers`, `<workdir>/build/nvencvt` | NVENC headers and the libnvencvt build | build_nvencvt.sh |
| `<workdir>/build/plugin-release`, `plugin-dev`, `xlate-release` | Copies of the plugin and translator sources and their builds | build_plugin_*.sh, build_xlate_release.sh |

An earlier `build.sh` gave the scripts `NULLMOTH_BUILD_ROOT=<workdir>/build`, so these folders were in
`<workdir>/build/build/...`. Nothing uses that folder now: `./build.sh clean` (or `clean sources`) deletes it, and the
next `fetch` lays the sources out again (the downloads in `<workdir>/src` are reused). A `NULLMOTH_BUILD_ROOT` you set
yourself is used as it is.

`build.sh` itself writes nothing to your shell start files, `~/.cargo`, `~/.rustup`, `/Library` or `/usr/local`
(on an Intel Mac, `/usr/local` is Homebrew's prefix, so the formulas Homebrew installs land there). A `RUSTUP_HOME` or
`CARGO_HOME` from your shell is not used. Nothing is written into the checkout.

All sources come from the pins in [build/pins.sh](../build/pins.sh): for each, its URL, the variable that replaces
the URL with a mirror, the tag and the commit id (or the SHA-256 of a single file). A source that does not match its
pin stops the build and nothing of it is used. The file also says, for every number, which script or document of the
repository requires it, and `build.sh` compares the pins with the scripts that use the same sources before every
fetch and build (`./build.sh doctor` prints the comparison).

## Commands

<!-- reference: commands -->
| Command | What it does |
|---|---|
| `all` | The default when no command is given. doctor, then deps when something is missing (unless `--no-install`), fetch, build every component, package when a base package is given, and a summary |
| `doctor` | Examines only: the Mac, Xcode and its SDK, every tool and its version, the pins against the scripts, the sources fetched so far, and things that stop a build on some Macs. Changes nothing of yours ([What each command writes](#what-each-command-writes)). Exit 1 when something required is missing or too old |
| `deps` | Installs what is missing: Homebrew formulas (after a yes, or `--yes`), SPIRV-LLVM-Translator for LLVM 22, the Python environment and Rust (the last three in the working directory). Explains how to install Xcode, which no script can do |
| `fetch` | Downloads the pinned sources into the working directory and verifies each commit id or SHA-256. Also fetches the translator's crates (`cargo fetch --locked`) once Rust is there, so that its build runs offline |
| `build [component...]` | Builds the named components and what they need, or all of them. Names and groups: [Components](#components-and-their-order) |
| `package` | Builds what is not up to date, then stages a package from a release package (`--base-package`) with every part this checkout builds replaced. Needs `--accel both` |
| `test` | The pin comparison, the host tests (`tools/run_all_tests.py`) and, once the NVIDIA tree is fetched, the NVRM patch chain against it. Exit 1 when any of them fails |
| `env` | Prints `export` lines for every variable `build.sh` gives the build scripts, to run one of them by hand with the same environment |
| `clean [outputs\|sources\|tools\|all]` | Deletes inside the working directory only. `outputs` (the default): outputs, stamps, logs, the package and the RM work tree. `sources`: the fetched sources (and the stamps). `tools`: everything under `<workdir>/tools`, the Python environment and Rust included (and the stamps). `all`: everything except the mark, after a yes |
| `help` | The commands, components and settings |
<!-- end -->

## Options and settings

Every option is also an environment variable and a line of the configuration file. Precedence: an option on the
command line wins over the environment, which wins over the configuration file, which wins over the default. The
configuration file is `~/.config/nullmoth/build.conf` when it exists, or the file named by `--config` or
`NM_CONFIG`; [build/build.conf.example](../build/build.conf.example) shows every setting. It holds `NAME=value`
lines (an `export ` before the name, quotes around the value and `~/` at its start are understood); it is read, never
run, and an unknown name stops `build.sh`. A yes/no setting takes `1`, `0`, `yes`, `no`, `true` or `false`.

<!-- reference: options -->
| Option | Variable | Default | Meaning |
|---|---|---|---|
| `--workdir DIR` | `NM_WORK` | `~/nullmoth-build` | Where everything `build.sh` makes, downloads and installs goes. Outside the checkout, no spaces |
| `-j N`, `--jobs N` | `NM_JOBS` | the number of cores | Parallel compile jobs (given to the scripts as `JOBS` and `NVK_JOBS` unless you set those) |
| `-y`, `--yes` | `NM_ASSUME_YES` | `0` | Answer yes to `build.sh`'s questions (installing formulas and Rust, `clean all`). Never to Homebrew's own installer |
| `--no-install` | `NM_NO_INSTALL` | `0` | Install nothing; say what is missing. With `all`, stop after doctor when something is missing |
| `--offline` | `NM_OFFLINE` | `0` | Never use the network: only what an earlier fetch left. NVK cannot be built offline |
| `-n`, `--dry-run` | `NM_DRY_RUN` | `0` | Print every command that would run, including installs and fetches; change nothing |
| `--flavour release\|dev`, `--flavor` | `NM_FLAVOUR` | `release` | `release` builds what a release ships. `dev` builds the plugin with its diagnostics compiled in (`build/build_plugin_dev.sh`), for finding a fault; do not hand it to others |
| `--accel 15\|26\|both` | `NM_ACCEL` | `both` | Which NVAccel.kext builds to make: for macOS 15, for macOS 26, or both. A package needs both |
| `-v`, `--verbose` | `NM_VERBOSE` | `0` | Show each step's output on the terminal as well as in its log |
| `--force` | `NM_FORCE` | `0` | Rebuild the named components (or all) even when they are up to date |
| `--base-package FILE` | `NM_BASE_PACKAGE` | none | A release package (`nullmoth-nvidia-<version>.tar.gz` or its unpacked `pkgroot`) the package is made from |
| `--config FILE` | `NM_CONFIG` | `~/.config/nullmoth/build.conf` when it exists | The configuration file |
| `-h`, `--help` | | | The same as `help` |
<!-- end -->

`--jobs=N`, `--workdir=DIR` and the other options with a value also take the `--name=value` form, and `-jN` works.

### Settings without an option

<!-- reference: variables -->
| Variable | Default | Meaning |
|---|---|---|
| `NM_BASE_PACKAGE_SHA256` | none | The base package's SHA-256 from the release page; checked before it is used |
| `NM_PACKAGE_VERSION` | the base's | Written to the package's `VERSION` (install.sh keeps only digits and dots) |
| `NM_OGKM_URL` | the URL in build/pins.sh | A mirror of NVIDIA's open-gpu-kernel-modules (both trees) |
| `NM_MESA_URL` | the URL in build/pins.sh | A mirror of Mesa; any clone that has the pinned commit works |
| `NM_KHRONOS_URL` | the URL in build/pins.sh | A URL prefix that holds `Vulkan-Headers` and `Vulkan-Loader`. Set by you, it is also what the loader build clones from; unset, the loader is built from the verified copies in `<workdir>/src/khronos` |
| `NM_OPEN_GPU_DOC_URL` | the URL in build/pins.sh | A mirror of NVIDIA's open-gpu-doc |
| `NM_RYZENADJ_URL` | the URL in build/pins.sh | A mirror of RyzenAdj |
| `NM_WEBKIT_SPI_URL` | the URL in build/pins.sh | A folder (URL) holding `CMBaseObjectSPI.h` and `VTVideoDecoderSPI.h` |
| `NM_SPIRV_LLVM_URL` | the URL in build/pins.sh | A mirror of SPIRV-LLVM-Translator (deps builds it for LLVM 22) |
| `NM_SPIRV_HEADERS_URL` | the URL in build/pins.sh | A mirror of SPIRV-Headers |
| `NM_RUSTUP_URL` | static.rust-lang.org, rustup 1.29.3 | Where `rustup-init` is downloaded from (its SHA-256 is checked either way) |
| `RUSTUP_DIST_SERVER` | rustup's | Where rustup downloads the toolchain from |
| `DEVELOPER_DIR` | the selected Xcode | An Xcode other than the one `xcode-select` selects |
| `NM_BREW` | found | The `brew` to use. When set, it is the only Homebrew considered, whatever its architecture. Unset, `build.sh` uses the Homebrew of the Mac's processor only (`/usr/local` on Intel, `/opt/homebrew` on Apple silicon, also from a shell under Rosetta), and otherwise a `brew` on `PATH` only when it resolves into that same prefix (a wrapper in no Homebrew prefix, or one that resolves into the other architecture's prefix, is not taken). A Homebrew of the other architecture, whose formulas are built for the other processor and cannot be linked here, is named by `doctor` but used only when `NM_BREW` points at it. Every `brew` call runs with `HOMEBREW_PREFIX`, `HOMEBREW_CELLAR`, `HOMEBREW_REPOSITORY`, `HOMEBREW_PATH` and `HOMEBREW_BREW_FILE` cleared and with `PATH` set to that Homebrew's `bin` followed by `/usr/bin:/bin:/usr/sbin:/sbin`, so a login shell's settings for another Homebrew cannot redirect it: Homebrew keeps the caller's `PATH` for a formula's post-install step and some of its builds, where that Homebrew's programs would otherwise come first. A tool taken from `PATH` (`python3`, `patch`, `gpatch`, `ninja`, `cmake`, `pkg-config`, `glslangValidator`) is left out when it belongs to the Homebrew of the other architecture (its file, after its symlinks, is there; or its `#!` interpreter is; or `file` says it is a Mach-O program without this processor's code); `doctor` says which and why |
| `NM_PYTHON` | found | A Python 3.10 or newer to make the virtual environment with |
| `NM_LLVM_PREFIX` | Homebrew's `llvm@22` or `llvm` | An LLVM 22.x with its clang libraries, when Homebrew has none |
| `NM_OGKM620_PRISTINE` | fetched | An unpacked NVIDIA 620.70.06 tree to use instead of fetching (checked by its `version.mk`) |
| `NM_OGKM620` | `<workdir>/build/rm620/ogkm-620.70.06` | A 620.70.06 tree whose RM and modeset archives are already built: the `rm` and `kms` steps are then skipped |
| `NM_OGKM610` | fetched | An unpacked NVIDIA 610.57.04 tree to use instead of fetching |
| `NM_MESA_GIT` | fetched | A Mesa clone that holds commit 90a959eb (mesa-26.3.0-rc3). The release plugin takes its Vulkan headers from that clone's work tree |
| `NM_VULKAN_INCLUDE` | Mesa's `include` | Vulkan headers for the development plugin |
| `NM_LILU_DEBUG_KEXT` | none | For the optional kexts `nmkbl` and `nmhda`: a Lilu DEBUG kext |
| `NM_VIRTUALSMC_SRC` | none | For `nmkbl`: a VirtualSMC source tree |
| `NM_MACKERNELSDK` | none | For `nmkbl` and `nmhda`: a MacKernelSDK tree |
| `NULLMOTH_BUILD_ROOT` | `<workdir>/build` | The build folder the existing scripts default their trees under |
| `NULLMOTH_BUILD_HOST` | this Mac | The release scripts' build Mac (an ssh host). Unset, `build.sh` runs them on this Mac |
| `NM_LOCAL_HOST_PATH` | `/usr/bin:/bin:/usr/sbin:/sbin` | The PATH of a command the release scripts run on this Mac as their build host |
| `NV_VERSION` | `620.70.06` | NVIDIA's version, for build_rm620.sh and build_kms620.sh; another value than the pin stops `build.sh` |
| `KMS_ARCHIVE` | the script's | build_nvrm.sh: another modeset archive |
| `RE` | the script's | build_nvrm.sh and accel_build.sh: the reconstructed accelerator headers |
| `MINVER` | the script's | fb_build.sh: deployment target |
| `FB_DEFS` | the script's | fb_build.sh: extra defines |
| `KHDR` | the script's | build_primary_selector.sh: kernel headers |
| `KLIB` | the script's | build_primary_selector.sh: kernel libraries |
| `MACOSX_DEPLOYMENT_TARGET` | `15.0` | Given to every step; it matters for NVAccel, whose compile line names no deployment target |
| `NVK_PATH` | `<workdir>/tools/bin` | build_nvk.sh: the folder put first on PATH (the tools it may use) |
| `NVK_SYSROOT` | the script's | build_nvk.sh: a sysroot for pkg-config |
| `NVK_JOBS` | `NM_JOBS` | build_nvk.sh: ninja jobs |
| `NVK_EXTRA_PATCHES` | none | build_nvk.sh: patches applied after the chain |
| `NVK_SKIP_PATCHES` | none | build_nvk.sh: patches of the chain to leave out (the result is then not the release's) |
| `NVK_REF_EXPORTS` | none | build_nvk.sh: a file of exports to compare with |
| `PKG_CONFIG_PATH` | SPIRV-LLVM-Translator, SPIRV-Tools and LLVM | Yours is put after these (pkg-config takes the first match, and Homebrew's translator follows its current LLVM), without its folders inside the other architecture's Homebrew |
| `LIBCLANG_PATH` | LLVM's `lib` | bindgen's libclang |
| `JOBS` | `NM_JOBS` | build_rm620.sh and build_kms620.sh: make jobs |
| `NULLMOTH_RUST_VERSION` | `1.98.1` | build_xlate_release.sh: the Rust version expected; another one builds different bytes, and `build.sh` warns |
| `NULLMOTH_RUST_BIN` | `<workdir>/tools/rustup/toolchains/1.98.1-<host>/bin` | build_xlate_release.sh: the folder with cargo and rustc |
| `NULLMOTH_CARGO_HOME` | `<workdir>/tools/cargo` | build_xlate_release.sh: the crate cache |
<!-- end -->

### Set by build.sh for the scripts

These are not settings: `build.sh` derives them, gives them to the build scripts and prints them with `./build.sh env`.

<!-- reference: variables -->
| Variable | Value |
|---|---|
| `OGKM` | the built 620.70.06 tree (`NM_OGKM620`), for fb_build.sh, accel_build.sh and rmcc.py, which otherwise default to `~/ogkm610` |
| `NVRM_PRISTINE_OGKM` | the pristine 620.70.06 tree, so that build_nvrm.sh checks that the patch chain still applies |
| `RUSTUP_HOME`, `CARGO_HOME`, `RUSTUP_TOOLCHAIN` | Rust in the working directory, version 1.98.1 |
| `NM_LOCAL_BUILD_HOST` | the host name `build/local_build_host.sh` serves |
| `CARGO_NET_OFFLINE` | `true` with `--offline` |
| `PYTHONDONTWRITEBYTECODE` | `1`, so that no `__pycache__` is written into the checkout |
| `PATH` | `<workdir>/tools/host` (only for the release scripts), `<workdir>/tools/bin`, `<workdir>/tools/venv/bin` (Python finds its environment only when started from there), then `/usr/bin:/bin:/usr/sbin:/sbin` |
| `NM_PACKAGE_NAME`, `NM_BUILD_INFO` | the tarball name and the build description `build/package_local.sh` puts in the package |
<!-- end -->

Every step also gets `CC`, `CXX`, `OBJC`, `CFLAGS`, `CXXFLAGS`, `OBJCFLAGS`, `CPPFLAGS`, `LDFLAGS`, `NM_TAHOE`,
`SDKROOT`, the search paths `PKG_CONFIG_PATH` (given back to NVK as above), `PKG_CONFIG_LIBDIR`, `CMAKE_PREFIX_PATH`,
`LIBRARY_PATH`, `CPATH`, `C_INCLUDE_PATH`, `CPLUS_INCLUDE_PATH`, `OBJC_INCLUDE_PATH`, and `HOMEBREW_PREFIX`,
`HOMEBREW_CELLAR`, `HOMEBREW_REPOSITORY` removed from its environment: an exported `CC` breaks build_rm620.sh,
`NM_TAHOE` selects the macOS 26 accelerator, and a login shell may point the others at a Homebrew of the other
architecture. `STEPS` and `NM_REV` (read by build_release_inputs.sh) and `X` and `RELEASE` (build_xlate.sh and
build_plugin.sh, the original author's development scripts for trees in `~/nvmtl-build`) are not used: `build.sh`
replaces those scripts.

To run one script by hand the way `build.sh` does:

```bash
eval "$(./build.sh env)"
bash build/fb_build.sh kexts "$NM_WORK/out/kexts/fb"
```

`./build.sh --dry-run --force build <component>` prints the exact command line of that step.

## Components and their order

<!-- reference: components -->
| Component | What | Script it runs | Needs |
|---|---|---|---|
| `rm` | NVIDIA's RM core for macOS (libnvkernel.a), patched with kexts/NVRM/patches | build/build_rm620.sh | |
| `kms` | NVIDIA's modeset core (libnvkms.a) | build/build_kms620.sh | rm |
| `nvrm` | NVRM.kext | build/build_nvrm.sh | rm, kms |
| `fb` | NVRMFB.kext | build/fb_build.sh | rm |
| `accel` | NVAccel.kext for macOS 15 and 26 (`--accel`) | build/accel_build_all.sh, or build/accel_build.sh for one | rm |
| `agdc` | NVRMAGDC.kext | build/build_agdc.sh | |
| `nmsmn` | NMSMN.kext (carried in the package) | kexts/NMSMN/build.sh | |
| `ryzenadj` | ryzenadj (carried in the package) | tools/ryzenadj-macos/build.sh | |
| `airdis` | nvmtl-air-dis, the plugin's AIR decoder | build/build_air_dis.sh | |
| `dlss` | dlss_cache and nm_unzstd | build/build_dlss_cache.sh | |
| `osver` | libnvmtl_osver.dylib | build/build_osver.sh | |
| `nvk` | NVK, the Vulkan driver (libvulkan_nouveau.dylib) | build/build_nvk.sh | |
| `boost` | nvmtl-boost | tools/nvmtl-boost/build.sh | nvk |
| `vulkan` | the Khronos Vulkan loader | build/build_vulkan_loader.sh | |
| `plugin` | the Metal plugin (NVMTLDriver) | build/build_plugin_release.sh, or build/build_plugin_dev.sh with `--flavour dev` | |
| `xlate` | the shader translator (libnvmtl_translate.dylib) | build/build_xlate_release.sh | |
| `nvencvt` | libnvencvt.dylib (video) | build/build_nvencvt.sh | rm |
| `selector` | NVPrimarySelector.kext; optional, not in `all` | build/build_primary_selector.sh | |
| `nmkbl` | NMKbdBacklight.kext; optional, built only when `NM_LILU_DEBUG_KEXT`, `NM_VIRTUALSMC_SRC` and `NM_MACKERNELSDK` are set | kexts/NMKbdBacklight/build.sh | |
| `nmhda` | NMHDA.kext; optional, built only when `NM_LILU_DEBUG_KEXT` and `NM_MACKERNELSDK` are set | kexts/NMHDA/build.sh | |
<!-- end -->

### Tools each component needs

Beyond Xcode, what each component's scripts (and the helpers they run: kexts/NVRM/rmcc.py, tools/test_patch_chain.sh,
tools/nvencvt_header_abi.py, NVK's generators) really use, and the oldest version known to work. `build.sh` tests each
tool for what the scripts do with it, not for its name, for every component a build will run (the ones pulled in as
dependencies too) before the first step starts.

| Component | Needs, besides Xcode | How build.sh tests it |
|---|---|---|
| `rm` | GNU make (Xcode's 3.81; only `make -n`); a `patch` that applies the kexts' chain with `-p1 --fuzz=0 --dry-run` and `--no-backup-if-mismatch` (macOS's `patch 2.0-12u11-Apple` does; the scripts get it with `-f` added, below); `python3` 3.8 or later (f-strings; macOS's 3.9 does) | `make --version` says GNU Make; the patch applies a change in a temporary folder as the scripts call it; `python3 --version` on the step's PATH |
| `kms` | GNU make, `python3` 3.8 or later | the same |
| `nvrm` | `python3` 3.8 or later (rmcc.py: `subprocess.run(capture_output=, text=)`, 3.7); the same `patch` (the chain test against the pristine tree) | the same |
| `fb`, `accel`, `agdc`, `nmsmn`, `ryzenadj`, `airdis`, `dlss`, `osver`, `plugin`, `selector` | nothing else | |
| `nvencvt` | `python3` 3.8 or later on the build Mac's PATH (`NM_LOCAL_HOST_PATH`, default `/usr/bin:/bin:/usr/sbin:/sbin`) | `python3 --version` there |
| `vulkan` | cmake 3.22.1 or later and ninja | Homebrew's or on PATH |
| `xlate` | Rust 1.98.1 in the working directory | `deps` installs it |
| `nvk` (and `boost`, built from it) | Python 3.10 or later (Mesa's meson.build) with meson, mako, PyYAML, packaging in `<workdir>/tools/venv`; LLVM 22 with its clang libraries; SPIRV-LLVM-Translator for LLVM 22; SPIRV-Tools; glslang; pkg-config; Rust with bindgen and cbindgen; a `patch` that also creates files (NVK's chain adds files; Homebrew `gpatch` when macOS's cannot) | each one, as `doctor` shows |

`deps` installs `gpatch` only when no `patch` found passes both tests. 3.8 is the oldest Python the kext and video
scripts were run with; nothing in them needs 3.10.

They are built in the order of the table. Groups: `all` (the seventeen components above the optional ones), `kexts`
(rm to nmsmn) and `userland` (airdis to nvencvt). The kext order is fixed: build_rm620.sh copies the pristine NVIDIA
tree, applies the patches in kexts/NVRM/patches and writes the command lines every later kext step reads back;
build_kms620.sh builds in the same tree; NVRM links both archives; NVRMFB and NVAccel compile against the patched tree.

What `build.sh` does around the existing scripts, so that they no longer need the original author's machine:

- Every path a script would take from the author's home folder is given explicitly: the NVIDIA trees, `OGKM` (the
  scripts default to `~/ogkm610`, the wrong tree), the output folders, Rust and the crate cache in the working directory.
- The scripts' `patch` is `<workdir>/tools/bin/patch`, a two-line script that runs the chosen patch with `-f`.
  macOS's patch asks a question on the terminal, wherever its output is sent, when a patch names a file that is not
  there or looks applied already, and waits for the answer. build_rm620.sh and tools/test_patch_chain.sh try each
  patch from the tree's root and then from `src/nvidia-modeset`, with patch's output sent to `/dev/null`, so without
  `-f` the `rm` step would wait at the first NVKMS patch for an answer to a question nobody sees. With `-f` such a
  try fails at once and the script tries the other folder; a change that looks applied fails instead of being reversed.
- build_rm620.sh does not stop when compiles fail; `build.sh` reads its `failed: N of M` line and stops the step when
  N is not 0. Each time the `rm` step runs, its work tree is deleted first, so no old object or old patch set survives.
- build_rm620.sh and build_kms620.sh end with exit 6 when the account name or the computer's short name occurs in the
  archive as a whole word (a path, `user@host`); inside another word (an account "ana" in "banana") it is not one. They
  do not print the names, and they give NVIDIA's makefiles neutral values for the build user and host
  (`NV_BUILD_USER=build NV_BUILD_HOST=localhost`), so that `make-n.txt` names neither. The archive itself never held
  them: its identity string is replaced with fixed text before anything is compiled, and the compile lines carry no
  absolute path and no debug information.
- What `build.sh` prints (doctor, dry runs, progress, STOP lines, the summary, the failure report) writes your home
  folder as `~`; `env` writes it as `"$HOME/..."`, which works when pasted. The step logs in `<workdir>/logs` keep full
  paths.
- Any command of a step that fails stops the step: an output an earlier build left behind is never stamped as new.
- The plugin, translator and video library release scripts send their compile to a build Mac over ssh. `build.sh`
  sets `NULLMOTH_BUILD_HOST` to a name that `build/local_build_host.sh` serves: linked as `ssh`, `rsync` and `scp`
  first on those scripts' PATH, it runs their commands on this Mac (no Remote Login needed) and refuses any other host.
  These three scripts build from a git revision: `HEAD` when the checkout is clean, otherwise a commit object of the
  working tree made with `git stash create` (untracked files are not part of it; the summary says which).
- build_vulkan_loader.sh clones from the verified copies under `<workdir>/src/khronos` (`NM_KHRONOS_URL`), and
  build_nvencvt.sh finds its headers already fetched and verified, so neither downloads anything unchecked.
- After NVK, `build.sh` stops when libvulkan_nouveau.dylib loads a library from `/usr/local`, `/opt/homebrew` or
  `/Users`: a Homebrew library leaked into the build.

## Building one piece

```bash
./build.sh build fb            # NVRMFB.kext, and the RM core first if it is not up to date
./build.sh build nvrm accel    # several
./build.sh build kexts         # a group
./build.sh --force build plugin
./build.sh --flavour dev build plugin
./build.sh --accel 26 build accel
```

Each step has a stamp in `<workdir>/stamps` that records what it was built from: `build.sh` itself, the pins of the
sources it uses, the Xcode, SDK, compiler and linker versions (and LLVM, glslang, meson and Rust where they matter),
the values of the variables it reads, the contents of its scripts and source folders in the checkout (committed or
not), and the stamps of the components it needs. A step whose stamp is unchanged and whose output exists is skipped;
any change rebuilds that step and the steps built on it, and nothing else. A changed `build.sh` rebuilds everything.
A step that stops leaves no stamp. One run at a time per working directory: a second one stops at the lock and names
the first; a lock left by a run that is gone is taken over.

## Installing the result and going back

`build.sh` never installs anything. A package cannot be made from this repository alone: install.sh refuses a
package without `nullmoth-runtime-check`, and the plugin bundle's Info.plist, `nvmtl-allow.txt`, `air-runtime`, the
video libraries, the Vulkan loader 1.4.362 and a few configuration files have no source here
([PROVENANCE.md](PROVENANCE.md)). The release itself starts each package from the previous one. So
`./build.sh package` does the same: with a release package as the base (`--base-package`, a
`nullmoth-nvidia-<version>.tar.gz` from Releases or its unpacked `pkgroot`), `build/package_local.sh` checks the base
against its own SHA256SUMS (and against `NM_BASE_PACKAGE_SHA256` when set), replaces every part this checkout builds
(the kexts, NVK, the translator, the plugin binary, the scripts, the DLSS tools, the licences), refreshes their
SHA256SUMS lines, adds `NULLMOTH-LOCAL-BUILD.txt` (commit, local changes, pins), runs `package/finish_package.sh`
like a release, then makes the check install.sh makes first (`shasum -a 256 -c SHA256SUMS`). install.sh checks a
package's integrity, not who built it, so it accepts this one. The ryzenadj built here has another SHA-256 than the
one the 1401 app pins, so the app will not install it; the driver is not affected.

Install it yourself, as for a release (read [README.md](../README.md), "Install — by hand", first: OpenCore must be
set up):

```bash
cd ~/nullmoth-build/package/pkgroot
sudo env CHECK=1 ./install.sh
sudo ./install.sh && sudo shutdown -r now
```

The first install.sh line is a dry run: it checks the package and this Mac and changes nothing. Run the commands as
shown (`./install.sh`, not `bash install.sh`: install.sh recognises a running install by that name).

To go back, install the release you used as the base in the same way, from its own unpacked `pkgroot`: it replaces
every file the tester build changed, and the next restart loads the release. To remove the driver completely:
`sudo ./uninstall.sh && sudo shutdown -r now` from any package (it refuses while SIP is enabled; README.md,
"Uninstall"). install.sh prints `Undo: sudo ./uninstall.sh <backup folder>`, but uninstall.sh does not restore that
backup: it removes the driver. With the 1401 app, "1401: Remove NVIDIA driver" in the OpenCore picker puts the Mac
back as it was before the app's install.

## What is not automated, and why

| What | Why |
|---|---|
| Installing Xcode, accepting its licence, its first launch | Apple allows it only through the App Store or developer.apple.com and `sudo`. `doctor` and `deps` print the exact steps |
| Installing Homebrew | Its installer asks for your password; `build.sh` runs it only after you type `yes` |
| Installing the driver, restarting | Needs `sudo` and changes the system; `build.sh` prints the commands |
| A package without a release package | Files install.sh needs have no source in this repository (above) |
| Building NVK offline | build_nvk.sh exports a fresh Mesa tree for each build, and meson downloads six Rust crates into it, checked against Mesa's wrap files |
| The GSP firmware | install.sh downloads NVIDIA's firmware at install time, not at build time |
| `nmkbl`, `nmhda` | They need Lilu, VirtualSMC and MacKernelSDK at the revisions the EFI uses, which no script records; give them with the three variables |
| Signing with a real identity | Releases are signed by their maintainer. In a tester package the NVK, translator and other libraries are signed ad hoc, and a kext is signed ad hoc where the base package's copy was signed |
| Byte-identical results | The release's Xcode version is recorded nowhere, and an Intel Mac may record other compiler flags in the RM archive than the release's Apple-silicon build Mac (`doctor` says so) |

## Troubleshooting

| Message or symptom | Cause | What to do |
|---|---|---|
| `STOP: the working directory contains a space` (or the checkout path, or the home folder) | The release scripts' remote commands and build_nvk.sh's compiler flags split on spaces | Use `--workdir` with a path without spaces; move the checkout |
| `STOP: this Mac runs macOS 14...` | Xcode 26 needs macOS 15.6 or later | Update macOS, or build on another Mac |
| doctor: `MISSING full Xcode 26.0 or later: only the command line tools are selected` | `xcode-select` points at the command line tools | Install Xcode 26, then `sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer` |
| `STOP: the developer folder xcode-select -p names, ..., does not exist` (or `has no usr/bin/xcrun`, or `DEVELOPER_DIR names`) | The selected Xcode was removed or renamed, `DEVELOPER_DIR` names a wrong folder, or a macOS update left the command line tools incomplete | `sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer` with the Xcode you have, or correct or unset `DEVELOPER_DIR` |
| doctor: `MISSING Metal/MTL4CommandQueue.h` | The selected Xcode has an older SDK | Select Xcode 26, or set `DEVELOPER_DIR` |
| `Homebrew has no LLVM 22 formula today` | Homebrew dropped `llvm@22` | Install LLVM 22.x with its clang libraries, set `NM_LLVM_PREFIX` to it and run `./build.sh deps` again (it builds SPIRV-LLVM-Translator against it) |
| `SPIRV-LLVM-Translator ... does not match LLVM` | Its version must have LLVM's major.minor | `./build.sh deps` builds one that matches |
| `STOP: step 'spirv-llvm-translator' failed` | Building SPIRV-LLVM-Translator v22.1.8 against LLVM 22 failed | Report it with the report file; the full log is `logs/spirv-llvm-translator.log` |
| `STOP: the pins and the scripts disagree` | build/pins.sh and a build script name different versions | Run `./build.sh doctor`; change both together |
| `... gave commit X for ..., the pin is Y` or `... has SHA-256 ..., the pin is ...` | A mirror or download is not the pinned source | Check the mirror variable; nothing of it was used |
| `STOP: another build.sh run (pid N) is using ...` | Two runs on one working directory | Wait for it or stop it; a lock of a run that is gone is taken over |
| `offline, and ... is not in ... yet` | `--offline` before any fetch | Run `./build.sh fetch` with the network once |
| `offline: NVK needs the network` | See [What is not automated](#what-is-not-automated-and-why) | Build NVK with the network |
| `<component> needs: ...` (for example `a-patch-that-applies-the-chains(gpatch)`, `python3-3.8`, `cmake/ninja`) | A tool that component, or one it needs, uses is missing or does not do what the scripts ask of it; nothing was built | `./build.sh doctor` says which; `./build.sh deps` installs it |
| rm or kms ends with exit 6 | build_rm620.sh and build_kms620.sh stop when NVIDIA's archive contains your account name or the computer's short name as a whole word (not inside another word) | The log names the command that shows the lines. If the name is a common word (user, mac, admin), build from another account or change the computer's local hostname (System Settings > General > Sharing) |
| `STOP: N compile commands for arch/nvalloc/unix/src/osapi.c in .../compile_cmds.sh, one expected` (nvrm, accel) | The RM's compile list is incomplete; the kexts take their -D flags from NVIDIA's own command for osapi.c and stop rather than take another file's | `./build.sh --force build rm` (build.sh rebuilds rm in a fresh folder and then everything built from it); by hand, run build_rm620.sh in a new work folder |
| `STOP: make -n listed N compile commands, fewer than ...` | build_rm620.sh or build_kms620.sh would have replaced a complete list with a shorter one; it kept the old one | Use a new work folder |
| `build_rm620.sh reported compile failures` | A compile of NVIDIA's RM failed; build_rm620.sh itself would not stop | Read the `failed:` lines in `logs/rm.log` |
| a step stops at a warning ceiling (`warn_ceiling`) | The ceilings in build/warnings.txt are 0: a compiler or linker other than the release's may print a new warning | Report it with the report file and your Xcode version |
| `libvulkan_nouveau.dylib loads a library from outside macOS` | A Homebrew library (often libelf in /usr/local on Intel) was linked | Report it with the report file |
| `the base package does not match its own SHA256SUMS` | The release package is damaged or changed | Download it again |
| `a package must carry NVAccel for macOS 15 and 26` | `package` with `--accel 15` or `26` | Use `--accel both` |
| the summary says the plugin was built from "the working tree ... with its uncommitted changes" | Your checkout has local changes | Expected; untracked files are not part of that build |

## When a build fails

A step that fails stops the run; no later step starts. The message says:

```
STOP: step 'fb' failed (exit status 1).
last lines of its log:
    ...
full log:    ~/nullmoth-build/logs/fb.log
report file: ~/nullmoth-build/reports/failure-fb-<time>.txt   (attach it to a bug report; ...)
```

The report file holds what is needed to reproduce the failure: the date, the step and its exit status, the command
line, the macOS version and build, the Mac model and processor, the version of Xcode, the SDK, clang, ld, git, make,
patch, Python, meson, ninja, cmake, pkg-config, glslang, LLVM, SPIRV-LLVM-Translator, SPIRV-Tools, Rust, bindgen,
cbindgen and Homebrew, the checkout's commit and branch, whether it has local changes and which files, the pins, the
variables the build gives the scripts, and the last 80 lines of the step's log. Your home folder is written as `~`.
Read it before you post it: the names of your changed files are in it.

To report: open an issue, say what you ran, and attach the report file. Fix what the message or the
[troubleshooting table](#troubleshooting) points to and run the same command again: the steps that finished are
skipped and the failed one runs again. `-v` shows a step's output while it runs.

## How it is tested

`tools/test_build_sh.py` runs the real `build.sh` on a Linux machine in a sandbox that imitates a Mac: stand-ins for
`sw_vers`, `uname`, `xcode-select`, `xcodebuild`, `xcrun`, `brew`, `rustup`, `cargo`, `python3` and the other tools,
real git against local repositories that stand in for the remotes, and a scratch copy of this tree in which every
component build script is replaced by a stand-in that records what it receives. It checks doctor on a Mac with
nothing installed, deps with and without installing, fetch (and a tampered remote), offline, dry run, the build
order, the variables every step gets, skipping and rebuilding, a failing step and its report, a failed script after
an earlier good build, build_rm620.sh's exit 6, the step's python3, that the scripts' `patch` never waits for an answer at the terminal, SPIRV-LLVM-Translator when Homebrew's LLVM is 23,
which of two Homebrews is used, that no tool of the other architecture's Homebrew is taken from `PATH`, that no brew
call keeps the caller's `HOMEBREW_` location variables or `PATH`, that `fetch` runs none of your git hooks, template or filters, that
nothing opens Apple's install dialog on a Mac without Xcode or runs with a developer folder that is gone, what a Mac with Xcode alone builds and what stops before the first step, that doctor,
dry runs and env write nothing (Homebrew's cache included) and print no home path,
an interrupted run,
two runs at once, paths with spaces, Apple silicon and an unsupported macOS, `env`, this document against the script,
and the pins against the scripts that use them; each part has at least one control that breaks `build.sh` (or its
input) on purpose and shows the check then fails. It runs `build.sh` under `/bin/bash`, or the bash named by
`BUILD_SH_BASH`, and says so when that is not bash 3.2, the `/bin/bash` of macOS; it has passed under GNU bash
3.2.48 and 5.2 on Linux, and once on the owner's Intel Mac, at a commit before the last changes to `build.sh` (the
Homebrew of the other architecture, the tools taken from `PATH`, `fetch`'s git set-up, the stop without Xcode). `tools/test_rm_compile_list.py` runs the real build_rm620.sh,
build_kms620.sh and accel_build.sh on a small stand-in NVIDIA tree (real make and patch): a second run in a built
folder keeps the compile list complete and the kexts' -D flags the same, and the personal-string check takes whole words. What it cannot show is how the real Xcode, Homebrew and compilers behave: that is what
the first run on a Mac will show.
