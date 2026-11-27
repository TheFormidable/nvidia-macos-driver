# Minecraft with the native Vulkan runtime

Minecraft 26.3 has its own Vulkan renderer. With one saved JVM argument it uses this driver's native Vulkan runtime
(`/Library/GPUBundles/nvmtl/minecraft`, installed with the driver) instead of MoltenVK. No mod and no mod loader is
needed. Rendering goes to NVK, and each frame is copied on the GPU into the game's window.

It needs Intel Java 25, a GPU this driver's NVK supports, and the driver installed on macOS 15 or 26.

## Add the JVM argument

Append this one argument to the Java (JVM) arguments of the launcher's profile or instance:

```text
-Dorg.lwjgl.vulkan.libname=/Library/GPUBundles/nvmtl/minecraft/lib/libnvk_minecraft.dylib
```

Keep the arguments that are there (memory and others). It is a JVM argument, not a game argument, a terminal command or
an environment variable.

- **Prism Launcher:** right-click the instance, then **Edit**, **Settings**, **Java**. Turn on the instance's own Java
  arguments, keep the ones there and append the line above. Check that the selected Java is an Intel Java 25.
- **Minecraft Launcher:** **Installations**, the installation's **Edit**, **More Options**, **JVM Arguments**. Append the
  line and save.
- **Other launchers:** the JVM arguments field of the instance or profile.

Then choose **Vulkan** in Minecraft's graphics settings and restart the game completely. The game's log and the F3
screen name **NVK Mesa**, and the log has a line `NVK Bridge: native device=...`. The argument alone does not switch a
profile that uses OpenGL to Vulkan.

Why an argument: on macOS, Minecraft 26.3's LWJGL 3.4.3 loads the MoltenVK it bundles when no library is set, and a
Vulkan loader manifest does not change that. The argument names the provider before Vulkan starts. The game then gives
the same library to SDL and keeps all its own device and feature checks.

## The companion mod

The source fork reports that its optional companion selector mod, from version 0.1.2, finds the runtime at
`/Library/GPUBundles/nvmtl/minecraft` by itself and checks it with `nvk-bridge-runtime.properties`. That mod has not been
run with this driver; the JVM argument above is the way this driver documents and tests. Use either the mod or the JVM
argument. With the JVM argument, remove the mod's jar from the instance's `mods/` folder and
the mod's own `-Dnvkbridge.runtime=...` and `-Dnvkbridge.enabled=...` arguments.

## A runtime in another folder

Use the absolute path of that runtime's frontend:

```text
-Dorg.lwjgl.vulkan.libname=/absolute/path/to/runtime/lib/libnvk_minecraft.dylib
```

When the path has spaces, quote the whole argument in launchers that split the field at spaces:

```text
"-Dorg.lwjgl.vulkan.libname=/Volumes/External Drive/runtime/lib/libnvk_minecraft.dylib"
```

Keep the runtime's files together: the frontend loads `libnvk_renderer.dylib` from its own folder. Name the frontend,
never `libnvk_renderer.dylib` or the driver's `libvulkan_nouveau.dylib`.

## Going back

Remove the `-Dorg.lwjgl.vulkan.libname=...` argument and restart Minecraft completely. The game then uses its usual
provider. When the library named by the argument fails to load, the game does not fall back to MoltenVK in the same
run.

To keep the runtime and go back only on how frames are presented with vertical sync off (3.0.9 and 3.0.11), write
the line `NVK_BRIDGE_PRESENT_NEWEST=0` into your own block of `/Library/GPUBundles/nvmtl/nvrm610.conf` (between
`# --- my settings, kept on update ---` and `# --- end my settings ---`), from the first column and without blanks,
and restart the game. Every present then waits for its frame on the game's thread, as before 3.0.9, the MAILBOX present
mode is no longer offered, and the game's log has `NVK Bridge: swapchain presentation=synchronous` for every
swapchain. The runtime reads the switch once, when the game first asks which present modes its window offers or makes
its first swapchain without vertical sync, and then logs `NVK Bridge: NVK_BRIDGE_PRESENT_NEWEST=0: IMMEDIATE swapchains
present synchronously`. Turning vertical sync on in the game does the same for that game without the file.

## What is known

The source fork reports Minecraft 26.3 with Fabric 0.19.5 and with NeoForge 26.3.0.99 rendering through this frontend
on an RTX 2060 SUPER, macOS 26.7.1, and checks the vanilla start with `ModFreeProbe.java`. This driver's runtime has run
Minecraft 26.3 without mods on this fork's test machine (AMD Ryzen 5 5500, RTX 5050, macOS 26.7.3, a display of
100 Hz).

How frames are presented with vertical sync off:

- **Through 3.0.8** every present waited for the window on the game's thread, so the game stood at a multiple of the
  display's rate: 200 frames a second there.
- **Since 3.0.9** (IMMEDIATE) a present does not wait for the window: whenever the window can take a frame the runtime
  shows the newest one the game has presented, once it has finished rendering, and a frame that a newer one overtook
  first is not shown. The game's frame rate can leave the display's. 3.0.9 was built and installed on that Mac on
  2026-11-22; it restarted without a fault, and with vertical sync off its owner read up to 1000 frames a second on
  the game's own counter, where a capture on 3.0.8 had 200. No capture was taken on 3.0.9, so that figure is a
  reading, not a measurement.
- **Since 3.0.11** the runtime also offers MAILBOX (the newest frame, one for each refresh of the display), which an
  application that prefers it to IMMEDIATE will choose.

The game's log says which way a swapchain presents (`NVK Bridge: swapchain presentation=newest frame` or
`=synchronous`), and when the game closes or resizes its window, how many frames it handed over, how many were shown
and how many were overtaken (`newest_frames_presented`, `newest_frames_shown`, `newest_frames_dropped`).
