# shellcheck disable=SC2034  # read by build.sh as data, never sourced
# build/pins.sh: every outside source and tool version the one-command build (./build.sh) uses, in one place.
# Read by build.sh (it only takes NM_PIN_*=value lines; nothing here runs). Each number is the one the repository's
# own scripts, patches and documents require; the comment above it says where that comes from. build.sh checks at
# start that the pins and the scripts that consume them agree ("./build.sh doctor" prints the comparison), and stops
# when they do not: change both together.
#
# A source has: NAME (what it is), URL (primary), MIRROR (the variable that replaces the URL, e.g. a local mirror or
# a file:// path), REF (tag; empty when only a commit is pinned), COMMIT (verified after the fetch: a mismatch stops
# the build) or SHA256 (for a single file).

NM_PIN_SOURCES="OGKM620 OGKM610 MESA VKHEADERS VKLOADER OPENGPUDOC RYZENADJ WEBKIT_CMBASE WEBKIT_VTDEC SPIRVLLVM SPIRVHEADERS"

# NVIDIA open-gpu-kernel-modules 620.70.06, the RM core and NVKMS the kexts link. build/build_rm620.sh and
# build/build_kms620.sh (their NV_VERSION default, "V=${NV_VERSION:-620.70.06}"), README.md (Build from source),
# NOTICE. The commit is the tag's peeled commit on github.com (git ls-remote, 2026-11-12), the same one
# build/kext_check.sh pins as OGKM_SHA.
NM_PIN_OGKM620_NAME="NVIDIA open-gpu-kernel-modules 620.70.06 (pristine; the kexts are built from it)"
NM_PIN_OGKM620_URL=https://github.com/NVIDIA/open-gpu-kernel-modules.git
NM_PIN_OGKM620_MIRROR=NM_OGKM_URL
NM_PIN_OGKM620_REF=620.70.06
NM_PIN_OGKM620_COMMIT=e6a2e01579fbbd1ca2ed43fef2ee2589e9bd8bd9

# open-gpu-kernel-modules 610.57.04, the headers NVK and libnvencvt are built against. build/build_nvk.sh (its usage
# line, argument 2), build/build_nvencvt.sh (its "fetch ogkm" line), vendor/nvenc/SOURCE ("Build inputs"). Peeled
# commit of the tag on github.com.
NM_PIN_OGKM610_NAME="NVIDIA open-gpu-kernel-modules 610.57.04 (headers for NVK and libnvencvt)"
NM_PIN_OGKM610_URL=https://github.com/NVIDIA/open-gpu-kernel-modules.git
NM_PIN_OGKM610_MIRROR=NM_OGKM_URL
NM_PIN_OGKM610_REF=610.57.04
NM_PIN_OGKM610_COMMIT=e4a5faa2567f28c8eabe0ebb6422b6d0abcf37eb

# Mesa mesa-26.3.0-rc3: nvk/MESA_BASE holds its first 8 digits, which build/build_nvk.sh reads (BASE=$(cat
# "$NVK/MESA_BASE"); its 2.9 case keeps release 2.9's commit whatever this pin becomes); NOTICE names
# gitlab.freedesktop.org. Any clone with the commit works (for example a GitHub mirror through NM_MESA_URL).
NM_PIN_MESA_NAME="Mesa 26.3.0-rc3 (NVK's base; its include/ is the plugin's Vulkan headers)"
NM_PIN_MESA_URL=https://gitlab.freedesktop.org/mesa/mesa.git
NM_PIN_MESA_MIRROR=NM_MESA_URL
NM_PIN_MESA_REF=mesa-26.3.0-rc3
NM_PIN_MESA_COMMIT=90a959ebba9dbb36f2ea5176091c15d6ebdd0af0
# Not fetched by build.sh: the Vulkan headers of the automatic checks (build/ci_vulkan_headers.sh, used by
# build/ci_plugin.sh and the host tests on GitHub's runners). Khronos Vulkan-Headers v1.4.363 (the tag's peeled commit,
# git ls-remote 2026-11-13): each of its C headers in include/vulkan and include/vk_video is byte for byte the file of
# the same name in Mesa's include/ at the commit on the second line (34 files compared, git blob ids equal), and Mesa
# has two more there, vk_android_native_buffer.h and .editorconfig. VK_HEADER_VERSION is 363 in both. When the Mesa
# pin above moves, ci_vulkan_headers.sh stops until the two are compared again (its --compare) and both lines move.
NM_PIN_CI_VKHEADERS_COMMIT=6802bb4733b63ed5efd3adb308a6c885ef180ea1
NM_PIN_CI_VKHEADERS_MESA=90a959ebba9dbb36f2ea5176091c15d6ebdd0af0
# Not fetched by build.sh: the base package of the automatic full build (build/ci_full_build.sh basepkg, run by
# .github/workflows/full-build.yml), which gives it to "./build.sh package" with NM_BASE_PACKAGE_SHA256. The release
# package 2.9 as published on the original project's release page; size and SHA-256 of the file downloaded 2026-11-13.
NM_PIN_CI_BASEPKG_URL=https://github.com/nullmoth/nvidia-macos-driver/releases/download/v2.9/nullmoth-nvidia-2.9.tar.gz
NM_PIN_CI_BASEPKG_SIZE=68833656
NM_PIN_CI_BASEPKG_SHA256=b624bf07c78cea03043e096383d5c8b01e4fb2c804a6ba377a4e66a2566422ef

