//! xlate.paths-walk (card round 11): a loop that picks its next buffer through a pointer phi on its back edge,
//! `p = (s & 1) ? b : a`. Under Logical addressing each buffer is its own descriptor and the select between them is
//! refused ("cannot reinterpret pointer for owned OpSelect type mismatch"); the construction with device addresses
//! carries it as an address phi and an address select.
use metal2vulkan::passes::Stage;
use metal2vulkan::passes::TransformOptions;

// tools/card/xlate_paths.m case_walk in the shape clang -O2 leaves it (see the fixture's header)
fn walk() -> String {
    include_str!("fixtures/app_shapes/walk.ll").to_string()
}

// NVMTL_XLATE_ADDR_RETRY=0: the module stays refused as before (its own test binary: the switch is read once)
#[test]
fn the_switch_off_keeps_the_refusal() {
    std::env::set_var("NVMTL_XLATE_ADDR_RETRY", "0");
    // inside the scope the plugin's wrapper enters for its last resort, and the wrapper does not even enter it
    let e = metal2vulkan::with_device_address_last_resort(|| {
        metal2vulkan::translate_native_no_retry_constructed_with_options(&walk(), Stage::Kernel, TransformOptions::default())
    })
    .expect_err("refused with the retry off");
    assert!(e.contains("cannot reinterpret pointer for owned OpSelect type mismatch") && !e.contains("device-address"), "{e}");
    assert!(!metal2vulkan::device_address_last_resort_applies(&e), "{e}");
}
