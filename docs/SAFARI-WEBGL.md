# WebGL in Safari

With this driver, WebGL and WebGPU in Safari draw in software. Chromium browsers and Firefox use the GPU.

Safari draws web content in its GPU process (`com.apple.WebKit.GPU`), which on macOS 26 runs from Safari's cryptex.
WebKit's sandbox profile for that process does not let it map `/Library/GPUBundles`, so it cannot load the driver's
Metal plugin. Its log says so:

    Error loading /Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver ...
    (file system sandbox blocked mmap() of '/Library/GPUBundles/NVMTLDriver.bundle/Contents/MacOS/NVMTLDriver')

Before that, the process tried the cryptex folders, which do not hold the bundle: Safari's own and the operating
system's.

    /System/Cryptexes/App/System/Library/StagedFrameworks/Safari/NVMTLDriver
    /System/Volumes/Preboot/Cryptexes/OS/Library/GPUBundles/NVMTLDriver.bundle/...

The driver does not change WebKit's sandbox. For GPU-heavy pages, use a Chromium browser or Firefox.

## The optional copy

`package/safari_gpu_copy.sh` places a copy of the driver bundle in the second folder. That folder is on the Preboot
volume, which is not part of the sealed system volume; the script never writes under `/System/Library` and refuses any
destination that does not resolve under `/System/Volumes/Preboot`. It is off by default. The first command installs
the driver with the copy, the second adds the copy later, and the third takes it away (uninstall.sh does this too):

    sudo env NULLMOTH_SAFARI_GPU_COPY=1 ./install.sh
    sudo ./safari_gpu_copy.sh install
    sudo ./safari_gpu_copy.sh remove

On a Mac where System Integrity Protection refuses the write, nothing is changed.

## What is not known

Whether the sandbox allows mapping code from that folder is not known: no run with the copy installed is recorded.
To find out on a Mac with the copy installed (macOS 15 or 26), after a restart:

1. Open a WebGL page in Safari (for example the three.js examples) and leave it open.
2. `log show --last 5m --predicate 'process == "com.apple.WebKit.GPU"'` and look for "file system sandbox blocked mmap()"
   against the Preboot path. If it is gone, the copy works. If it is still blocked, the sandbox allows only paths inside
   the sealed system, and the copy should stay off.
3. If the bundle maps but the process still logs "Vulkan is not up", the next blocker is the Vulkan driver files the plugin
   opens from `/Library/GPUBundles/nvmtl`. Either way, attach that log to an issue of this fork
   (https://github.com/TheFormidable/nvidia-macos-driver/issues).