# Khronos Vulkan-Headers and Vulkan-Loader v1.4.367: build/build_vulkan_loader.sh (TAG=, LOADER_SHA=, HEADERS_SHA=),
# NOTICE. The mirror variable is a base URL (".../KhronosGroup"); build_vulkan_loader.sh reads the same variable.
NM_PIN_VKHEADERS_NAME="Khronos Vulkan-Headers v1.4.367 (for the Vulkan loader)"
NM_PIN_VKHEADERS_URL=https://github.com/KhronosGroup/Vulkan-Headers
NM_PIN_VKHEADERS_MIRROR=NM_KHRONOS_URL
NM_PIN_VKHEADERS_REF=v1.4.367
NM_PIN_VKHEADERS_COMMIT=9fbc2a782ee3f8a4db64e7d991e23da75e893f44
NM_PIN_VKLOADER_NAME="Khronos Vulkan-Loader v1.4.367"
NM_PIN_VKLOADER_URL=https://github.com/KhronosGroup/Vulkan-Loader
NM_PIN_VKLOADER_MIRROR=NM_KHRONOS_URL
NM_PIN_VKLOADER_REF=v1.4.367
NM_PIN_VKLOADER_COMMIT=aa6dee8385afe4b2cc0152d086af2184743e16fd

# NVIDIA open-gpu-doc (video class headers of libnvencvt): build/build_nvencvt.sh ("fetch ogd"), vendor/nvenc/SOURCE.
# Commit only.
NM_PIN_OPENGPUDOC_NAME="NVIDIA open-gpu-doc (video class headers for libnvencvt)"
NM_PIN_OPENGPUDOC_URL=https://github.com/NVIDIA/open-gpu-doc.git
NM_PIN_OPENGPUDOC_MIRROR=NM_OPEN_GPU_DOC_URL
NM_PIN_OPENGPUDOC_REF=
NM_PIN_OPENGPUDOC_COMMIT=9fdf5c4062007929d9f4e6cbad9c9771fe61b880

# RyzenAdj v0.19.0, built into the package's ryzenadj: tools/ryzenadj-macos/build.sh (its TAG= and COMMIT=
# lines; it refuses any other HEAD), NOTICE.
NM_PIN_RYZENADJ_NAME="RyzenAdj v0.19.0 (the package's ryzenadj)"
NM_PIN_RYZENADJ_URL=https://github.com/FlyGoat/RyzenAdj.git
NM_PIN_RYZENADJ_MIRROR=NM_RYZENADJ_URL
NM_PIN_RYZENADJ_REF=v0.19.0
NM_PIN_RYZENADJ_COMMIT=ea71739b4a3e1a0a624dfd1c9c268a31cb2a4182

