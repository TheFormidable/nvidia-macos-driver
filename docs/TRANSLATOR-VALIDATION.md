# Translator checks

The shader translator (`translator/translator`, the `metal2vulkan` crate, and `translator/wrapper`, the
`libnvmtl_translate` C library the plugin loads) is checked by its Rust tests, which run on any host, and by programs
that need a Mac with the driver's GPU. This page lists both for the repairs that came from the
HighDelay/nvidia-macos-driver-nvk fork (in the translator since 3.0), and says where each has run.

## Rust tests

With `spirv-val` and `spirv-dis` on `PATH` (tests that find no `spirv-val` say so and skip that step):

```sh
cargo test --all-targets --manifest-path translator/translator/Cargo.toml
cargo test --all-targets --manifest-path translator/wrapper/Cargo.toml
```

What the repairs added or changed:

| Repair | Tests |
| --- | --- |
| Argument-buffer texture arrays read through the image heap; a single texture buffer through the heap's texel binding | `tests/argument_buffer_wrapped_resource.rs` (flat C-array and texture-buffer cases, each with `NVMTL_NO_TIER2_FIXED` unset, 0 and 1, the last two in child processes) |
| Buffer-address lowering refuses an escaping address-table pointer, and finds a kept buffer's chains before checking their uses | wrapper `buffer_addresses.rs`, `refuses_table_pointers_passed_to_helpers_before_changing_types`, `a_pointer_reaching_a_phi_on_the_back_edge_keeps_no_descriptor` |
| ivec2 uniform through a vec4 slot, 2 to 16 samplers in a sampler-phi retry | `tests/gl_uniform_vector_loads.rs`, `tests/gl_sampler_phi_limits.rs`, `inline.rs` unit tests |
| Address-space parsing, array_ref<void> table reads, local device-pointer stores, RGB10A2 residual | `native::parse` unit tests; `native::tests::buffers`, `textures`, `intrinsics` (data-flow checks) |
| Texture and sampler handles in a local object's fields | `native::tests::textures`, `native_local_resource_objects_keep_both_live_samples` |
| array<sampler, 1> and per-element sampler state | `tests/sampler_descriptor_arrays.rs`; wrapper `sampler_array_state_tests` |
| Constant-expression call arguments kept whole when inlining | `inline.rs`, `call_arguments_preserve_complete_constant_expressions` and the inlined splat |
| Struct-wrapped scalar-array vector reads (this tree's implementation), their limits | `tests/wrapped_uniform_load_bounds.rs` |
| WebGPU argument-buffer textures (integer copy, multisample, query only) | `tests/argument_buffer_integer_resource.rs`, `tests/argument_buffer_image_queries.rs` |
| KHR compute-derivative contract of the owned emitter's check | `native::owned_cfg`, `owned_derivative_execution_model_check_matches_vulkan_validation` |

The fast sin/cos test runs its default and its `NVMTL_XLATE_TRIG_FLUSH=1` mode in two child processes, so the
variable never reaches tests that run beside it.

### On Linux

When the repairs were merged (3.0), these tests were built and run on Linux (x86_64, Rust 1.97.0, the distribution's
`spirv-val`). The library's
tests that sweep public shader fixtures fail there because the fixtures are not in the tree (for example
`every_public_fixture_reflects_the_images_it_binds`: "only 5 fixtures carrying 0 texture shapes were inspected"), and
the wrapper's `compositor_pixel_sampler_spirv_validates` and `ca_named_modules_validate` need private shader files;
these fail the same way without the repairs. The owned emitter's
`owned_pointer_null_policy_keeps_its_stricter_logical_rejection` and
`owned_derivative_execution_model_check_matches_vulkan_validation` pass; without the repairs, the two tests in their
place fail.

## GPU programs (a Mac with the driver)

None of these is a host test: `tools/run_all_tests.py` does not run them. They install nothing, restart nothing and
change no setting. Each is a row of the card checks (`tools/card/checks_xlate.py`, run by `tools/card_selftest.py`):
`xlate.sampler-array`, `xlate.int-handle`, `xlate.msaa`, `xlate.readback-prepare`, one `xlate.readback-<case>` row
per readback case, and the `xlate.paths-*` rows of `tools/card/xlate_paths.m` (written for this fork: the repairs no
other program checks on the card, such as argument-buffer texture arrays by a run-time index, a sampler picked among
8, 9 and 16, a back-edge pointer phi, RGB10A2 and RGB565 packing, and textures in a local object's fields).

Where they ran: on this fork's RTX 5050 (macOS 26.7.3) with 3.0.1's package, where `xlate.paths-samplerphi`,
`xlate.paths-abarray` and `xlate.paths-walk` failed (3.0.6 fixed the three translations); and at 3.0.7, where every
translator row passed (of 134 rows, 130 passed; the three that failed were Zink and 3.0.1 rows: CHANGELOG.md, 3.0.7,
"What ran where"). No card-check run since 3.0.7 is recorded.

- `tools/translator_readback.py` translates bounded compute kernels (taken from the unit tests' LLVM fixtures, with
  observable outputs added) through the built translator's C entry point, validates them with `spirv-val`, builds
  `tools/translator_readback.cpp` with `xcrun clang++` and runs each kernel through the built plugin binary loaded
  into that program's own process, comparing the output and neighbouring guard words. Cases: direct and helper
  address-table reads, local pointer stores, both arms of an address phi, 64-bit loads, RGB565 and RGB10A2 packing
  with NaN, clamp and half-way rounding, 1D textures, texture and sampler slots up to 127 and 15, distinct sampled and
  storage views, dynamic uniform slots read lane by lane, an `int2` read through a `float4` slot, a helper inlined
  with a vector splat argument, and texture and sampler fields of an object. `--prepare-only` stops after translation
  and validation and needs no GPU. A case whose program gives no result within 30 s ends the script by SIGALRM, since
  that may be a hung GPU. On Linux, with the Linux build of the wrapper, `--prepare-only` translated all 30 cases and
  `spirv-val --target-env vulkan1.2` accepted each. `tools/card_selftest.py` runs each case as its own row.

  ```sh
  python3 tools/translator_readback.py --plugin /path/to/NVMTLDriver \
    --translator /path/to/libnvmtl_translate.dylib --tools /path/to/spirv-tools/bin \
    --headers /path/to/Vulkan-Headers/include --out /path/to/readback-results
  ```

- `tools/test_sampler_array_gpu.m`: through the system's Metal device, samples with a scalar sampler,
  `array<sampler, 1>`, a texture array of one, nonzero binding bases and element 1 of `array<sampler, 2>`, each with
  nearest, linear and pixel-coordinate samplers (15 pixel checks).
- `tools/test_integer_texture_handle_gpu.m` and `tools/test_webgpu_msaa_gpu.m`: the WebGPU readbacks described in
  [WebGPU argument-buffer textures in Safari](SAFARI-WEBGPU-TEXTURES.md).

## Reported by the source fork

On an RTX 2060 SUPER, macOS 26.7.1, with the source fork's own build: 21 translator GPU readback cases and 15
sampler-array pixel checks passed after the sampler-array and inlining repairs, a captured three.js clearcoat shader
translated and passed `spirv-val`, and 27 readback cases passed after the uniform-slot cases were added. For the
WebGPU repairs, the four texture-swap and fog readbacks and the six resolve and query readbacks passed. The
struct-wrapped vector read in this tree is its own implementation, not the source fork's; the source fork's
readbacks and its 426-shader cache comparison do not cover it.
