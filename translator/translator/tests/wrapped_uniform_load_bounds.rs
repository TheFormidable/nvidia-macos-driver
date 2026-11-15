//! Bounds of the struct-wrapped scalar-array vector read (see struct_wrapped_array_vector_loads.rs), on a compute
//! kernel shaped like DaVinci Resolve's uniform-slot reads: a `GenericUniformHeader { packed_uint4 header; }` buffer
//! read as float4 at a constant and at a data-dependent slot. Deeper wrappers, trailing members and an array of
//! arrays read only the leading array's four words; a lane width or count that does not match, or a matching array
//! that is not the leading member, is not read as the vector.
use metal2vulkan::passes::{Stage, TransformOptions};
use metal2vulkan::{disassemble, translate_native_no_retry_constructed_with_options};

fn spirv_val(spv: &[u8]) {
    let out = std::process::Command::new("spirv-val")
        .args(["--target-env", "vulkan1.2", "-"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .and_then(|mut c| {
            use std::io::Write;
            c.stdin.take().unwrap().write_all(spv)?;
            c.wait_with_output()
        });
    match out {
        Ok(out) => assert!(
            out.status.success(),
            "spirv-val: {}{}",
            String::from_utf8_lossy(&out.stdout),
            String::from_utf8_lossy(&out.stderr)
        ),
        Err(e) => eprintln!("spirv-val not runnable ({e}) - the independent check did NOT run"),
    }
}

// Both slot loads read the slot's four words and bitcast each to a float lane.
fn assert_lanes_are_bitcast_words(asm: &str) {
    if std::env::var_os("M2V_SHOW_ASM").is_some() {
        eprintln!("{asm}");
    }
    let count = |op: &str| asm.lines().filter(|l| l.contains(op)).count();
    assert_eq!(count("OpCompositeConstruct"), 2, "one vector per load:\n{asm}");
    assert_eq!(count("OpBitcast"), 8, "four lanes per load:\n{asm}");
}

#[test]
fn wrapped_uniform_loads_translate_at_a_constant_and_a_dynamic_slot() {
    assert_lanes_are_bitcast_words(&disassemble(&translate_kernel(KERNEL)).unwrap());
}

const KERNEL: &str = include_str!("fixtures/buffers/wrapped_uniform_load.ll");

fn translate_kernel(ll: &str) -> Vec<u8> {
    let spv = translate_native_no_retry_constructed_with_options(
        ll,
        Stage::Kernel,
        TransformOptions::default(),
    )
    .unwrap_or_else(|e| panic!("wrapped uniform kernel must translate: {e}"));
    spirv_val(&spv);
    spv
}

#[test]
fn wrapped_uniform_loads_cover_three_struct_levels() {
    let ll = KERNEL
        .replace("type { { [4 x i32] } }", "type { { { [4 x i32] } } }")
        .replace(
            "i64 0, i32 0, i32 0, i64 1",
            "i64 0, i32 0, i32 0, i32 0, i64 1",
        );
    assert_lanes_are_bitcast_words(&disassemble(&translate_kernel(&ll)).unwrap());
}

#[test]
fn wrapped_uniform_reads_leave_trailing_members_out_of_the_load() {
    let ll = KERNEL
        .replace(
            "type { { [4 x i32] } }",
            "type { { [4 x i32] }, <4 x i32> }",
        )
        .replace(
            "!\"air.arg_type_size\", i32 16, !\"air.arg_type_align_size\", i32 4",
            "!\"air.arg_type_size\", i32 32, !\"air.arg_type_align_size\", i32 4",
        );
    assert_lanes_are_bitcast_words(&disassemble(&translate_kernel(&ll)).unwrap());
}

#[test]
fn a_leading_array_of_scalar_arrays_reads_only_its_first_array() {
    let ll = KERNEL
        .replace("[4 x i32]", "[2 x [4 x i32]]")
        .replace(
            "i64 0, i32 0, i32 0, i64 1",
            "i64 0, i32 0, i32 0, i64 0, i64 1",
        )
        .replace(
            "!\"air.arg_type_size\", i32 16, !\"air.arg_type_align_size\", i32 4",
            "!\"air.arg_type_size\", i32 32, !\"air.arg_type_align_size\", i32 4",
        );
    assert_lanes_are_bitcast_words(&disassemble(&translate_kernel(&ll)).unwrap());
}

#[test]
fn a_wrapped_float_array_needs_no_numeric_conversion_to_float_lanes() {
    let ll = KERNEL
        .replace("[4 x i32]", "[4 x float]")
        .replace("packed_uint4", "packed_float4");
    let asm = disassemble(&translate_kernel(&ll)).unwrap();
    assert!(
        !asm.contains("OpConvertUToF") && !asm.contains("OpConvertSToF"),
        "{asm}"
    );
}

fn vector_read_from(header: &str) -> String {
    format!(
        r#"target triple = "air64_v28-apple-macosx26.7.0"
%Header = type {header}
define void @k(ptr addrspace(1) %out, ptr addrspace(2) readonly %input) {{
entry:
  %slot = getelementptr inbounds %Header, ptr addrspace(2) %input, i64 1
  %value = load <4 x float>, ptr addrspace(2) %slot, align 4
  store <4 x float> %value, ptr addrspace(1) %out, align 16
  ret void
}}
!air.kernel = !{{!0}}
!0 = !{{ptr @k, !1, !2}}
!1 = !{{}}
!2 = !{{!3, !4}}
!3 = !{{i32 0, !"air.buffer", !"air.location_index", i32 0, i32 1, !"air.write", !"air.address_space", i32 1, !"air.arg_type_size", i32 16, !"air.arg_type_align_size", i32 16, !"air.arg_type_name", !"float4*", !"air.arg_name", !"out"}}
!4 = !{{i32 1, !"air.buffer", !"air.location_index", i32 1, i32 1, !"air.read", !"air.address_space", i32 2, !"air.arg_type_size", i32 16, !"air.arg_type_align_size", i32 4, !"air.arg_type_name", !"Header", !"air.arg_name", !"input"}}
"#
    )
}

#[test]
fn mismatched_lane_width_or_count_does_not_widen_the_array_read() {
    for header in [
        "{ { [4 x i16] } }",
        "{ { [3 x i32] } }",
        "{ { [5 x i32] } }",
    ] {
        let result = translate_native_no_retry_constructed_with_options(
            &vector_read_from(header),
            Stage::Kernel,
            TransformOptions::default(),
        );
        let err = result.expect_err("a mismatched leading array must not be read as float4");
        assert!(err.contains("reinterpret load"), "{header}: {err}");
    }
}

#[test]
fn a_matching_array_after_another_member_is_not_a_zero_offset_read() {
    for header in ["{ i32, [4 x i32] }"] {
        let result = translate_native_no_retry_constructed_with_options(
            &vector_read_from(header),
            Stage::Kernel,
            TransformOptions::default(),
        );
        let err = result.expect_err("the loader must not skip the leading member");
        assert!(err.contains("reinterpret load"), "{header}: {err}");
    }
}