# Two WebKit SPI headers for libnvencvt at WebKit commit 3bfbc200: build/build_nvencvt.sh (its "webkit-spi" curl
# line), vendor/nvenc/SOURCE.
# build_nvencvt.sh fetches them with curl and checks nothing; these SHA-256 were taken from that URL on 2026-11-12.
NM_PIN_WEBKIT_COMMIT=3bfbc200370710495ffa440abdd31b943c83ad7a
NM_PIN_WEBKIT_CMBASE_NAME="WebKit CMBaseObjectSPI.h (libnvencvt)"
NM_PIN_WEBKIT_CMBASE_URL=https://raw.githubusercontent.com/WebKit/WebKit/3bfbc200370710495ffa440abdd31b943c83ad7a/Source/ThirdParty/libwebrtc/Source/webrtc/webkit_sdk/WebKit/CMBaseObjectSPI.h
NM_PIN_WEBKIT_CMBASE_MIRROR=NM_WEBKIT_SPI_URL
NM_PIN_WEBKIT_CMBASE_REF=
NM_PIN_WEBKIT_CMBASE_SHA256=2e30b39dadff06145a1dfc29f735b9067eb99d08151a7fe8b9464803df784a62
NM_PIN_WEBKIT_VTDEC_NAME="WebKit VTVideoDecoderSPI.h (libnvencvt)"
NM_PIN_WEBKIT_VTDEC_URL=https://raw.githubusercontent.com/WebKit/WebKit/3bfbc200370710495ffa440abdd31b943c83ad7a/Source/ThirdParty/libwebrtc/Source/webrtc/webkit_sdk/WebKit/VTVideoDecoderSPI.h
NM_PIN_WEBKIT_VTDEC_MIRROR=NM_WEBKIT_SPI_URL
NM_PIN_WEBKIT_VTDEC_REF=
NM_PIN_WEBKIT_VTDEC_SHA256=3d3f58450ba71ec689a50dfab37d3d84e3d7e686c20b01d5892300866030092f

# SPIRV-LLVM-Translator for LLVM 22, which Mesa's native mesa_clc links (Mesa meson.build:2066-2082: LLVMSPIRVLib of the
# same major.minor as the LLVM it uses). Homebrew has no versioned formula of it: homebrew-core's spirv-llvm-translator
# (23.1.3 on 2026-11-12) depends on "llvm", which is 23.1.5 there, while llvm@22 is 22.1.8. So "./build.sh deps"
# builds v22.1.8, the newest v22 tag, with the Homebrew formula's own cmake recipe into <workdir>/tools. Its .pc says
# 22.1.0.0 (CMakeLists.txt: BASE_LLVM_VERSION 22.1.0). The tag is lightweight; the commit is from git ls-remote.
NM_PIN_SPIRVLLVM_NAME="Khronos SPIRV-LLVM-Translator v22.1.8 (for LLVM 22; built by deps)"
NM_PIN_SPIRVLLVM_URL=https://github.com/KhronosGroup/SPIRV-LLVM-Translator.git
NM_PIN_SPIRVLLVM_MIRROR=NM_SPIRV_LLVM_URL
NM_PIN_SPIRVLLVM_REF=v22.1.8
NM_PIN_SPIRVLLVM_COMMIT=a2c6155836cfb24e4d6a3fe704fb60122422e5b4
# SPIRV-Headers at the commit v22.1.8's spirv-headers-tag.conf names (build.sh compares the two after the fetch).
NM_PIN_SPIRVHEADERS_NAME="Khronos SPIRV-Headers (for SPIRV-LLVM-Translator)"
NM_PIN_SPIRVHEADERS_URL=https://github.com/KhronosGroup/SPIRV-Headers.git
NM_PIN_SPIRVHEADERS_MIRROR=NM_SPIRV_HEADERS_URL
NM_PIN_SPIRVHEADERS_REF=
NM_PIN_SPIRVHEADERS_COMMIT=cb42dec3830d3ac67fa449ecdc0c0f73d5e74498

# Not fetched by build.sh, listed so that every download of a build is named somewhere:
#   Mesa's six Rust crates for NAK (paste 1.0.14, proc-macro2 1.0.86, quote 1.0.35, rustc-hash 2.1.1, syn 2.0.87,
#   unicode-ident 1.0.12): meson downloads them from static.crates.io during every NVK build and checks each against
#   the source_hash in Mesa's subprojects/*.wrap; licenses/nvk-rust-crates.txt must match them (build_nvk.sh, the
#   cmp against that file).
#   The translator's crates: cargo checks each against translator/wrapper/Cargo.lock; "./build.sh fetch" runs
#   "cargo fetch --locked" so that the build itself can run offline.

