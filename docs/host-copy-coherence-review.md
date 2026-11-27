# Host-copy coherence adaptation for PR #178

A review from the original project (NullMoth), kept because the code it describes is still the plugin's. Its commit
b549808a, based on the original's 1.15.0 (`56bc4f9ae3105df052eefd3ea094c782e56853c3`), adapted the missing
texture-backed host-copy synchronization identified by
[modesttao in PR #178](https://github.com/nullmoth/nvidia-macos-driver/pull/178).

`nvmtl_submission_copies` (`plugin/NVMTLEventSync.m`) finds copy-backed buffers bound directly or through textures/views, and deduplicates backing objects within each submission part. Both submission paths upload before GPU submission. Successful completion publishes surfaces and downloads potentially written buffers before signaling the completion event. Direct managed buffers retain their existing writeback path.

Texture usage `Unknown`, `ShaderWrite`, and `RenderTarget` permits writeback; explicit read-only usage does not. Buffers with neither a caller-memory copy nor a managed shadow are excluded. The default submit-ahead host-pointer hold includes textures/views. Finishing an already submitted part does not upload again. GPU completion waits and failure handling remain in place.

Since then the same function also takes the members of a used heap that have a host copy, and since 3.0.5 a caller
copy that a part only reads no longer holds that part back from the submit-ahead while no sealed part may have
written it (from pull request 524 of the original project; way back `NVMTL_GSUB_HOST_READS=0`).
`tools/test_readonly_copyback.py` (resource tracking and copy selection) and `tools/test_host_reads_ahead.py` (the
read-only case) check them on the host.

## CPU regression

On macOS with the Command Line Tools:

```sh
python3 tools/test_submission_copies.py
```

The test extracts the production discovery helper, byte-transfer functions, and both submission methods into an Objective-C fixture. It checks post-wrapping host edits, texture views, shared backing deduplication, unknown/render-target usage, read-only protection, direct managed writeback, allocation failure, failed submission isolation, publication before event signaling, and submit-ahead completion. It runs both default and `NVMTL_GSUB_HOSTPTR_LIFT=1` modes. Vulkan/GPU execution is stubbed. Restoring the original submission methods in the same fixture reproduces the stale-pixel assertion.

## GPU regression

```sh
clang -fobjc-arc tools/bench_copy_coherence.m -o /tmp/bench_copy_coherence \
  -framework Foundation -framework Metal -framework IOKit
/tmp/bench_copy_coherence /absolute/path/to/NVMTLDriver
NVMTL_PROCESS_ENV=1 NVMTL_GSUB_HOSTPTR_LIFT=1 /tmp/bench_copy_coherence /absolute/path/to/NVMTLDriver
```

Use a built plugin with its matching runtime libraries available beside it. The harness explicitly loads that binary into its own process; it does not install it. It intentionally uses an unaligned pointer and checks the driver's `_hostPtr` fallback, so this is a regression test of this driver only. An aligned/zero-copy buffer is rejected as the wrong test path. Apply a process timeout when automating GPU runs. The asynchronous commit with submit-ahead (`NVMTL_ASYNC_COMMIT`) is the default, so the first run covers it; a release plugin keeps a process's own `NVMTL_` variables only with `NVMTL_PROCESS_ENV=1`, hence that variable in the second run. The harness is not one of the card checks, and no run of it on this fork's card is recorded.

Measured by the original project on an RTX 4050 Laptop (AD107), Tahoe 26.7.1 / 25G241: original 1.15.0 failed all 16 host-to-texture and all 16 texture-to-host cases; this adaptation passed all cases, including alternating texture views. Enabling submit-ahead options gave the same results. Those GPU runs enable the options but do not trace which parts used ahead submission; the CPU fixture executes that method directly. These tests do not measure frame rate or establish long-term stability.
