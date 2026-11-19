//! From pull request 524 of the original project (its iOS Simulator fixtures): a helper that views
//! a local union through a dynamically indexed byte view keeps that index as an index into a byte
//! array, so the module translates and validates. The translator of 3.0.4, and this one with
//! NVMTL_WHOLE_VIEWS=0, refuses six of the ten fixtures: the dynamic index lands on a member of the
//! union's struct, where SPIR-V takes only a constant. The fixtures are synthetic LLVM, translated
//! on the CPU; nothing here runs a GPU. The pull request validated with an outside spirv-val named
//! by METAL2VULKAN_SPIRV_VAL; here the validator of the spirv-tools crate the wrapper's own tests
//! use is run in the process, so the test needs nothing set but NVMTL_WHOLE_VIEWS unset (on).
use metal2vulkan::passes::{Stage, TransformOptions};
use rspirv::dr::{load_bytes, Operand};
use rspirv::spirv::Op;
use spirv_tools::val::Validator;
use std::collections::HashMap;

fn translate(ll: &str) -> Result<Vec<u8>, String> {
    let bytes = metal2vulkan::translate_native_no_retry_constructed_with_options(
        ll,
        Stage::Kernel,
        TransformOptions::default(),
    )
    .map_err(|e| format!("does not translate: {e:?}"))?;
    let words: Vec<u32> = bytes
        .chunks_exact(4)
        .map(|c| u32::from_le_bytes(c.try_into().unwrap()))
        .collect();
    spirv_tools::val::create(Some(spirv_tools::TargetEnv::Vulkan_1_2))
        .validate(&words, None)
        .map_err(|e| format!("is not valid Vulkan 1.2 SPIR-V: {e:?}"))?;
    Ok(bytes)
}

/// The module indexes an access chain with the helper's widened dynamic index (an OpUConvert).
fn keeps_dynamic_index(bytes: &[u8]) -> bool {
    let module = load_bytes(bytes).unwrap();
    let defs: HashMap<_, _> = module
        .all_inst_iter()
        .filter_map(|i| i.result_id.map(|id| (id, i)))
        .collect();
    let found = module.all_inst_iter().any(|i| {
        matches!(i.class.opcode, Op::AccessChain | Op::InBoundsAccessChain)
            && i.operands.iter().skip(1).any(|o| {
                matches!(o, Operand::IdRef(id)
                    if defs.get(id).is_some_and(|d| d.class.opcode == Op::UConvert))
            })
    });
    found
}

#[test]
fn simulator_union_helper_byte_views_keep_dynamic_element_indices() {
    let inputs = [
        (
            "byte_array-helper_array",
            include_str!("fixtures/simulator/byte_array-helper_array.ll"),
        ),
        (
            "byte_array-helper_byte",
            include_str!("fixtures/simulator/byte_array-helper_byte.ll"),
        ),
        (
            "byte_union-helper_array",
            include_str!("fixtures/simulator/byte_union-helper_array.ll"),
        ),
        (
            "byte_union-helper_byte",
            include_str!("fixtures/simulator/byte_union-helper_byte.ll"),
        ),
        (
            "scalar_union-helper_array",
            include_str!("fixtures/simulator/scalar_union-helper_array.ll"),
        ),
        (
            "scalar_union-helper_byte",
            include_str!("fixtures/simulator/scalar_union-helper_byte.ll"),
        ),
        (
            "vec_union-helper_array",
            include_str!("fixtures/simulator/vec_union-helper_array.ll"),
        ),
        (
            "vec_union-helper_byte",
            include_str!("fixtures/simulator/vec_union-helper_byte.ll"),
        ),
        (
            "word_union-helper_array",
            include_str!("fixtures/simulator/word_union-helper_array.ll"),
        ),
        (
            "word_union-helper_byte",
            include_str!("fixtures/simulator/word_union-helper_byte.ll"),
        ),
    ];
    let mut failed = Vec::new();
    for (name, ll) in inputs {
        match translate(ll) {
            Ok(bytes) if keeps_dynamic_index(&bytes) => {}
            Ok(_) => failed.push(format!(
                "{name}: the dynamic array element became member zero"
            )),
            Err(e) => failed.push(format!("{name}: {e}")),
        }
    }
    assert!(
        failed.is_empty(),
        "{} of {} fixtures:\n{}",
        failed.len(),
        inputs.len(),
        failed.join("\n")
    );
}