# Rust. The release translator is built with rustc 1.98.1 (build/build_xlate_release.sh, its NULLMOTH_RUST_VERSION
# default; "another version builds the same source into different bytes"); the same toolchain builds NVK's Rust
# (Mesa meson.build:950 needs 1.85) and translator/translator/Cargo.toml says rust-version 1.87.
NM_PIN_RUST_VERSION=1.98.1
# rustup-init 1.29.3, the official installer, from static.rust-lang.org/rustup/archive (SHA-256 from the .sha256 file
# published beside it, 2026-11-12). It is run with --no-modify-path, RUSTUP_HOME and CARGO_HOME inside the workdir.
NM_PIN_RUSTUP_VERSION=1.29.3
NM_PIN_RUSTUP_URL=https://static.rust-lang.org/rustup/archive/1.29.3
NM_PIN_RUSTUP_MIRROR=NM_RUSTUP_URL
NM_PIN_RUSTUP_SHA256_X86_64=bc107f1fb0228de7fa32413c89c1b305cea9cd256e94b01150cd054128145429
NM_PIN_RUSTUP_SHA256_AARCH64=63752a1919a3a4793173ec70f8f212621a7ce38ba6a246095984482f4de20f9f
# bindgen and cbindgen for NAK, installed with "cargo install --locked" (crates.io checksums). Mesa meson.build:955-970:
# bindgen 0.71.1 or newer, not 0.72.0, 0.73.0, 0.73.1; src/nouveau/nil/meson.build: cbindgen 0.28 or newer. 0.73.2
# and 0.29.4 meet those bounds.
NM_PIN_BINDGEN_VERSION=0.73.2
NM_PIN_BINDGEN_BAD="0.72.0 0.73.0 0.73.1"
NM_PIN_CBINDGEN_VERSION=0.29.4

# Python packages for Mesa's build, installed into the workdir's virtual environment with pip --require-hashes.
# Mesa meson.build:1160-1209 needs mako, PyYAML and packaging; meson.build:940-942 needs meson 1.7.0 or newer when
# Rust is used (NVK); 1.12.1 meets that.
# Hashes from pypi.org (2026-11-12): the source archive and the macOS wheels for CPython 3.10 to 3.14.
NM_PIN_PY_PACKAGES="MESON MAKO MARKUPSAFE PYYAML PACKAGING"
NM_PIN_PY_MESON="meson==1.12.1"
NM_PIN_PY_MESON_HASHES="930bc7542cbd9f57009e182fd014eba48cf1a0180a7b9006d2d32d8f168d8b02 ab0a6ca09f8ef70c564c8241fb5a23957886a0b53fb58412b5e07eaf07dba743"
NM_PIN_PY_MAKO="mako==1.4.3"
NM_PIN_PY_MAKO_HASHES="723296007c870bfd6b3f0c3230dba7198096e5269297ebf5e4eff9e7ffa39d4f cd6537fe88d5fec315c55c2f8529bc4ce7a9a352ad7db3eeaa6a66e2dd4ec37a"
NM_PIN_PY_MARKUPSAFE="markupsafe==3.0.3"
NM_PIN_PY_MARKUPSAFE_HASHES="722695808f4b6457b320fdc131280796bdceb04ab50fe1795cd540799ebe1698 2f981d352f04553a7171b8e44369f2af4055f888dfb147d55e42d29e29e74559 e1c1493fb6e50ab01d20a22826e57520f1284df32f2d8601fdd90b6304601419 1cc7ea17a6824959616c525620e387f6dd30fec8cb44f649e31712db02123dad 4bd4cd07944443f5a265608cc6aab442e4f74dff8088b0dfc8238647b8f6ae9a d53197da72cc091b024dd97249dfc7794d6a56530370992a5e1a08983ad9230e 1872df69a4de6aead3491198eaf13810b565bdbeec3ae2dc8780f14458ec73ce e1cf1972137e83c5d4c136c43ced9ac51d0e124706ee1c8aa8532c1287fa8795 116bb52f642a37c115f517494ea5feb03889e04df47eeff5b130b1808ce7c219 eaa9599de571d72e2daf60164784109f19978b327a3910d3e9de8c97b5b70cfe c47a551199eb8eb2121d4f0f15ae0f923d31350ab9280078d1e5f12b249e0026"
NM_PIN_PY_PYYAML="pyyaml==6.0.3"
NM_PIN_PY_PYYAML_HASHES="d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f 214ed4befebe12df36bcc8bc2b64b396ca31be9304b8f59e25c11cf94a4c033b 02ea2dfa234451bbb8772601d7b8e426c2bfa197136796224e50e35a78777956 44edc647873928551a01e7a563d7452ccdebee747728c1080d881d68af7b997e 652cb6edd41e718550aad172851962662ff2681490a8a711af6a4d288dd96824 7f047e29dcae44602496db43be01ad42fc6f1cc0d8cd6c83d342306c32270196 fc09d0aa354569bc501d4e787133afc08552722d3ab34836a80547331bb5d4a0 8da9669d359f02c0b91ccc01cac4a67f16afec0dac22c2ad09f46bee0697eba8 2283a07e2c21a2aa78d9c4442724ec1eb15f5e42a723b99cb3d822d48f5f7ad1 8d1fab6bb153a416f9aeb4b8763bc0f22a5586065f86f7664fc23339fc1c1fac 34d5fcd24b8445fadc33f9cf348c1047101756fd760b4dacb5c3e99755703310"
NM_PIN_PY_PACKAGING="packaging==25.0"
NM_PIN_PY_PACKAGING_HASHES="29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484 d443872c98d677bf60f6a1f2f8c1cb748e8fe762d2bf9d3148b5599295b0fc4f"

