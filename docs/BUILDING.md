# Building the driver from source

`./build.sh` builds the whole driver on a Mac with one command: it examines the Mac, installs what is missing after
asking, downloads every outside source at a pinned commit or checksum, builds each component in order, and can stage
an installable package. It never uses `sudo` and never installs the driver; at the end it prints the command for that.

En español: [BUILDING.es.md](BUILDING.es.md).

> **What has run, and where.** Nothing built from this branch has been installed, loaded or run on an NVIDIA card, and
> no package made by `./build.sh package` has been installed. What has run:
>
> - On one Intel Mac (12 threads, macOS 26.7, Xcode 26.5). At commit 650db45, `./build.sh deps` started from an empty
>   working directory and built every tool NVK and the translator need into it in 46.5 minutes, without Homebrew. At
>   commit 3933ff6, with those tools kept, `deps`, `doctor` and `fetch` succeeded, `./build.sh --no-install build` built
>   all 17 components in 5 minutes 41 seconds with no warning, and `package` staged a package on release 2.9 in
>   27 seconds. Nothing outside the working directory changed. The host tests have not run whole on that Mac at that
>   commit. {{PENDING 5b}}
> - On GitHub's Intel runner (macos-15-intel: 4 processors, macOS 15, Xcode 26.3 with the 26.2 SDK), at commit 3933ff6.
>   303 of the 315 host tests passed and none failed (3 were skipped and 9 not run there), `tools/test_build_sh.py`
>   among them. The workflow that builds everything in an empty working directory, with the tools restored from
>   GitHub's cache, ran `deps`, `doctor` and `fetch`, built all 17 components in 30 minutes 20 seconds and staged the
>   package in 60 seconds; the result of its `./build.sh test` and of its checks of what was built is not stated
>   here. {{PENDING full-test}} {{PENDING full-prove}}
> - Only against stand-ins for macOS's tools, Xcode, Homebrew, rustup, the git remotes and each component's build
>   script (`tools/test_build_sh.py`, on Linux and on that runner): `deps` taking its tools from Homebrew, which is what
>   it does on an Apple-silicon Mac, and the stop on a Mac without Xcode.
> - Nowhere: a whole build on Apple silicon (see [Requirements](#requirements)), and a build of this branch's commits
>   after 3933ff6 on either Mac. {{PENDING 5c}}
>
> `build.sh` is written for macOS's `/bin/bash` 3.2 and its BSD tools. On a system other than macOS only `doctor`,
> `fetch`, `test`, `env` and a dry run of any command but `deps` work. A step can still stop on something neither the
> stand-ins nor these two Macs showed (another Xcode or SDK, a formula name, a compiler warning). Run
> `./build.sh doctor` first, then `./build.sh`; when a step stops, the message names it and gives a report file to
> attach to an issue (see [When a build fails](#when-a-build-fails)). A step that stops never leaves a half-built
> result behind as finished, so after the fix the next run continues from that step.

## Requirements

- A Mac with macOS 15.6 or later, Intel or Apple silicon. Xcode 26 does not run on older systems, so `deps`, `build`,
  `package` and `all` refuse them at the start. Every part is built for x86_64, the architecture the driver runs on.
  An Intel Mac compiles NVIDIA's RM and NVKMS with exactly the commands an Apple-silicon Mac uses (see
  [byte-identical results](#what-is-not-automated-and-why)).
  Apple silicon is meant to work as a build host (the release scripts default to an Apple-silicon Rust toolchain), but
  no whole build with `build.sh` has run on one: there `deps` takes its tools from Homebrew, a way that has run only
  against stand-ins. GitHub's Apple-silicon runner compiles and links the kexts, and at commit 95811c2 they had the
  same bytes as on its Intel runner. The host tests have not passed whole on Apple silicon: their last run that got a
  runner, at an earlier commit, passed 298 of 312 with 2 failures, in arms that have passed on the Intel runner since
  commit ef2a216, and GitHub has had no Apple-silicon runner free at any push after it.
- **Xcode 26 or later**, the full Xcode, selected with `xcode-select`. Whole builds have succeeded with two of them,
  both at commit 3933ff6: Xcode 26.5 (SDK 26.5) on the Intel Mac above, and Xcode 26.3 (SDK 26.2) on GitHub's Intel
  runner. Xcode 26.3 is the newest Xcode that runs on macOS 15.6 to 26.1 (Xcode 26.4.1, 26.5 and 26.6 need macOS 26.2
  or later), and with an SDK older than 26.4 the plugin compiles a part of its own that the 26.5 SDK leaves out.
  Xcode 26.0 to 26.2 have not been tried. The command line tools alone are not enough:
  the plugin imports Metal 4 headers (`Metal/MTL4CommandQueue.h`) that only the macOS 26 SDK has. No Kernel Debug Kit
  is needed: no build step reads one. On a Mac with neither Xcode nor the command line tools, every command but
  `help` stops at once with the instructions (`doctor` names Xcode as the one thing missing): macOS's `git`, `make`,
  `python3` and `xcrun` are then stand-ins that offer to install the command line tools, and none of them is run.
  The same stop comes when the developer folder that `DEVELOPER_DIR` or `xcode-select -p` names is not there or holds none
  of Apple's tools (an Xcode removed or renamed after it was selected): Apple's tools would then all fail.
- A git clone of this repository (not a downloaded archive): the plugin, translator and video library are built from
  git revisions. The path of the clone, of the working directory and of your home folder must not contain a space,
  and the working directory's none of `:` `;` `'` `"` `\` `$` or a backquote either.
- The network, for `fetch`, for `deps`, and for every NVK build (see [What is not automated](#what-is-not-automated-and-why)).
- The build tools of NVK, the Vulkan loader and the translator (see [Tools each component needs](#tools-each-component-needs)):
  every other component builds with Xcode and what macOS has. Where they come from is what `NM_TOOLS_FROM` says:
  - On an **Intel Mac** (`auto` there means `source`), `./build.sh deps` builds ninja, CMake, Python 3.14 (with the
    OpenSSL, zstd and xz it needs), pkgconf, SPIRV-Tools, glslang and LLVM 22 with clang from pinned sources with
    `build/toolchain.sh`, into `<workdir>/tools/toolchain`. **Homebrew is not needed** and nothing is installed outside
    the working directory. On an Intel Mac build.sh does not use Homebrew, even when it is installed: a copy of one of
    these tools that Homebrew or `PATH` offers is not used (`doctor` names it), and `build.sh` calls no `brew`. Only
    your own settings (`NM_PYTHON`, `NM_LLVM_PREFIX`, `NM_SPIRV_TOOLS_PREFIX`, `PKG_CONFIG_PATH`, `LIBCLANG_PATH`,
    `NVK_PATH`) are taken instead, and `doctor` says that they are yours. Homebrew's formulas for these tools had no
    ready-made builds (bottles) for Intel Macs when homebrew-core was read on 2026-11-13: through Homebrew, `deps`
    would have each of them and what they need compiled into `/usr/local`, and could change your `python3`. That way
    has run only against stand-ins.
    The only thing it does not build is GNU patch, which NVK's patches need when macOS's `patch` cannot create files:
    put it on `PATH` as `gpatch` (Homebrew's formula is `gpatch`); `deps` says so.
  - On **Apple silicon** (`auto` there means `homebrew`), a tool you already have is used wherever it is (a setting
    such as `NM_LLVM_PREFIX`, Homebrew, `PATH`), and `deps` installs a missing one from Homebrew's ready-made formulas. When Homebrew
    is missing, it explains how to install it and offers to run its official installer, but only after you type `yes`
    (`--yes` does not count for this). `NM_TOOLS_FROM=source` builds them as on an Intel Mac instead. This way has
    run only against the stand-ins of `tools/test_build_sh.py`, never on an Apple-silicon Mac.
- Time and disk, as measured with `build.sh`. On the Intel Mac above, `deps` from an empty working directory took
  46.5 minutes, downloads included (building LLVM about 30 of them), and 49 seconds when the tools were already
  there; `fetch` took 40 seconds, `build` of all 17 components 5 minutes 41 seconds, `package` 27 seconds. On
  GitHub's Intel runner, `deps` from nothing took 3 hours 53 minutes; with the tools restored from its cache, `deps`
  took 40 seconds, `build` 30 minutes 20 seconds and `package` 60 seconds. `deps` leaves the tools built from source
  (1.1 GB: each build tree is removed once its tool's checks pass), Rust (515 MB) and its crates (97 MB). After a
  whole build and package the working directory on the Intel Mac held about 8 GB (sources 3.4 GB, tools 1.8 GB,
  build trees 1.2 GB, outputs 1.1 GB, the package 492 MB). `deps` asks for 10 GB free before it starts LLVM and says
  how much is free when there is less. Keep the Mac awake and on power: a tool whose build is interrupted (Ctrl-C,
  sleep, power) is built again from the beginning by the next `./build.sh deps`, and LLVM is the long one. A tool
  that fails stops `deps` with its log named; the next `deps` starts at that tool.
- Time with `NM_TOOLS_FROM=homebrew` on an Intel Mac: Homebrew compiles each missing formula here, LLVM 22 the
  longest; no such run has been timed, and `deps` warns before it asks. To compile, Homebrew also needs
  Apple's command line tools besides Xcode, as new as this macOS: without them it stops at the first formula with
  `Xcode alone is not sufficient` or `Your Command Line Tools are too outdated`. `xcode-select --install` installs them,
  Software Update updates them; `pkgutil --pkg-info=com.apple.pkg.CLTools_Executables` shows the version you have.

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
| Homebrew formulas, only the missing ones: `gpatch` (only when no `patch` here does what the scripts need), `ninja`, `cmake`, `pkgconf`, `glslang`, `spirv-tools`, `llvm@22` (or `llvm` while Homebrew's is 22), `python@3.14`, and `spirv-llvm-translator` only while Homebrew's `llvm` is 22 | Homebrew's own prefix (`/opt/homebrew` on Apple silicon, `/usr/local` on Intel). Nothing you already have is upgraded or removed by `build.sh`; Homebrew itself may upgrade an outdated dependency of a formula it installs, and the prompt says so. Homebrew also installs what these formulas need and links the commands of those that are not keg-only (`llvm@22` is keg-only) into its `bin`, which macOS's default `PATH` puts before `/usr/bin` on an Intel Mac; with today's formulas that includes `python@3.15`, which `llvm@22` needs to build and which would become the `python3` of a shell with that `PATH` (the build itself takes `python@3.14`, from Homebrew's `opt` folder). So when the installs give Homebrew's `bin` a `python3` it did not have, as a link into a Python formula they added or one an earlier `deps` of this working directory unlinked (it notes those in `<workdir>/tools/.nm-brew-unlinked`, and drops one you have linked again yourself), `deps` runs `brew unlink` on that formula once they are done (or one of them fails, or you stop `deps` with Ctrl-C during them) and prints a line with the way back (`brew link <formula>`); a `python3` that was there before (a link that led nowhere does not count: your shell skipped it, and Homebrew replaces it), or a Python formula you already had, is left as it is. A `brew install` that ends with an error although its formula is now installed (Homebrew did not link a command over a file of yours that was in the way, and says so) does not stop `deps`: it warns and goes on, and its last lines say whether anything is still missing. After the installs `deps` names each command they added to Homebrew's `bin` that macOS also has in `/usr/bin`, `/bin`, `/usr/sbin` or `/sbin`, with the `brew unlink` that puts it back. A dry run states this rule and calls no `brew` at all. Each `brew install` runs with `HOMEBREW_NO_INSTALL_CLEANUP=1`, so Homebrew does not follow it with its own clean-up (older versions of what it installed or upgraded and, every 30 days, of every formula, then formulas nothing needs any more, dead links and empty folders in its prefix, old downloads): an upgraded dependency keeps its older version |
| With `NM_TOOLS_FROM=source` (the default on an Intel Mac), the missing build tools, built from pinned sources by `build/toolchain.sh` for this Mac's processor and for macOS 15.0 and later (`MACOSX_DEPLOYMENT_TARGET`). Each tool's stamp in `.nm-toolchain/` holds its pinned commit and recipe; a changed pin or recipe rebuilds that tool and the one that has it linked in (Python after OpenSSL, zstd or xz; glslang after SPIRV-Tools) and no other, and a tool without its stamp is not used. Every program and library they install links only macOS's own libraries and the toolchain's, and every link they install leads only into the toolchain, `/usr/lib` or `/System/Library`; ninja, CMake, Python, pkgconf, glslang and LLVM must print their pinned version: `build/toolchain.sh` checks it before it writes the stamp | `<workdir>/tools/toolchain`; build trees in `<workdir>/tools/toolchain-build` (each removed once its tool's checks pass), logs in `<workdir>/logs/toolchain`, verified sources in `<workdir>/src/git/tc-*.git` and `<workdir>/src/toolchain` |
| SPIRV-LLVM-Translator v22.1.8 for LLVM 22, built from source when no matching one is found (Homebrew's follows its current LLVM, 23 today). It is linked against one LLVM: `deps` notes which in `.nm-llvm` beside it (the toolchain's stamp of LLVM, or another LLVM's version and folder) and builds it again when the LLVM in use is another one, or when that note is missing | `<workdir>/tools/spirv-llvm-translator`; its sources in `<workdir>/src`, its log `<workdir>/logs/spirv-llvm-translator.log` |
| Python packages for Mesa (meson 1.12.1, mako, MarkupSafe, PyYAML, packaging), each checked against its SHA-256 | `<workdir>/tools/venv`; pip's download cache in `<workdir>/tools/pip-cache` (not `~/Library/Caches/pip`) |
| Rust 1.98.1 with the x86_64-apple-darwin target, bindgen-cli 0.73.2, cbindgen 0.29.4 (rustup-init 1.29.3, SHA-256 checked, run with `--no-modify-path`) | `<workdir>/tools/rustup` (RUSTUP_HOME), `<workdir>/tools/cargo` (CARGO_HOME) |
| Links to exactly the tools the build may use; the first folder on every step's PATH, followed by the Python environment's `bin` (Homebrew's `bin` never is on it) | `<workdir>/tools/bin` |
| `ssh`, `rsync`, `scp` that act on this Mac (see [Components](#components-and-their-order)) | `<workdir>/tools/host` |
| Downloaded sources, verified: NVIDIA's open-gpu-kernel-modules 620.70.06 and 610.57.04, Mesa 26.3.0-rc3, Vulkan-Headers and Vulkan-Loader v1.4.367, open-gpu-doc, RyzenAdj v0.19.0, two WebKit headers; with `NM_TOOLS_FROM=source`, by `deps` and only for the tools it builds: ninja 1.13.2 (v1.13.2, 3441b633c2fe), CMake 4.4.5 (v4.4.5, aa4fae824a02), OpenSSL 3.5.9 (openssl-3.5.9, 45e844fa2a14), zstd 1.5.7 (v1.5.7, f8745da6ff1a), xz 5.8.4 (v5.8.4, d3e650e63c11), CPython 3.14.10 (v3.14.10, b703b2c5e704), pkgconf 2.5.1 (pkgconf-2.5.1, 4fc570f91d9d), Khronos SPIRV-Headers vulkan-sdk-1.4.363.0 (vulkan-sdk-1.4.363.0, 496543121ce6), Khronos SPIRV-Tools vulkan-sdk-1.4.363.0 (vulkan-sdk-1.4.363.0, ef96ed763b43), Khronos glslang 16.6.0 (16.6.0, e1b562a8bed2), LLVM 22.1.8 with clang (llvmorg-22.1.8, ca7933e47d3a). None of those is shipped: NVK's own build has LLVM and SPIRV-Tools turned off, and only the native `mesa_clc`, a build-time program, links them | `<workdir>/src`, and `<workdir>/build/...` in the layout the build scripts already expect (see below) |
| Outputs | `<workdir>/out` (kexts in `out/kexts`, NVK in `out/nvk`, and so on) |
| One log per step, one stamp per step, failure reports | `<workdir>/logs`, `<workdir>/stamps`, `<workdir>/reports` |
| The staged package and its tarball | `<workdir>/package/pkgroot`, `<workdir>/package/nullmoth-nvidia-local-<commit>.tar.gz` |

### What each command writes

| Command | Writes |
|---|---|
| `doctor`, `env`, `help`, and every command with `--dry-run` | Nothing of yours. Each makes one temporary file of its own in your temporary folder (`$TMPDIR`), with a scratch folder beside it for the patch test, and removes them when it ends; `xcrun` keeps its lookup cache (`xcrun_db`) in that same per-user temporary folder, as it does whenever any Xcode tool is run. They read the Mac (`sw_vers`, `sysctl`, `xcode-select -p` before anything else, `xcodebuild -version`, `xcrun`, `file` on each tool taken from `PATH`, each chosen tool's `--version`), the checkout (`git status` without refreshing its index; a dry run never records local changes with `git stash create`) and the working directory. They never run `brew info` or `brew install`: a dry run of `deps` names `llvm@22` without asking Homebrew which LLVM formula it has |
| `deps` | With `NM_TOOLS_FROM=source`: `<workdir>` only, and it calls no `brew` at all. With Homebrew: Homebrew's prefix, and Homebrew's own cache and logs (`~/Library/Caches/Homebrew`, `~/Library/Logs/Homebrew`) for the formulas it installs, after a yes; while it compiles one, its build folder is in `/private/tmp` (Homebrew's `HOMEBREW_TEMP`) and is removed when that formula is done. Its `brew info` questions keep Homebrew's formula list in `<workdir>/tools/homebrew-cache`; with `--offline` or `--no-install` it asks none. Everything else in `<workdir>/tools` and `<workdir>/src`, apart from the temporary files that the compilers, pip and cargo make in your temporary folder (`$TMPDIR`) and remove (cargo keeps its build folder there when an install fails, and says where) |
| `fetch` | `<workdir>/src`, `<workdir>/build/...` (the layout below) and the translator's crates in `<workdir>/tools/cargo`. Its git runs without your git hooks (`core.hooksPath`), file system monitor and global attributes file, and its `git init` and `git clone` without your template folder (`init.templateDir`) |
| `build`, `all`, `package` | `<workdir>` only, with one exception: when the checkout has uncommitted changes, the plugin, translator and video builds take them as a commit object made with `git stash create`, which is stored in the checkout's `.git` (on no branch; `git gc` removes it in time). The steps' scripts run git with no global or system git configuration (`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`), so none of your hooks, file system monitor, signing, filters or URL rewrites act in them; a proxy for their downloads is taken from `https_proxy` (and the other proxy variables) |
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

A working directory used by an older `build.sh` can hold these folders in `<workdir>/build/build/...`. Nothing uses
that folder: `./build.sh clean` (or `clean sources`) deletes it, and the next `fetch` lays the sources out again (the
downloads in `<workdir>/src` are reused). A `NULLMOTH_BUILD_ROOT` you set
yourself is used as it is.

`build.sh` itself writes nothing to your shell start files, `~/.cargo`, `~/.rustup`, `/Library` or `/usr/local` (on an
Intel Mac, `/usr/local` is Homebrew's prefix, so the formulas Homebrew installs with `NM_TOOLS_FROM=homebrew` land there). A `RUSTUP_HOME` or
`CARGO_HOME` from your shell is not used, nor is a Rust you have elsewhere (in `~/.cargo`, for example): `doctor`
reports Rust as missing until `deps` has put the pinned one into the working directory, and leaves yours alone. cargo
still reads the `.cargo/config.toml` of every folder above the one it works in, so with the working directory or the
checkout inside your home folder your own `~/.cargo/config.toml` applies to the translator's fetch and build (its
profile, linker and source settings change what is built); `doctor` names such a file. Nothing is written into the
checkout.

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
| `test` | The pin comparison, the host tests (`tools/run_all_tests.py`) and, once the NVIDIA tree is fetched, the NVRM patch chain against it. Exit 1 when any of them fails. The plugin's tests that compile against Vulkan's headers get `NM_VULKAN_INCLUDE`: yours when you set it, otherwise the fetched Mesa's `include` (the folder the release plugin is compiled against); one line says which, or that there are none until `./build.sh fetch` |
| `env` | Prints `export` lines for every variable `build.sh` gives the build scripts, to run one of them by hand with the same environment |
| `clean [outputs\|sources\|tools\|all]` | Deletes inside the working directory only. `outputs` (the default): outputs, stamps, logs, the package, the RM work tree, and the build trees and exported sources of the tools built from source (not those tools). `sources`: the fetched sources (and the stamps). `tools`: everything under `<workdir>/tools`, the Python environment, Rust and the tools built from source included (and the stamps), without a question: on an Intel Mac the next `deps` builds those tools again, which took 46.5 minutes on the Intel Mac above and 3 hours 53 minutes on GitHub's Intel runner. `all`: everything except the mark, after a yes |
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
| `--workdir DIR` | `NM_WORK` | `~/nullmoth-build` | Where everything `build.sh` makes, downloads and installs goes. Outside the checkout, no spaces, none of `: ; ' " \ $` and no backquote |
| `-j N`, `--jobs N` | `NM_JOBS` | the number of cores | Parallel compile jobs (given to the scripts as `JOBS` and `NVK_JOBS` unless you set those) |
| `-y`, `--yes` | `NM_ASSUME_YES` | `0` | Answer yes to `build.sh`'s questions (installing formulas and Rust, `clean all`). Never to Homebrew's own installer |
| `--no-install` | `NM_NO_INSTALL` | `0` | Install nothing; say what is missing. With `all`, stop after doctor when something is missing |
| `--offline` | `NM_OFFLINE` | `0` | Never use the network: only what an earlier fetch left. NVK cannot be built offline |
| `-n`, `--dry-run` | `NM_DRY_RUN` | `0` | Print every command that would run, including installs and fetches; change nothing |
| `--flavour release\|dev`, `--flavor` | `NM_FLAVOUR` | `release` | `release` builds what a release ships. `dev` builds the plugin with its diagnostics compiled in (`build/build_plugin_dev.sh`), for finding a fault; do not hand it to others |
| `--accel 15\|26\|both` | `NM_ACCEL` | `both` | Which NVAccel.kext builds to make: for macOS 15, for macOS 26, or both. A package needs both |
| `-v`, `--verbose` | `NM_VERBOSE` | `0` | Show each step's output on the terminal as well as in its log |
| `--force` | `NM_FORCE` | `0` | Rebuild the named components (or all) even when they are up to date |
| `--base-package FILE` | `NM_BASE_PACKAGE` | none | The release package the package is made from: `nullmoth-nvidia-2.9.tar.gz` or its unpacked `pkgroot` (the release is `NM_PIN_BASE_RELEASE` in `build/pins.sh`; another release is refused, by doctor at once for an unpacked folder, whose `VERSION` it reads) |
| `--config FILE` | `NM_CONFIG` | `~/.config/nullmoth/build.conf` when it exists | The configuration file |
| `-h`, `--help` | | | The same as `help` |
<!-- end -->

`--jobs=N`, `--workdir=DIR` and the other options with a value also take the `--name=value` form, and `-jN` works.

### Settings without an option

<!-- reference: variables -->
| Variable | Default | Meaning |
|---|---|---|
| `NM_BASE_PACKAGE_SHA256` | none | The base package's SHA-256 from the release page (for release 2.9 it is also `NM_PIN_CI_BASEPKG_SHA256` in build/pins.sh); checked before it is used |
| `NM_PACKAGE_VERSION` | the version of CHANGELOG.md's first heading (`3.0`) | Written to the package's `VERSION`. install.sh keeps only digits and dots and records it in `/Library/NullMoth/driver-version` (so `package` stops on a value with anything else: `3.0rc1` would be recorded as `3.01`), the installed version the app's update check and Send logs read. The base package's own `VERSION` is not kept, so an installed tester build is not recorded as that release (2.9). `package` stops when the heading is not a version and this is not set |
| `NM_OGKM_URL` | the URL in build/pins.sh | A mirror of NVIDIA's open-gpu-kernel-modules (both trees) |
| `NM_MESA_URL` | the URL in build/pins.sh | A mirror of Mesa; any clone that has the pinned commit works |
| `NM_KHRONOS_URL` | the URL in build/pins.sh | A URL prefix that holds `Vulkan-Headers` and `Vulkan-Loader`. Set by you, it is also what the loader build clones from; unset, the loader is built from the verified copies in `<workdir>/src/khronos` |
| `NM_OPEN_GPU_DOC_URL` | the URL in build/pins.sh | A mirror of NVIDIA's open-gpu-doc |
| `NM_RYZENADJ_URL` | the URL in build/pins.sh | A mirror of RyzenAdj |
| `NM_WEBKIT_SPI_URL` | the URL in build/pins.sh | A folder (URL) holding `CMBaseObjectSPI.h` and `VTVideoDecoderSPI.h` |
| `NM_SPIRV_LLVM_URL` | the URL in build/pins.sh | A mirror of SPIRV-LLVM-Translator (deps builds it for LLVM 22) |
| `NM_SPIRV_HEADERS_URL` | the URL in build/pins.sh | A mirror of SPIRV-Headers |
| `NM_TOOLS_FROM` | `auto` | Where the build tools come from: `source` builds them with `build/toolchain.sh` from the pinned sources into `<workdir>/tools/toolchain` and uses only those (or your own settings): a copy Homebrew or `PATH` offers is not used, and no `brew` is called; `homebrew` uses a tool that is there wherever it is and installs Homebrew's formula for a missing one; `auto` is `source` on an Intel Mac (Homebrew has no ready-made builds for it) and `homebrew` on Apple silicon. Any other value stops `build.sh` |
| `NM_TC_NINJA_URL` | the URL in build/pins.sh | A mirror of ninja (v1.13.2), a source deps builds the toolchain from |
| `NM_TC_CMAKE_URL` | the URL in build/pins.sh | A mirror of CMake (v4.4.5), a source deps builds the toolchain from |
| `NM_TC_OPENSSL_URL` | the URL in build/pins.sh | A mirror of OpenSSL (openssl-3.5.9), a source deps builds the toolchain from |
| `NM_TC_ZSTD_URL` | the URL in build/pins.sh | A mirror of zstd (v1.5.7), a source deps builds the toolchain from |
| `NM_TC_XZ_URL` | the URL in build/pins.sh | A mirror of xz (v5.8.4), a source deps builds the toolchain from |
| `NM_TC_PYTHON_URL` | the URL in build/pins.sh | A mirror of CPython (v3.14.10), a source deps builds the toolchain from |
| `NM_TC_PKGCONF_URL` | the URL in build/pins.sh | A mirror of pkgconf (pkgconf-2.5.1), a source deps builds the toolchain from |
| `NM_TC_SPIRVHEADERS_URL` | the URL in build/pins.sh | A mirror of SPIRV-Headers (for the toolchain's SPIRV-Tools) (vulkan-sdk-1.4.363.0), a source deps builds the toolchain from |
| `NM_TC_SPIRVTOOLS_URL` | the URL in build/pins.sh | A mirror of SPIRV-Tools (vulkan-sdk-1.4.363.0), a source deps builds the toolchain from |
| `NM_TC_GLSLANG_URL` | the URL in build/pins.sh | A mirror of glslang (16.6.0), a source deps builds the toolchain from |
| `NM_TC_LLVM_URL` | the URL in build/pins.sh | A mirror of llvm-project (llvmorg-22.1.8), a source deps builds the toolchain from |
| `NM_RUSTUP_URL` | static.rust-lang.org, rustup 1.29.3 | Where `rustup-init` is downloaded from (its SHA-256 is checked either way) |
| `RUSTUP_DIST_SERVER` | rustup's | Where rustup downloads the toolchain from |
| `DEVELOPER_DIR` | the selected Xcode | An Xcode other than the one `xcode-select` selects |
| `NM_BREW` | found | The `brew` to use. When set, it is the only Homebrew considered, whatever its architecture. Unset, `build.sh` uses the Homebrew of the Mac's processor only (`/usr/local` on Intel, `/opt/homebrew` on Apple silicon, also from a shell under Rosetta), and otherwise a `brew` on `PATH` only when it resolves into that same prefix (a wrapper in no Homebrew prefix, or one that resolves into the other architecture's prefix, is not taken). A Homebrew of the other architecture, whose formulas are built for the other processor and cannot be linked here, is named by `doctor` but used only when `NM_BREW` points at it. Every `brew` call runs with `HOMEBREW_PREFIX`, `HOMEBREW_CELLAR`, `HOMEBREW_REPOSITORY`, `HOMEBREW_PATH` and `HOMEBREW_BREW_FILE` cleared and with `PATH` set to that Homebrew's `bin` followed by `/usr/bin:/bin:/usr/sbin:/sbin`, so a login shell's settings for another Homebrew cannot redirect it: Homebrew keeps the caller's `PATH` for a formula's post-install step and some of its builds, where that Homebrew's programs would otherwise come first. A tool taken from `PATH` (`python3`, `patch`, `gpatch`, `ninja`, `cmake`, `pkg-config`, `glslangValidator`) is left out when it belongs to the Homebrew of the other architecture (its file, after its symlinks, is there; or its `#!` interpreter is; or `file` says it is a Mach-O program without this processor's code); `doctor` says which and why |
| `NM_PYTHON` | found | A Python 3.10 or newer to make the virtual environment with |
| `NM_LLVM_PREFIX` | the toolchain's LLVM; with `NM_TOOLS_FROM=homebrew` also Homebrew's `llvm@22` or `llvm` | An LLVM 22.x with its clang libraries, taken before those |
| `NM_SPIRV_TOOLS_PREFIX` | the toolchain's; with `NM_TOOLS_FROM=homebrew` also Homebrew's `spirv-tools` | A SPIRV-Tools 2024.1 or newer that is not Homebrew's: the prefix whose `lib/pkgconfig` holds `SPIRV-Tools.pc` |
| `NM_OGKM620_PRISTINE` | fetched | An unpacked NVIDIA 620.70.06 tree to use instead of fetching (checked by its `version.mk`) |
| `NM_OGKM620` | `<workdir>/build/rm620/ogkm-620.70.06` | A 620.70.06 tree whose RM and modeset archives are already built: the `rm` and `kms` steps are then skipped |
| `NM_OGKM610` | fetched | An unpacked NVIDIA 610.57.04 tree to use instead of fetching |
| `NM_MESA_GIT` | fetched | A Mesa clone that holds commit 90a959eb (mesa-26.3.0-rc3). The release plugin takes its Vulkan headers from that clone's work tree |
| `NM_VULKAN_INCLUDE` | Mesa's `include` | Vulkan headers for the development plugin and for the host tests of `./build.sh test` |
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
| `SDKROOT` | the macOS SDK of the Xcode chosen (`xcrun --sdk macosx --show-sdk-path`), never yours | Also for the SPIRV-LLVM-Translator build and pip. Without it Apple's clang searches `/usr/local/include` and `/usr/local/lib` (Homebrew's on an Intel Mac) before the SDK, so what is in Homebrew could change what is built. Not set where there is no xcrun |
| `PATH` | `<workdir>/tools/host` (only for the release scripts), `<workdir>/tools/bin`, `<workdir>/tools/venv/bin` (Python finds its environment only when started from there), then `/usr/bin:/bin:/usr/sbin:/sbin` |
| `NM_PACKAGE_NAME`, `NM_BUILD_INFO` | the tarball name and the build description `build/package_local.sh` puts in the package |
<!-- end -->

Every step also gets `CC`, `CXX`, `OBJC`, `CFLAGS`, `CXXFLAGS`, `OBJCFLAGS`, `CPPFLAGS`, `LDFLAGS`, `NM_TAHOE`,
your own `SDKROOT` (it may name another platform's SDK; the step gets the macOS one, above) and `TOOLCHAINS` (it
makes xcrun find another toolchain's compiler), the search paths `PKG_CONFIG_PATH` (given back to NVK as above), `PKG_CONFIG_LIBDIR`, `CMAKE_PREFIX_PATH`,
`LIBRARY_PATH`, `CPATH`, `C_INCLUDE_PATH`, `CPLUS_INCLUDE_PATH`, `OBJC_INCLUDE_PATH`, and `HOMEBREW_PREFIX`,
`HOMEBREW_CELLAR`, `HOMEBREW_REPOSITORY` removed from its environment: an exported `CC` breaks build_rm620.sh,
`NM_TAHOE` selects the macOS 26 accelerator, and a login shell may point the others at a Homebrew of the other
architecture. Your `SDKROOT` and `TOOLCHAINS` do not reach `build.sh` itself either: Apple's `/usr/bin/git`, `make`
and `python3` find the real tool through xcrun, and `build.sh` asks those tools questions before its first step. With
an `SDKROOT` that named an SDK which was not there, a build on one Mac stopped there.
doctor names both when they are set; to build with another SDK, choose its Xcode with `DEVELOPER_DIR`. `STEPS` and `NM_REV` (read by build_release_inputs.sh) and `X` and `RELEASE` (build_xlate.sh and
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
| `vulkan` | the Khronos Vulkan loader v1.4.367, which the package offers to applications (`vulkan-for-apps.sh`); not the plugin's own loader | build/build_vulkan_loader.sh | |
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
| `vulkan` | cmake 3.22.1 or later and ninja | `--version`; the toolchain's, and with `NM_TOOLS_FROM=homebrew` also Homebrew's or one on PATH |
| `xlate` | Rust 1.98.1 in the working directory | `deps` installs it |
| `nvk` (and `boost`, built from it) | Python 3.10 or later (Mesa's meson.build) with meson, mako, PyYAML, packaging in `<workdir>/tools/venv`; LLVM 22 with its clang libraries; SPIRV-LLVM-Translator for LLVM 22; SPIRV-Tools; glslang; pkg-config; Rust with bindgen and cbindgen; a `patch` that also creates files (NVK's chain adds files; Homebrew `gpatch` when macOS's cannot) | each one, as `doctor` shows |

`deps` installs `gpatch` only when no `patch` found passes both tests. The oldest Python the kext and video scripts
have been run with is macOS's own `python3` (3.9 with Xcode); nothing in them needs 3.10.

They are built in the order of the table. Groups: `all` (the seventeen components above the optional ones), `kexts`
(rm to nmsmn) and `userland` (airdis to nvencvt). The kext order is fixed: build_rm620.sh copies the pristine NVIDIA
tree, applies the patches in kexts/NVRM/patches and writes the command lines every later kext step reads back;
build_kms620.sh builds in the same tree; NVRM links both archives; NVRMFB and NVAccel compile against the patched tree.

What `build.sh` does around the existing scripts, so that they do not need the original author's machine:

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
- build_rm620.sh and build_kms620.sh end with exit 6 when the archive holds the account name or the computer's short
  name in a form a build leaks it in: a path in the home folder (`$HOME`, `/Users/<account>`), `<account>@`, `@<computer>`
  or `<computer>.local`. As an ordinary word it is only reported (a `note:` line in the log): NVIDIA's own text has
  words such as user, admin and test, which an account may be named, and inside another word (an account "ana" in
  "banana") it is not one at all. They do not print the names, and they give NVIDIA's makefiles neutral values for the build user and host
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
the first; a lock left by a run that is gone is taken over (one whose process number now belongs to another program
too, and one without an owner once it has had none for three seconds: a run that has just made it writes its
owner a moment later).

## Installing the result and going back

`build.sh` never installs anything. A package cannot be made from this repository alone: install.sh refuses a
package without `nullmoth-runtime-check`, and the plugin bundle's Info.plist, `nvmtl-allow.txt`, `air-runtime`, the
video libraries, the plugin's own Vulkan loader (1.4.362) and a few configuration files have no source here
([PROVENANCE.md](PROVENANCE.md)). The release itself starts each package from the previous one. So
`./build.sh package` does the same: with a release package as the base (`--base-package`, a
`nullmoth-nvidia-2.9.tar.gz` from Releases or its unpacked `pkgroot`), `build/package_local.sh` checks the base
against its own SHA256SUMS (and against `NM_BASE_PACKAGE_SHA256` when set), refuses a base that is not release 2.9
(the release this tree is staged onto) or that holds anything but folders, plain files and links to a file beside
them (a link out of its folder would carry the step's writes with it), replaces every part this checkout builds
(the kexts, NVK, the translator, the plugin binary, the scripts, the DLSS tools, the licences), refreshes their
SHA256SUMS lines, writes `VERSION` (`NM_PACKAGE_VERSION`: by default the release this checkout is, from CHANGELOG.md),
adds `NULLMOTH-LOCAL-BUILD.txt` (commit, local changes, pins), runs `package/finish_package.sh`
like a release, then makes the check install.sh makes first (`shasum -a 256 -c SHA256SUMS`). install.sh checks a
package's integrity, not who built it, so it accepts this one. The tarball's headers name `root` and `wheel`, not the
account that built it, and it carries none of your Mac's extended attributes (the quarantine mark of a downloaded
base names your browser), so it can be passed on. The ryzenadj built here has another SHA-256 than the
one the 1401 app pins, so the app will not install it; the driver is not affected.

No package made by `./build.sh package` has been installed on any Mac, and nothing built from this branch has been
loaded or run on an NVIDIA card: whoever installs one first is testing it. The sizes of Apple's IOKit classes on
macOS 26.7 have not been measured either; the kexts compare them with their own when they start (the layout check).

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

Installing a tester build over a release leaves the same fallback job a release install leaves: if this build's
framebuffer refuses the running macOS's Apple classes so that the driver shows nothing on the card, the job puts the
release back and restarts the Mac by itself during the first few minutes of a start (see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md), the layout-refusal entry). A Mac that restarts by itself onto the release
after you installed a tester build has met that case, also one whose OpenCore does not exclude IONDRVSupport: there
Apple's firmware framebuffer can show a slow desktop without acceleration on the card for a few seconds first, and the
Mac restarts even when it has logged you in by itself. Only a display on another GPU with a user logged in leaves the
restart to you (`/Library/NullMoth/lastgood.log` says so); the next restart brings the release.

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
| Byte-identical results | The release's Xcode version is recorded nowhere, so a build here cannot be compared with the release. What has been compared: on the Intel Mac named at the top, NVK's library had the same bytes in three builds (one with tools built by hand, then at commits 650db45 and 3933ff6), and NVRM.kext's binary and the plugin had the same bytes at those two commits (the plugin with the 26.5 SDK, which leaves out its part for older SDKs); at commit 95811c2 the kexts built on GitHub's Intel and Apple-silicon runners had the same bytes. Not compared: the other kexts, the translator (its rustc options differ between those two commits), the video library, and anything between that Mac and GitHub's runners. The Vulkan loader's CMake compiles a configuration folder into the library, and build_vulkan_loader.sh sets it to `/etc` (`-DSYSCONFDIR=/etc`) instead of its temporary install folder: with that option two builds on one Intel Mac had the same bytes. {{PENDING 5c}} The compile commands of NVIDIA's RM and NVKMS are the same on an Intel and on an Apple-silicon Mac: NVIDIA's makefiles ask the build Mac's compiler which x86-only hardening options it takes, and `build/nv_compile_lines.py` leaves out whatever that adds, so an Intel Mac compiles with the release's commands (its build log names what was left out) |

## Troubleshooting

| Message or symptom | Cause | What to do |
|---|---|---|
| `STOP: the working directory contains a space` (or the checkout path, or the home folder) | The release scripts' remote commands and build_nvk.sh's compiler flags split on spaces | Use `--workdir` with a path without spaces; move the checkout |
| `STOP: the working directory contains one of : ; ' " \ $` (or a backquote) | `PATH` and the build tools' search paths split on a colon (CMake's on a semicolon), and pkg-config reads quotes and backslashes in its paths as quoting | Use `--workdir` with a path without them |
| `STOP: this Mac runs macOS 14...` | Xcode 26 needs macOS 15.6 or later | Update macOS, or build on another Mac |
| doctor: `MISSING full Xcode 26.0 or later: only the command line tools are selected` | `xcode-select` points at the command line tools | Install Xcode 26, then `sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer` |
| `STOP: the developer folder xcode-select -p names, ..., does not exist` (or `holds none of Apple's tools`, or `DEVELOPER_DIR names`) | The selected Xcode was removed or renamed, `DEVELOPER_DIR` names a wrong folder, or a macOS update left the command line tools incomplete | `sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer` with the Xcode you have, or correct or unset `DEVELOPER_DIR` |
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
| rm or kms ends with exit 6 | build_rm620.sh and build_kms620.sh stop when NVIDIA's archive holds your home folder's path, your account name followed by `@`, or `@` and the computer's short name: a build leaked them | The log names the command that shows the lines. Report it with the report file (the lines name you: read them before you post them) |
| `STOP: N compile commands for arch/nvalloc/unix/src/osapi.c in .../compile_cmds.sh, one expected` (nvrm, accel) | The RM's compile list is incomplete; the kexts take their -D flags from NVIDIA's own command for osapi.c and stop rather than take another file's | `./build.sh --force build rm` (build.sh rebuilds rm in a fresh folder and then everything built from it); by hand, run build_rm620.sh in a new work folder |
| `STOP: make -n listed N compile commands, fewer than ...` | build_rm620.sh or build_kms620.sh would have replaced a complete list with a shorter one; it kept the old one | Use a new work folder |
| `STOP: NVRM.kext imports ___x86_return_thunk, which no macOS defines` (or another kext, or another `___x86_`, `___fentry__` or `mcount` name; the step ends with exit 7) | The kext was compiled with an option made for the Linux kernel, whose helper no macOS has: it would link and never load. It stops before anything is packaged | With `KMS_ARCHIVE` set, it names an NVKMS archive built with other commands: unset it. Otherwise build again in a new work folder (`--workdir`); if it stays, report it with the report file |
| `build_rm620.sh reported compile failures` | A compile of NVIDIA's RM failed; build_rm620.sh itself would not stop | Read the `failed:` lines in `logs/rm.log` |
| a step stops at a warning ceiling (`warn_ceiling`) | The ceilings in build/warnings.txt are 0: a compiler or linker other than the release's may print a new warning | Report it with the report file and your Xcode version |
| `libvulkan_nouveau.dylib loads a library from outside macOS` | A library from outside macOS (Homebrew's, for one) was linked | Report it with the report file |
| `STOP: step 'toolchain-<tool>' failed` (deps, `NM_TOOLS_FROM=source`) | `build/toolchain.sh` could not build or check that tool; its log is `<workdir>/logs/toolchain/<tool>.log` | Read the log's last lines; run `./build.sh deps` again after a fix: it starts at that tool, the ones before it are kept |
| `... links <library>, which is neither the system's nor the toolchain's`, or `... is a link to <path>, which is neither ...` (in a toolchain log) | A tool picked up a library from another folder, usually Homebrew's `/usr/local` on an Intel Mac, or installed a link that leads there; the stamp is not written, so the tool is not used | Report it with the report file; the library named is what leaked |
| `... is built for macOS N and later, above MACOSX_DEPLOYMENT_TARGET` (in a toolchain log) | A tool was built for a newer macOS than the build's oldest (15.0), so NVK's link would warn | Report it with the report file |
| deps was interrupted while LLVM built (Ctrl-C, sleep, power) | The half-built tree is not resumed | Run `./build.sh deps` again: LLVM is built again from an empty build tree (about 30 minutes on the Intel Mac named at the top); keep the Mac awake |
| `deps` prints rustup's warning `default toolchain implicitly overridden` twice | rustup prints it while `deps` installs the pinned Rust; a good run on the Intel Mac named at the top printed it too | Nothing: it is expected |
| `building LLVM needs about 10 GB free` | Not enough free space where the working directory is | Make room, or choose a working directory on another disk with `--workdir` |
| `the base package does not match its own SHA256SUMS` | The release package is damaged or changed | Download it again |
| `the base package is release <x>, not 2.9` | `package` stages onto release 2.9 only: another release's files that have no source here would ship under this build's version | Use `nullmoth-nvidia-2.9.tar.gz` from Releases |
| `the base package holds a link that leaves its folder` (or `a device, pipe or socket`, `a set-user-id or set-group-id file`) | The file is not a release package: a release holds folders, plain files and links to a file beside them | Download the release package again from Releases; do not use the file |
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

`tools/test_build_sh.py` runs the real `build.sh` in a sandbox that imitates a Mac, on Linux and on GitHub's Intel
runner: stand-ins for
`sw_vers`, `uname`, `xcode-select`, `xcodebuild`, `xcrun`, `brew`, `rustup`, `cargo`, `python3` and the other tools,
real git against local repositories that stand in for the remotes, and a scratch copy of this tree in which every
component build script is replaced by a stand-in that records what it receives. It checks doctor on a Mac with
nothing installed, deps with and without installing, when deps unlinks a Python formula again and when it does not, fetch (and a tampered remote), offline, dry run, the build
order, the variables every step gets, skipping and rebuilding, a failing step and its report, a failed script after
an earlier good build, build_rm620.sh's exit 6, the step's python3, the Vulkan headers `test` gives the host tests, that the scripts' `patch` never waits for an answer at the terminal, SPIRV-LLVM-Translator when Homebrew's LLVM is 23,
which of two Homebrews is used, that no tool of the other architecture's Homebrew is taken from `PATH`, that no brew
call keeps the caller's `HOMEBREW_` location variables or `PATH`, that `fetch` runs none of your git hooks, template or filters, that
nothing opens Apple's install dialog on a Mac without Xcode or runs with a developer folder that is gone, what a Mac with Xcode alone builds and what stops before the first step, that doctor,
dry runs and env write nothing (Homebrew's cache included) and print no home path,
an interrupted run,
two runs at once, paths with spaces, Apple silicon and an unsupported macOS, `env`, this document against the script,
and the pins against the scripts that use them; each part has at least one control that breaks `build.sh` (or its
input) on purpose and shows the check then fails. It runs `build.sh` under `/bin/bash`, or the bash named by
`BUILD_SH_BASH`, and says so when that is not bash 3.2, the `/bin/bash` of macOS; it has passed under GNU bash
3.2.48 and 5.2 on Linux, and under macOS's `/bin/bash` on GitHub's Intel runner at commit 3933ff6 (it has not run on
the Intel Mac at that commit). {{PENDING 5b}} `tools/test_rm_compile_list.py` runs the real build_rm620.sh,
build_kms620.sh and accel_build.sh on a small stand-in NVIDIA tree (real make and patch): a second run in a built
folder keeps the compile list complete and the kexts' -D flags the same, and the personal-string check stops at a home path or `user@host` and only reports an ordinary word. What these tests cannot show is how a real Xcode, Homebrew and compiler behave: the whole builds on the two Macs
named at the top have shown it for Xcode 26.3 and 26.5, and no run on a Mac has used Homebrew.

In addition, every push is built in an empty working directory on GitHub's Intel runner (no NVIDIA card, nothing
installed or loaded), with the tools built from source restored from GitHub's cache once a run has made them (from
nothing, `deps` took 3 hours 53 minutes there): `.github/workflows/full-build.yml` runs deps, doctor, fetch, build,
package and test, then checks that Homebrew was neither used nor changed and that everything built loads only from macOS, that folder and
the driver's own folders. Its result is under the repository's Actions tab, workflow full-build; when it fails or runs
out of time, its last step prints the logs. At commit 3933ff6 every step up to `package` succeeded; the result of its
`test` step and of its checks is not stated here. {{PENDING full-test}} {{PENDING full-prove}} Until it has passed once,
its result does not hold up a change.
