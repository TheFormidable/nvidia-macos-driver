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

## What is known

The source fork reports Minecraft 26.3 with Fabric 0.19.5 and with NeoForge 26.3.0.99 rendering through this frontend
on an RTX 2060 SUPER, macOS 26.7.1, and checks the vanilla start with `ModFreeProbe.java`. This tree's runtime was built
and checked on Linux only; it has not run Minecraft on a Mac with this tree's NVK.
