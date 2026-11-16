"""The install rows of tools/card_selftest.py: what the package put under /Library/GPUBundles for native Vulkan and
native OpenGL, read without the GPU, and the two installed probes of native OpenGL, which touch no GPU either.

The read rows name a function of the runner ("func"), which reads the installed files with Python and runs no program:
  install.identity        the gates' facts as a row: the staged package's commit, the SHA256SUMS comparison of every
                          file under /Library/GPUBundles (nvrm610.conf left out), the driver sources of HEAD
  install.zink-bundle     IOGLBundleName is NVKZinkGLDriver, and NVKZinkGLDriver.bundle's Info.plist names
                          com.nullmoth.NVKZinkGLDriver and holds its executable (inactive when the accelerator names
                          Apple's OpenGL, as with the boot argument nvzink=0)
  install.minecraft-files nvmtl/minecraft holds exactly the runtime's four files with the installer's modes and owner;
                          the loader manifest (file_format_version 1.0.0, ./lib/libnvk_minecraft.dylib, the Vulkan
                          version of the build's headers); the properties file's library.sha256 and runtime.id match
                          the installed frontend; both libraries are x86_64 with their @loader_path install names, the
                          frontend's minos is 15.0, and the renderer's LC_UUID is that of nvmtl/libvulkan_nouveau.dylib
                          (the shader-cache identity). The loaders beside it are listed as information.
  install.zink-files      nvmtl/zink holds the package's eight files, its Mach-O files are Mach-O, its frontend and
                          renderer copies carry the LC_UUID of the runtime's frontend and of libvulkan_nouveau.dylib;
                          the GL versions of cgl-dispatch-map.json and the macOS build are information
  install.mesa-source     MESA-SOURCE.json names the Mesa commit build/prepare_zink.py pins
The program rows run the installed probes as package/install.sh does after an install:
  install.gl-bundle-trusted  gld_bundle_probe NVMTLDriver (the control), then NVKZinkGLDriver: macOS's trusted
                             GPU-bundle lookup finds both; a control that is not found makes the row a skip, as the
                             installer treats it
  install.zink-sync-probe    zink_sync_probe: Mesa's barrier and queue as the zink patch repairs them (CPU only; a
                             broken barrier deadlocks, hence the limit)
Not yet run on a Mac."""

CHECKS = [
    {"id": "install.identity", "part": "install", "kind": "read", "order": 100, "func": "install_identity",
     "title": "the installed driver is the staged package of the expected commit"},
    {"id": "install.zink-bundle", "part": "install", "kind": "read", "order": 101, "func": "install_zink_bundle",
     "title": "the accelerator names NVKZinkGLDriver, and its bundle is in place",
     "info": [r"^INFO (IOGLBundleName|CFBundleIdentifier) "]},
    {"id": "install.minecraft-files", "part": "install", "kind": "read", "order": 102, "func": "install_minecraft",
     "title": "nvmtl/minecraft: the native Vulkan runtime's files, manifest and identity",
     "info": [r"^INFO (manifest|runtime\.id|loader|built loader) "]},
    {"id": "install.zink-files", "part": "install", "kind": "read", "order": 103, "func": "install_zink_files",
     "title": "nvmtl/zink: native OpenGL's files and runtime copies",
     "info": [r"^INFO (GL core|macOS build|also in) "]},
    {"id": "install.mesa-source", "part": "install", "kind": "read", "order": 104, "func": "install_mesa_source",
     "sources": ["build/prepare_zink.py"],
     "title": "MESA-SOURCE.json names the pinned Mesa commit"},
    {"id": "install.gl-bundle-trusted", "part": "install", "kind": "cpu", "order": 110,
     "title": "macOS's GPU-bundle lookup trusts NVKZinkGLDriver.bundle (NVMTLDriver.bundle as the control)",
     "argv": ["/bin/sh", "-c",
              'p=$1; if ! "$p" NVMTLDriver; then echo "SKIP: the GPU-bundle lookup does not find NVMTLDriver.bundle '
              'either here, so it cannot judge the OpenGL bundle"; exit 3; fi; "$p" NVKZinkGLDriver',
              "gl-bundle-trusted", "{GB}/nvmtl/zink/gld_bundle_probe"],
     "limit": 30,
     "expect": {"rc": 0, "stdout": [r"^Trusted GPU bundle: /Library/GPUBundles/NVMTLDriver\.bundle",
                                    r"^Trusted GPU bundle: /Library/GPUBundles/NVKZinkGLDriver\.bundle"]},
     "rc_map": {3: "skip"}},
    {"id": "install.zink-sync-probe", "part": "install", "kind": "cpu", "order": 111,
     "title": "Mesa's barrier and queue on this macOS (the installed zink_sync_probe)",
     "argv": ["{GB}/nvmtl/zink/zink_sync_probe"],
     "limit": 30,
     "expect": {"rc": 0, "stdout": [r"^PASS: 1000 four-thread barrier generations, one serial return each; "
                                    r"100 queue completion/teardown cycles$"]},
     "rc_map": {}},
]
