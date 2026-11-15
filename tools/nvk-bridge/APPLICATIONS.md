# MPV and other Vulkan applications with the native Vulkan runtime

The runtime at `/Library/GPUBundles/nvmtl/minecraft` serves any application that draws into a window with
`VK_EXT_metal_surface`, not only Minecraft. Rendering goes to NVK, and each frame is copied on the GPU into the window.
The runtime is a complete Vulkan driver for the Vulkan loader (an ICD): it exports `vk_icdGetInstanceProcAddr`,
`vk_icdGetPhysicalDeviceProcAddr` and `vk_icdNegotiateLoaderICDInterfaceVersion`, and its manifest is
`nvk_bridge_icd.json`.

It needs an x86_64 application and the driver installed on macOS 15 or 26. Nothing selects it by default: each launch
chooses it, as below, and an application launched without that setting keeps its usual provider.

```sh
NVK_BRIDGE_RUNTIME=/Library/GPUBundles/nvmtl/minecraft
ls "$NVK_BRIDGE_RUNTIME/nvk_bridge_icd.json" "$NVK_BRIDGE_RUNTIME/lib/libnvk_minecraft.dylib" \
   "$NVK_BRIDGE_RUNTIME/lib/libnvk_renderer.dylib"
```

## MPV

Use an x86_64 MPV built with the Vulkan loader, `gpu-next` and the `macvk` context (`mpv --gpu-context=help` lists the
contexts). Start with software decoding, which checks the Vulkan path alone:

```sh
VK_DRIVER_FILES="$NVK_BRIDGE_RUNTIME/nvk_bridge_icd.json" \
mpv --no-config --vo=gpu-next --gpu-api=vulkan --gpu-context=macvk \
    --hwdec=no --log-file="$HOME/Desktop/mpv-nvk.log" -- "$HOME/Movies/example.mp4"
```

The log names the NVIDIA device and NVK. When playback works, `--hwdec=videotoolbox-copy` in place of `--hwdec=no`
decodes with VideoToolbox; that mode copies decoded frames through system memory. The runtime adds no decoder and does
not share decoder surfaces with Vulkan.

## Other applications

| What the application lets you choose | What to give it |
|---|---|
| a Vulkan driver manifest (ICD) | `/Library/GPUBundles/nvmtl/minecraft/nvk_bridge_icd.json` |
| a provider library loaded through `vkGetInstanceProcAddr` | `/Library/GPUBundles/nvmtl/minecraft/lib/libnvk_minecraft.dylib` |
| a whole Vulkan loader library | the application's loader, with `VK_DRIVER_FILES` as below |

For an application that uses the Vulkan loader, set the manifest for that launch:

```sh
VK_DRIVER_FILES=/Library/GPUBundles/nvmtl/minecraft/nvk_bridge_icd.json /path/to/application
```

For an application with a window, quit it completely first, then start it through LaunchServices with the setting:

```sh
open --env VK_DRIVER_FILES=/Library/GPUBundles/nvmtl/minecraft/nvk_bridge_icd.json -a "/Applications/Example.app"
```

Give absolute paths in an application's settings: settings files may not expand shell variables. A provider library
exports `vkGetInstanceProcAddr` only. An application that calls core functions such as `vkCreateInstance` by name needs a
loader and the manifest route. The driver's own `/Library/GPUBundles/nvmtl/libvulkan_nouveau.dylib` has no GPU-copy
presentation; name the frontend.

## When it does not work

- **MoltenVK still appears:** check which library the application loads. An application linked to MoltenVK directly
  cannot be redirected with a manifest.
- **`macvk` or `gpu-next` is not offered:** use an MPV build that has them.
- **The loader does not find the driver:** add `VK_LOADER_DEBUG=all` to the same launch. Older loaders read
  `VK_ICD_FILENAMES` instead of `VK_DRIVER_FILES`.
- **An extension is missing:** the runtime does not offer the renderer's present id, present wait, swapchain
  maintenance, present timing or HDR metadata extensions (the runtime's README.md, `tools/nvk-bridge` in the driver's
  source). An application that requires one of them cannot
  use this runtime.
- **Hardware decoding fails:** try the software decoding command above first.

The loader's selection rules are in the Khronos loader and driver interface document ("Overriding the Default Driver
Discovery"). Each application's own rendering and presentation needs decide whether it works; the runtime implements
what the runtime's README.md lists.
