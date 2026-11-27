# WebGPU argument-buffer textures in Safari

Safari runs WebGPU on Metal, and WebKit's Metal shaders reach their textures through argument buffers. Three shapes of
those shaders lost a texture in the translator. The repairs come from the HighDelay/nvidia-macos-driver-nvk fork, which
found them with a WebGPU game in Safari: the menu was drawn over a black car and garage, and after the first repair the
scene was too dark. Chromium browsers drew the game correctly. The repairs are in the translator since 3.0 and apply to
any shader with these shapes, not only to Safari's.

With this driver Safari itself does not reach them: its GPU process cannot load the Metal plugin, so WebGPU in Safari
draws in software ([SAFARI-WEBGL.md](SAFARI-WEBGL.md)). Chromium browsers and Firefox use the GPU.

## What the translator does

### A texture copied as an integer

Metal's optimizer can copy a texture field of an argument buffer as a 64-bit integer and turn it back into the handle:

```llvm
%field = getelementptr inbounds %Group, ptr addrspace(2) %group, i64 0, i32 1
%payload = load i64, ptr addrspace(2) %field, align 8
%texture = inttoptr i64 %payload to ptr addrspace(1)
; air.sample_texture_2d takes %texture
```

When the integer's only use is that `inttoptr` and the result is a known texture, the load is emitted as the
texture's own resource load, under the handle's name, before any raw-buffer handling
(`translator/translator/src/native/emitter/body/core_inst.rs`, `emit_load_resolved_inner`). The pairs are found from the
module's `inttoptr` instructions in every address model (`translator/translator/src/native/emitter/functions.rs`).
An integer with any other use stays an integer. A pair found only after raw-buffer emission, or only with buffer
device addresses, makes the first construction fail with `native emitter: unknown SSA value`, and the raw-first
sampler retry can then replace the sample with zero.

### A uniform buffer inside an argument group

The raw-first retries (`emit_vulkan_spirv_all_buffers_raw_with_sidecar` and its workgroup variant in
`translator/translator/src/native/emit_tiers.rs`) keep the device-address model when the module loads a buffer
pointer from a constant buffer and needs device addresses, as a uniform buffer nested in a WebGPU argument group
does. Directly bound word buffers and atomic cursors keep their raw form.

### A multisampled texture, and a texture that is only queried

Embedded texture discovery keeps the multisampled flag of the texture's type (`translator/translator/src/meta/embedded.rs`),
and the flag reaches the image type, the reflection the plugin reads, the loaded values, descriptor arrays and the
bindless heap's metadata (`translator/translator/src/passes/stage_input/mod.rs`). A read of sample N is an
`OpImageFetch` with the `Sample` operand. Without the flag the sample index became a mip level, and in a resolve shader
that averages four samples only sample 0 counted.

Discovery runs for every AIR image intrinsic (`air_intrinsics::air_image_intrinsic`), not only for sample, gather,
read and write, so a shader that only asks a texture for its size, mip count or sample count gets its descriptor.
Without it those queries returned zero sizes and counts of one.

## Checks in this tree

- `translator/translator/tests/argument_buffer_integer_resource.rs`: six cases, among them a texture-only group without
  device addressing, an integer that has another use, separate uniform and texture groups, and the sampler retry
  keeping a nested uniform buffer's address.
- `translator/translator/tests/argument_buffer_image_queries.rs`: size and mip queries of a query-only texture, a
  multisample query, and a four-sample resolve that fetches samples.
- `tools/test_integer_texture_handle_gpu.m` with `tools/fixtures/integer_texture_handle.metal`, and
  `tools/test_webgpu_msaa_gpu.m` with `tools/fixtures/webgpu_msaa_resolve.metal`: offscreen readbacks on a Mac. Each
  loads the NVMTLDriver binary named on its command line into its own process with `dlopen`, makes an `NVMTLDevice`
  on the `NVAccel` service, renders and compares every pixel with a reference computed on the CPU. They install
  nothing and change no setting. The build and run lines are in each file's first lines. The first swaps two
  textures and turns on a nonzero fog uniform (four frames); the second renders the resolve and the queries through
  the reflected and the descriptor-built argument encoders (six frames).

The two Rust tests ran on Linux and pass. The two Metal programs are the card-check rows `xlate.int-handle` and
`xlate.msaa` (`tools/card/checks_xlate.py`, run by `tools/card_selftest.py`). On this fork's RTX 5050 (macOS 26.7.3)
they ran with 3.0.1's package, and again at 3.0.7, where every translator row passed (of 134 rows, 130 passed; the
three that failed were Zink and 3.0.1 rows: CHANGELOG.md, 3.0.7, "What ran where").

## Reported by the source fork

On an RTX 2060 SUPER, macOS 26.7.1, Safari 27: the shader errors were gone after the integer-handle repair, but the
scene stayed dark; with the multisample and query repair the resolve probe returned the clear color (0.2, 0.4, 0.6, 1)
instead of (0.05, 0.10, 0.15, 0.25), the size and sample query returned (4, 4, 4, 1), and the game drew the car and
the garage. The four texture-swap and fog readbacks and the six resolve and query readbacks passed there. Other GPUs,
macOS 15 and full gameplay were not tested by the source fork. How Safari's GPU process loaded the plugin there is not recorded in
this repository; with this driver it cannot ([SAFARI-WEBGL.md](SAFARI-WEBGL.md)).
