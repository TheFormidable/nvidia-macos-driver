# DLSS 4 / 4.5 support files

The driver ships no NVIDIA code for DLSS. The installer downloads NVIDIA's own files from NVIDIA's public sources on
the user's Mac, checks their hashes, and builds what NVK needs from them locally.

Since 3.0.13 `install.sh` does this by default (`package/dlss_fetch.sh --yes`: about 750 MB the first time, nothing
when the same versions are already in place); `sudo env NULLMOTH_DLSS=0 ./install.sh` skips the step, and a failure in
it leaves the driver installed. Through 3.0.12 the step ran only with `NULLMOTH_DLSS=1`. From 3.0.13 to 3.0.18 this
fork's own packages skipped it all the same, because `install.sh` did not recognise the way their `SHA256SUMS` names
the files (`./dlss_fetch.sh`) and printed "this package's SHA256SUMS does not cover the DLSS step"; 3.0.19 fixed that.
With the files under `/Library/NullMoth/dlss` the Metal plugin serves the MetalFX temporal upscaler with DLSS Super
Resolution and the MetalFX frame interpolator with DLSS Frame Generation without any switch (WindowServer never loads
them); `uninstall.sh` removes the folder.

What `dlss_fetch.sh` downloads, each file checked against the SHA-256 pinned in the script: the DLSS Super
Resolution, Ray Reconstruction and Frame Generation runtimes 310.9.16 from NVIDIA's DLSS repository, the NGX core from
NVIDIA's Linux 620.70.06 driver package (only that one file is kept), and nvJitLink 12.9.86. They stay under NVIDIA's
licences (DLSS SDK licence, driver licence, CUDA EULA).

## What is in this folder

- `elfhost.c`, `lnxlibc.c` (and their headers): load an x86-64 Linux shared object (NVIDIA's DLSS runtimes, NGX core
  and nvJitLink) into a macOS process, written from the ELF and System V x86-64 ABI specifications. `lnxlibc.c`
  translates the glibc entry points whose ABI differs from macOS (structure layouts, flag values, thread objects, TLS).
  The Metal plugin builds its own copy from `plugin/dlss/` (the same `elfhost.c`, a `lnxlibc.c` of its own) to run the
  NGX core in an application's process (`plugin/nvmtl_dlss.c`).
- `dlss_cache.c`: at install time, compiles each DLSS fatbin's PTX for the user's GPU with NVIDIA's nvJitLink and
  `assertfail_trap.ptx`, into `/Library/NullMoth/dlss/kernels/<BLAKE3 of the PTX object>.sm<N>.cubin`. NVK reads an
  entry only when the fatbin has no code it can load itself (RTX 50 cards: DLSS ships sm_89 SASS and PTX).
- `assertfail_trap.ptx`: the device-side `__assertfail` the DLSS 4 transformer kernels import; it traps.
- `nm_unzstd.c`: a Zstandard decoder (standard input to standard output) that `dlss_fetch.sh` uses to unpack NVIDIA's
  driver package, which macOS cannot decompress itself.
- `zstd/`: the Zstandard 1.5.6 reference decoder (BSD-3-Clause, unmodified; its `README` names the release tarball's
  SHA-256), used only by `nm_unzstd.c`.
- `blake3/`: BLAKE3 1.8.2 reference C (CC0-1.0 or Apache-2.0), the same hash NVK uses for the cache name.
- `nvk_decide.c`: offline check of NVK's module decision (`nvk/dlss/patch_dlss_cache.py`) over captured fatbins.
- `dlss_fg.c`, `dlss_rr.c`: test programs, not packaged. Each runs DLSS Frame Generation or Ray Reconstruction on the
  real GPU through NVK and NVIDIA's NGX core (loaded with `elfhost`) on synthetic frames, and judges the read-back
  output against a reference, with controls (usage and environment variables in each file's header).

## Build

`build/build_dlss_cache.sh <out dir>` builds `dlss_cache` and `nm_unzstd` for x86_64 (macOS 12.0 and later), signs
both ad hoc, and puts them with `assertfail_trap.ptx`, the Zstandard licence and `dlss_fetch.sh` where the package
expects them (`./build.sh` runs it as its `dlss` component). Build on a Mac with the Xcode command line tools.
