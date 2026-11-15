# Photo Booth's preview

Photo Booth draws its camera preview with Core Image into the drawables of a CAMetalLayer. Two separate things in the
Metal plugin decide whether that preview shows: whether Core Image accepts the drawable as a destination, and which
way up the rows land in it. Both changes come from the HighDelay/nvidia-macos-driver-nvk fork, which reports the
preview visible and upright with them on an RTX 2060 SUPER, macOS 26.7.1, with a USB webcam. On this branch they are
built and checked on Linux only; nothing of them has run on a card here.

## A destination Core Image accepts

QuartzCore wraps a drawable that is not framebuffer-only with a descriptor whose usage is ShaderRead only, or
Unknown. Core Image refuses a destination whose usage does not include ShaderWrite, and the preview stays black.

- An application's single-plane surface wrapper that is not framebuffer-only reports ShaderRead and RenderTarget, and
  ShaderWrite only when its backing has a storage view (`nvmtl_surface_usage` in `plugin/NVMTLDevice.m`).
- Such a wrapper asks for the storage view before the family's VRAM is imported, whenever its format can be a storage
  image (`nvmtlNewSurfaceTexture`).
- A linear import can lose the storage view that the optimal format has. A writable wrapper then gets a private image,
  which is writable whenever the format is, and the dropped image and the import are released once
  (`nvmtl_surface_image_create`).
- An Unknown usage stays Unknown only when the backing has no storage view; the optimal-tiled storage twin carries
  those writes. Over a real storage view it reports the explicit bits.

WindowServer and framebuffer-only textures keep the usage they asked for, and the planes of two-plane surfaces keep
the views they asked for.

## Which way up

The source fork reports that Photo Booth's legacy call, `-[CIContext render:toMTLTexture:commandBuffer:bounds:colorSpace:]`,
writes the image's rows bottom first into the drawable's texture, which the layer shows top first, so the preview is
upside down. `plugin/NVMTLPhotoBooth.m` reflects the image within the rendered bounds, and only when the destination is
a drawable texture of this driver in Photo Booth itself (process name "Photo Booth", bundle `com.apple.PhotoBooth`).
Camera buffers, offscreen textures, saved photos, other processes and Core Image's own `flipped` semantics keep their
paths. The adapter replaces `-[CAMetalLayer nextDrawable]` and that `CIContext` method in Photo Booth's process only:
it checks their type signatures first and leaves them alone when they differ, adds its implementation to the class
itself rather than changing an inherited one, and keeps the plugin image loaded for the life of the process.

`NVMTL_PHOTOBOOTH_FLIP=0` in the settings file turns the adapter off: Photo Booth's preview is then shown as Core Image
renders it. Quit and reopen Photo Booth after changing it.

## Checks

- `python3 tools/test_surface_drawable_usage.py`: the usage policy and the import fallback, compiled from the source
  with stand-ins, run as "Photo Booth" and as "WindowServer", with controls that must fail. It also holds the backing
  of shader-writable Managed wrappers (a tiled image over the kept import, `NVMTL_MANAGED_SURFACE_TILED`).
- `python3 tools/test_photobooth_preview.py`: the adapter's functions against stand-in classes (process identity,
  marking, the reflection, pass-through arguments, invalid bounds, signatures, inherited methods, the switch), with a
  control that must fail. It needs `xcrun clang` with Objective-C.
- `tools/photobooth_surface_probe.m`, on a Mac with the driver installed: Core Image renders into eight synthetic
  IOSurface drawables of the installed driver and every pixel is read back. It opens no camera and changes nothing on
  the system. Its build and run line is in its header.
