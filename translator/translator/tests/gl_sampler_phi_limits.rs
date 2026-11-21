use metal2vulkan::inline_and_forward_object_fields;

fn sampler_phi(count: usize) -> String {
    sampler_phi_with(count, "")
}

// `attrs`: call-site parameter attributes between the sampler operand's type and its value, as Apple's compiler
// writes them (`readonly captures(none)`; `nocapture readonly` before LLVM 21)
fn sampler_phi_with(count: usize, attrs: &str) -> String {
    let params: Vec<_> = (0..count).map(|i| format!("ptr addrspace(2) %s{i}")).collect();
    let cases: String = (1..count).map(|i| format!("    i32 {i}, label %c{i}\n")).collect();
    let blocks: String = (1..count).map(|i| format!("c{i}:\n  br label %after\n")).collect();
    let incoming: Vec<_> = std::iter::once("[ %s0, %entry ]".to_string())
        .chain((1..count).map(|i| format!("[ %s{i}, %c{i} ]"))).collect();
    format!("\
define {{ float, i8 }} @main(ptr addrspace(1) %tex, {}, i32 %mode) {{
entry:
  switch i32 %mode, label %after [
{cases}  ]
{blocks}after:
  %selected = phi ptr addrspace(2) {}
  %result = call {{ float, i8 }} @air.sample_compare_depth_2d.f32(ptr addrspace(1) %tex, ptr addrspace(2) {attrs}%selected, i32 1)
  ret {{ float, i8 }} %result
}}
", params.join(", "), incoming.join(", "))
}

#[test]
fn sampler_retry_covers_nine_through_sixteen_states_and_stays_bounded() {
    for count in [2, 8, 9, 16, 17] {
        let input = sampler_phi(count);
        let output = inline_and_forward_object_fields(&input).unwrap_or(input);
        if count <= 16 {
            assert!(!output.contains("phi ptr addrspace(2)"), "{count} samplers:\n{output}");
            assert_eq!(output.matches("@air.sample_compare_depth_2d.f32(").count(), count);
            for i in 0..count {
                assert!(output.contains(&format!("ptr addrspace(2) %s{i}, i32 1)")));
            }
        } else {
            assert!(output.contains("phi ptr addrspace(2)"), "fan-out above the bound must stay refused");
            assert_eq!(output.matches("@air.sample_compare_depth_2d.f32(").count(), 1);
        }
    }
}

#[test]
fn sampler_retry_sees_through_call_site_attributes() {
    for attrs in ["readonly captures(none) ", "nocapture readonly ", "noundef readonly captures(address, provenance) "] {
        for count in [2, 8, 9, 16, 17] {
            let input = sampler_phi_with(count, attrs);
            let output = inline_and_forward_object_fields(&input).unwrap_or(input);
            if count <= 16 {
                assert!(!output.contains("phi ptr addrspace(2)"), "{count} samplers, {attrs:?}:\n{output}");
                assert_eq!(output.matches("@air.sample_compare_depth_2d.f32(").count(), count);
                for i in 0..count {
                    assert!(output.contains(&format!("ptr addrspace(2) {attrs}%s{i}, i32 1)")), "{attrs:?} %s{i}:\n{output}");
                }
            } else {
                assert!(output.contains("phi ptr addrspace(2)"), "fan-out above the bound must stay refused");
                assert_eq!(output.matches("@air.sample_compare_depth_2d.f32(").count(), 1);
            }
        }
    }
}

// The phi as an operand that is not a sampler (`ptr addrspace(1)` texture slot) is no sampler read: left as written.
#[test]
fn sampler_retry_leaves_a_phi_in_another_operand_slot() {
    let input = sampler_phi(8).replace("ptr addrspace(2) %selected, i32 1)", "ptr addrspace(2) %s0, i32 1)")
        .replace("ptr addrspace(1) %tex, ", "ptr addrspace(1) readonly %selected, ");
    let output = inline_and_forward_object_fields(&input).unwrap_or(input);
    assert!(output.contains("phi ptr addrspace(2)"), "{output}");
}
