//! xlate.paths-walk (card round 11): a loop that picks its next buffer through a pointer phi on its back edge,
//! `p = (s & 1) ? b : a`. Under Logical addressing each buffer is its own descriptor and the select between them is
//! refused ("cannot reinterpret pointer for owned OpSelect type mismatch"); the construction with device addresses
//! carries it as an address phi and an address select. That construction is a last resort: the translator builds it
//! only inside with_device_address_last_resort, which the plugin's wrapper enters once every attempt it makes for a
//! module has failed.
use metal2vulkan::passes::Stage;
use metal2vulkan::passes::TransformOptions;

// tools/card/xlate_paths.m case_walk in the shape clang -O2 leaves it (see the fixture's header)
fn walk() -> String {
    include_str!("fixtures/app_shapes/walk.ll").to_string()
}

fn tmp() -> std::path::PathBuf {
    let d = std::env::temp_dir().join(format!("m2v_xlate_paths_walk_{}", std::process::id()));
    let _ = std::fs::create_dir_all(&d);
    d
}

#[test]
fn a_loop_carried_select_between_two_buffers_translates_through_addresses() {
    let spv = metal2vulkan::with_device_address_last_resort(|| {
        metal2vulkan::translate_native_no_retry_constructed_with_options(&walk(), Stage::Kernel, TransformOptions::default())
    })
    .expect("the walk kernel must translate");
    // the production entry does not validate; spirv-val does, as the translator's own retry path does
    metal2vulkan::tools::spirv_val_bytes(&spv, &tmp()).expect("the device-address module is valid Vulkan 1.2 SPIR-V");
    let asm = metal2vulkan::disassemble(&spv).expect("disassemble");
    assert!(asm.contains("OpMemoryModel PhysicalStorageBuffer64"), "{asm}");
    assert!(asm.contains("Binding 640"), "the address table:\n{asm}");
    // the pointer phi and the select are over 64-bit addresses, not over descriptor pointers
    let int64 = asm.lines().find(|l| l.ends_with("= OpTypeInt 64 0")).and_then(|l| l.split_whitespace().next()).expect("i64");
    assert!(asm.lines().any(|l| l.contains("= OpPhi  ") && l.contains(&format!("OpPhi  {int64} "))), "{asm}");
    assert!(asm.lines().any(|l| l.contains(&format!("OpSelect  {int64} "))), "{asm}");
    assert!(!asm.lines().any(|l| l.contains("= OpSelect ") && !l.contains(&format!("OpSelect  {int64} "))), "no pointer select left:\n{asm}");
}

// A module that translates as written never reaches the retry: a single-buffer loop keeps its descriptor form.
#[test]
fn a_loop_over_one_buffer_keeps_its_descriptors() {
    let ll = walk().replace("%next = select i1 %c, ptr addrspace(1) %a, ptr addrspace(1) %b", "%next = select i1 %c, ptr addrspace(1) %a, ptr addrspace(1) %a");
    let spv = metal2vulkan::with_device_address_last_resort(|| {
        metal2vulkan::translate_native_no_retry_constructed_with_options(&ll, Stage::Kernel, TransformOptions::default())
    })
    .expect("one buffer");
    let asm = metal2vulkan::disassemble(&spv).expect("disassemble");
    assert!(!asm.contains("PhysicalStorageBuffer64"), "{asm}");
}

// The validating entry (translate_sanitized_native runs spirv-val on what it constructs) takes the same retry.
#[test]
fn the_validating_entry_constructs_the_same_kernel_with_addresses() {
    let spv = metal2vulkan::with_device_address_last_resort(|| metal2vulkan::translate_sanitized_native(&walk(), Stage::Kernel, &tmp()))
        .expect("validated walk kernel");
    let asm = metal2vulkan::disassemble(&spv).expect("disassemble");
    assert!(asm.contains("OpMemoryModel PhysicalStorageBuffer64") && asm.contains("Binding 640"), "{asm}");
}

// Outside the last-resort scope both entries refuse the kernel exactly as before the retry existed, so a caller that
// goes on to another attempt after this refusal still reaches it; the scope ends with its closure.
#[test]
fn outside_the_last_resort_the_refusal_stands() {
    for _ in 0..2 {
        assert!(!metal2vulkan::device_address_last_resort_active());
        let e = metal2vulkan::translate_native_no_retry_constructed_with_options(&walk(), Stage::Kernel, TransformOptions::default())
            .expect_err("refused outside the last resort");
        assert!(e.contains("cannot reinterpret pointer for owned OpSelect type mismatch") && !e.contains("device-address"), "{e}");
        assert!(metal2vulkan::device_address_last_resort_applies(&e), "{e}");
        let e = metal2vulkan::translate_sanitized_native(&walk(), Stage::Kernel, &tmp()).expect_err("validating entry refuses too");
        assert!(metal2vulkan::device_address_last_resort_applies(&e) && !e.contains("device-address"), "{e}");
        metal2vulkan::with_device_address_last_resort(|| assert!(metal2vulkan::device_address_last_resort_active()));
    }
}
