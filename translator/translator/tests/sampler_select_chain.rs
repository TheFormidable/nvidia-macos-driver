//! xlate.paths-samplerphi (card round 11) when Apple's optimizer turns the switch that picks a sampler into a chain of
//! selects instead of a phi (tests/fixtures/app_shapes/samp1.ll is that shape for two samplers). The sampler pass
//! samples once through each candidate of a select whose arms are samplers or further selects (dispatch_texture.rs
//! lower_sampling_call), so a chain over 8, 9 or 16 [[sampler(n)]] arguments translates as written, with the
//! call-site attributes Apple's compiler writes, and needs no retry.
use metal2vulkan::passes::Stage;
use metal2vulkan::{disassemble, translate_sanitized_native};

const ATTRS: [&str; 3] = ["", "readonly captures(none) ", "nocapture readonly "];

// %sel = select (k == n-1), s(n-1), select (k == n-2), s(n-2), ... s0: the shape LLVM's SimplifyCFG leaves for a switch
// whose arms only choose a value
fn chain_kernel(n: usize, attrs: &str) -> String {
    let samplers: Vec<String> = (0..n).map(|j| format!("ptr addrspace(2) {attrs}%s{j}")).collect();
    let mut chain = String::new();
    let mut prev = "%s0".to_string();
    for j in 1..n {
        chain.push_str(&format!("  %c{j} = icmp eq i32 %kv, {j}\n"));
        chain.push_str(&format!("  %p{j} = select i1 %c{j}, ptr addrspace(2) %s{j}, ptr addrspace(2) {prev}\n"));
        prev = format!("%p{j}");
    }
    let mut md = Vec::new();
    md.push("!20 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 0, i32 1, !\"air.sample\", !\"air.arg_type_name\", !\"texture2d<float, sample>\", !\"air.arg_name\", !\"t\"}".to_string());
    for j in 0..n {
        md.push(format!("!{} = !{{i32 {}, !\"air.sampler\", !\"air.location_index\", i32 {j}, i32 1, !\"air.arg_type_name\", !\"sampler\", !\"air.arg_name\", !\"s{j}\"}}", 21 + j, 1 + j));
    }
    md.push(format!("!{} = !{{i32 {}, !\"air.buffer\", !\"air.buffer_size\", i32 4, !\"air.location_index\", i32 0, i32 1, !\"air.read\", !\"air.address_space\", i32 2, !\"air.arg_type_size\", i32 4, !\"air.arg_type_align_size\", i32 4, !\"air.arg_type_name\", !\"uint\", !\"air.arg_name\", !\"k\"}}", 21 + n, 1 + n));
    md.push(format!("!{} = !{{i32 {}, !\"air.buffer\", !\"air.location_index\", i32 1, i32 1, !\"air.read_write\", !\"air.address_space\", i32 1, !\"air.arg_type_size\", i32 16, !\"air.arg_type_align_size\", i32 16, !\"air.arg_type_name\", !\"float4\", !\"air.arg_name\", !\"o\"}}", 22 + n, 2 + n));
    let arg_list: Vec<String> = (20..23 + n).map(|i| format!("!{i}")).collect();
    format!(
        "\
target datalayout = \"e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32\"
target triple = \"spirv-unknown-vulkan1.2\"

define void @chain{n}(ptr addrspace(1) {attrs}%t, {}, ptr addrspace(2) noundef readonly align 4 captures(none) dereferenceable(4) \"air-buffer-no-alias\" %k, ptr addrspace(1) noundef writeonly align 16 captures(none) \"air-buffer-no-alias\" %o) local_unnamed_addr #0 {{
entry:
  %kv = load i32, ptr addrspace(2) %k, align 4
{chain}  %r = tail call {{ <4 x float>, i8 }} @air.sample_texture_2d.v4f32(ptr addrspace(1) {attrs}%t, ptr addrspace(2) {attrs}{prev}, <2 x float> splat (float 1.300000e+00), i1 true, <2 x i32> zeroinitializer, i1 true, float 0.000000e+00, float 0.000000e+00, i32 0) #1
  %v = extractvalue {{ <4 x float>, i8 }} %r, 0
  store <4 x float> %v, ptr addrspace(1) %o, align 16
  ret void
}}

declare {{ <4 x float>, i8 }} @air.sample_texture_2d.v4f32(ptr addrspace(1) {attrs}, ptr addrspace(2) {attrs}, <2 x float>, i1, <2 x i32>, i1, float, float, i32) local_unnamed_addr #1

attributes #0 = {{ convergent mustprogress nounwind willreturn \"frame-pointer\"=\"all\" }}
attributes #1 = {{ convergent nounwind willreturn memory(read) }}

!air.kernel = !{{!10}}
!air.version = !{{!11}}
!air.language_version = !{{!12}}

!10 = !{{ptr @chain{n}, !13, !14}}
!11 = !{{i32 2, i32 8, i32 0}}
!12 = !{{!\"Metal\", i32 4, i32 0, i32 0}}
!13 = !{{}}
!14 = !{{{}}}
{}
",
        samplers.join(", "),
        arg_list.join(", "),
        md.join("\n")
    )
}

fn tmp() -> std::path::PathBuf {
    let d = std::env::temp_dir().join(format!("m2v_sampler_select_chain_{}", std::process::id()));
    let _ = std::fs::create_dir_all(&d);
    d
}

#[test]
fn a_sampler_picked_by_a_select_chain_samples_once_per_candidate() {
    for attrs in ATTRS {
        for n in [2usize, 8, 9, 16] {
            let ll = chain_kernel(n, attrs);
            let spv = translate_sanitized_native(&ll, Stage::Kernel, &tmp())
                .unwrap_or_else(|e| panic!("n={n} attrs={attrs:?}: {e}"));
            let asm = disassemble(&spv).expect("disassemble");
            let samples = asm.lines().filter(|l| l.contains("OpImageSample")).count();
            assert_eq!(samples, n, "n={n} attrs={attrs:?}: one sample per candidate sampler:\n{asm}");
            assert!(asm.contains("OpSelect"), "n={n} attrs={attrs:?}: the chosen result is selected:\n{asm}");
        }
    }
}
