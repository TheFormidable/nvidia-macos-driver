use metal2vulkan::inline_and_forward_object_fields;

fn sampler_phi(count: usize) -> String {
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
  %result = call {{ float, i8 }} @air.sample_compare_depth_2d.f32(ptr addrspace(1) %tex, ptr addrspace(2) %selected, i32 1)
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