# Minimum versions of the tools the build takes from the Mac (checked by "./build.sh doctor").
# Xcode 26: plugin/NVMTLMetal4.m:17 imports <Metal/MTL4CommandQueue.h> without a guard, which only the macOS 26 SDK
# has (README.md's "Xcode 16" predates it). Every other part builds with it for macOS 15.0.
NM_PIN_TOOL_XCODE_MIN=26.0
NM_PIN_TOOL_SDK_MIN=26.0
# Xcode 26 runs on macOS 15.6 or later (Apple's Xcode 26 release notes).
NM_PIN_TOOL_MACOS_MIN=15.6
# GNU make: open-gpu-kernel-modules utils.mk:210-218 needs 3.80 (macOS ships 3.81). Only "make -n" runs.
NM_PIN_TOOL_MAKE_MIN=3.80
# Python: Mesa meson.build:1155 ">= 3.10". tools/test_fw_fetch.py needs 3.14 (compression.zstd), so 3.14 is installed.
NM_PIN_TOOL_PYTHON_MIN=3.10
# Python for the kexts and libnvencvt (build_rm620.sh, build_kms620.sh, kexts/NVRM/rmcc.py, tools/nvencvt_header_abi.py):
# their newest needs are f-strings (3.6) and subprocess.run(capture_output=, text=) (3.7); 3.8 is the oldest they were
# run with. macOS's own python3 (3.9 with Xcode) is enough; only NVK needs 3.10.
NM_PIN_TOOL_PYTHON_KEXT_MIN=3.8
NM_PIN_TOOL_PYTHON_TESTS_MIN=3.14
NM_PIN_TOOL_PYTHON_FORMULA=python@3.14
# meson: Mesa meson.build:940-942 (1.7.0 with Rust). ninja: meson's own minimum (1.8.2).
NM_PIN_TOOL_MESON_MIN=1.7.0
NM_PIN_TOOL_NINJA_MIN=1.8.2
# cmake: cmake_minimum_required(VERSION 3.22.1) in Vulkan-Loader and Vulkan-Headers v1.4.367 CMakeLists.txt.
NM_PIN_TOOL_CMAKE_MIN=3.22.1
# LLVM 22 for the native mesa_clc/vtn_bindgen2 build: build/build_nvk.sh (its NVK_PATH comment: "LLVM 22.1.x");
# nvk-macos.patch includes <clang/Options/OptionUtils.h>, a clang 22 header. SPIRV-LLVM-Translator must have the same
# major.minor (Mesa meson.build:2071-2082); SPIRV-Tools 2024.1 or newer (meson.build:2087-2094).
NM_PIN_TOOL_LLVM_MAJOR=22
NM_PIN_TOOL_SPIRV_TOOLS_MIN=2024.1
# glslangValidator 12.2: Mesa meson.build:734-758 when BVH shaders are built, which nvk-macos.patch turns on for NVK.
NM_PIN_TOOL_GLSLANG_MIN=12.2
# rustc: Mesa meson.build:950 (1.85); the pinned toolchain above is newer.
NM_PIN_TOOL_RUSTC_MIN=1.85
# Homebrew formulas build.sh installs when the tool is missing (only missing ones; never an upgrade). llvm@22 is "llvm"
# while Homebrew's current LLVM is 22, and so is spirv-llvm-translator; otherwise the translator is built (above).
NM_PIN_BREW_FORMULAS="gpatch ninja cmake pkgconf glslang spirv-tools llvm@22 python@3.14"

# ---- The working-directory toolchain (build/toolchain.sh)
# On a Mac where Homebrew has no ready-made builds (every Intel Mac since 2026: homebrew-core carries bottles for Apple
# silicon only), the tools NVK's build needs are built from these sources into the working directory instead of being
# installed with Homebrew. Each is a tag's peeled commit as "git ls-remote" gave it on 2026-11-13. The ninja, glslang,
# SPIRV-Tools, Python and LLVM versions equal homebrew-core's on that day, so an Intel Mac building this way and an
# Apple-silicon Mac using Homebrew build NVK with the same tools. The mirror variable replaces the URL, as above.
NM_PIN_TOOLCHAIN_SOURCES="TCNINJA TCCMAKE TCOPENSSL TCZSTD TCXZ TCPYTHON TCPKGCONF TCSPIRVHEADERS TCSPIRVTOOLS TCGLSLANG TCLLVM"
# ninja 1.13.2: what meson and cmake drive (meson needs 1.8.2); Homebrew's version.
NM_PIN_TCNINJA_NAME="ninja 1.13.2 (toolchain)"
NM_PIN_TCNINJA_URL=https://github.com/ninja-build/ninja.git
NM_PIN_TCNINJA_MIRROR=NM_TC_NINJA_URL
NM_PIN_TCNINJA_REF=v1.13.2
NM_PIN_TCNINJA_COMMIT=3441b633c2fe2c494e958780ba0f4227b1327634
# CMake 4.4.5: the Vulkan loader, SPIRV-Tools, glslang, LLVM and the SPIRV-LLVM-Translator are CMake projects (the
# loader needs 3.22.1). The newest 4.4 release; Homebrew has 4.5.0, and nothing here needs a 4.5 feature.
NM_PIN_TCCMAKE_NAME="CMake 4.4.5 (toolchain)"
NM_PIN_TCCMAKE_URL=https://github.com/Kitware/CMake.git
NM_PIN_TCCMAKE_MIRROR=NM_TC_CMAKE_URL
NM_PIN_TCCMAKE_REF=v4.4.5
NM_PIN_TCCMAKE_COMMIT=aa4fae824a02a175c4a11653d7a851a948f0c004
# OpenSSL 3.5.9 (the 3.5 long-term-support line, Homebrew's openssl@3), static, only inside Python (ssl, hashlib).
NM_PIN_TCOPENSSL_NAME="OpenSSL 3.5.9 (toolchain, for Python)"
NM_PIN_TCOPENSSL_URL=https://github.com/openssl/openssl.git
NM_PIN_TCOPENSSL_MIRROR=NM_TC_OPENSSL_URL
NM_PIN_TCOPENSSL_REF=openssl-3.5.9
NM_PIN_TCOPENSSL_COMMIT=45e844fa2a14ec92d146bd8f5778ac130b6625fb
# zstd 1.5.7, static, only inside Python: compression.zstd, which tools/test_fw_fetch.py imports. Homebrew's version.
NM_PIN_TCZSTD_NAME="zstd 1.5.7 (toolchain, for Python)"
NM_PIN_TCZSTD_URL=https://github.com/facebook/zstd.git
NM_PIN_TCZSTD_MIRROR=NM_TC_ZSTD_URL
NM_PIN_TCZSTD_REF=v1.5.7
NM_PIN_TCZSTD_COMMIT=f8745da6ff1ad1e7bab384bd1f9d742439278e99
# xz 5.8.4, static liblzma only inside Python: lzma, which tools/test_igfx_trace.py imports (the macOS SDK has no
# lzma.h). Homebrew's version.
NM_PIN_TCXZ_NAME="xz 5.8.4 (toolchain, for Python)"
NM_PIN_TCXZ_URL=https://github.com/tukaani-project/xz.git
NM_PIN_TCXZ_MIRROR=NM_TC_XZ_URL
NM_PIN_TCXZ_REF=v5.8.4
NM_PIN_TCXZ_COMMIT=d3e650e63c110e830fd5391e7f8b45df0b91d3da
# CPython 3.14.10: Mesa needs 3.10, the host tests 3.14 (NM_PIN_TOOL_PYTHON_TESTS_MIN); Homebrew's python@3.14.
NM_PIN_TCPYTHON_NAME="CPython 3.14.10 (toolchain)"
NM_PIN_TCPYTHON_URL=https://github.com/python/cpython.git
NM_PIN_TCPYTHON_MIRROR=NM_TC_PYTHON_URL
NM_PIN_TCPYTHON_REF=v3.14.10
NM_PIN_TCPYTHON_COMMIT=b703b2c5e704341d61e367b9b940b77ed11dded0
# pkgconf 2.5.1 as pkg-config, for meson. Homebrew has 3.0.10; 2.5.1 is the release whose meson build the toolchain's
# recipe was written against, and Mesa asks pkg-config nothing a 2.x pkgconf does not answer.
NM_PIN_TCPKGCONF_NAME="pkgconf 2.5.1 (toolchain)"
NM_PIN_TCPKGCONF_URL=https://github.com/pkgconf/pkgconf.git
NM_PIN_TCPKGCONF_MIRROR=NM_TC_PKGCONF_URL
NM_PIN_TCPKGCONF_REF=pkgconf-2.5.1
NM_PIN_TCPKGCONF_COMMIT=4fc570f91d9d8d843ab32d2198a5c064538d8ffd
# SPIRV-Headers vulkan-sdk-1.4.363.0, the commit SPIRV-Tools vulkan-sdk-1.4.363.0 is built with (its DEPS file names
# 496543121ce6419f23d6fa5d7194ba66c36212d2 as spirv_headers_revision; glslang 16.6.0's known_good.json names the same).
# Not NM_PIN_SPIRVHEADERS above, which is the commit the SPIRV-LLVM-Translator names: the two are fetched separately.
NM_PIN_TCSPIRVHEADERS_NAME="Khronos SPIRV-Headers vulkan-sdk-1.4.363.0 (toolchain, for SPIRV-Tools)"
NM_PIN_TCSPIRVHEADERS_URL=https://github.com/KhronosGroup/SPIRV-Headers.git
NM_PIN_TCSPIRVHEADERS_MIRROR=NM_TC_SPIRVHEADERS_URL
NM_PIN_TCSPIRVHEADERS_REF=vulkan-sdk-1.4.363.0
NM_PIN_TCSPIRVHEADERS_COMMIT=496543121ce6419f23d6fa5d7194ba66c36212d2
# SPIRV-Tools vulkan-sdk-1.4.363.0: Mesa's mesa_clc needs 2024.1 or newer (NM_PIN_TOOL_SPIRV_TOOLS_MIN); Homebrew's.
NM_PIN_TCSPIRVTOOLS_NAME="Khronos SPIRV-Tools vulkan-sdk-1.4.363.0 (toolchain)"
NM_PIN_TCSPIRVTOOLS_URL=https://github.com/KhronosGroup/SPIRV-Tools.git
NM_PIN_TCSPIRVTOOLS_MIRROR=NM_TC_SPIRVTOOLS_URL
NM_PIN_TCSPIRVTOOLS_REF=vulkan-sdk-1.4.363.0
NM_PIN_TCSPIRVTOOLS_COMMIT=ef96ed763b43b59b33b31b362f09a02b729fa1c9
# glslang 16.6.0 (glslangValidator, 12.2 or newer for NVK's BVH shaders); Homebrew's. Its known_good.json names the
# SPIRV-Tools commit above.
NM_PIN_TCGLSLANG_NAME="Khronos glslang 16.6.0 (toolchain)"
NM_PIN_TCGLSLANG_URL=https://github.com/KhronosGroup/glslang.git
NM_PIN_TCGLSLANG_MIRROR=NM_TC_GLSLANG_URL
NM_PIN_TCGLSLANG_REF=16.6.0
NM_PIN_TCGLSLANG_COMMIT=e1b562a8bed273a02f30b59b66a5d499793cede5
# LLVM 22.1.8 with clang: NM_PIN_TOOL_LLVM_MAJOR, and the SPIRV-LLVM-Translator v22.1.8 above is built against it;
# Homebrew's llvm@22.
NM_PIN_TCLLVM_NAME="LLVM 22.1.8 with clang (toolchain)"
NM_PIN_TCLLVM_URL=https://github.com/llvm/llvm-project.git
NM_PIN_TCLLVM_MIRROR=NM_TC_LLVM_URL
NM_PIN_TCLLVM_REF=llvmorg-22.1.8
NM_PIN_TCLLVM_COMMIT=ca7933e47d3a3451d81e72ac174dcb5aa28b59d1
