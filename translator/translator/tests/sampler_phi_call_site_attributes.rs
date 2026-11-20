//! xlate.paths-samplerphi (card round 11): a kernel sample through a sampler that a switch picks among 8, 9 or 16
//! [[sampler(n)]] arguments. Apple's compiler writes call-site parameter attributes between the operand's type and
//! its value (`ptr addrspace(2) readonly captures(none) %sel`, as in tests/fixtures/gl/texture_env_three_sites_behind_a_
//! switch.ll), and the sampler-phi split must find the operand through them. Without that the phi survives and the
//! kernel is refused.
use metal2vulkan::inline_and_forward_object_fields;
use metal2vulkan::passes::Stage;
use metal2vulkan::translate_sanitized_native;

const ATTRS: [&str; 3] = ["", "readonly captures(none) ", "nocapture readonly "];

fn pick_kernel(n: usize, attrs: &str) -> String {
    let samplers: Vec<String> = (0..n).map(|j| format!("ptr addrspace(2) {attrs}%s{j}")).collect();
    let cases: String = (1..n).map(|j| format!("    i32 {j}, label %c{j}\n")).collect();
    let blocks: String = (1..n).map(|j| format!("c{j}:\n  br label %after\n\n")).collect();
    let incoming: Vec<String> = std::iter::once("[ %s0, %entry ]".to_string())
        .chain((1..n).map(|j| format!("[ %s{j}, %c{j} ]")))
        .collect();
    let mut md = Vec::new();
    md.push(format!("!20 = !{{i32 0, !\"air.texture\", !\"air.location_index\", i32 0, i32 1, !\"air.sample\", !\"air.arg_type_name\", !\"texture2d<float, sample>\", !\"air.arg_name\", !\"t\"}}"));
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

define void @pick{n}(ptr addrspace(1) {attrs}%t, {}, ptr addrspace(2) noundef readonly align 4 captures(none) dereferenceable(4) \"air-buffer-no-alias\" %k, ptr addrspace(1) noundef writeonly align 16 captures(none) \"air-buffer-no-alias\" %o) local_unnamed_addr #0 {{
entry:
  %kv = load i32, ptr addrspace(2) %k, align 4
  switch i32 %kv, label %after [
{cases}  ]

{blocks}after:
  %sel = phi ptr addrspace(2) {}
  %r = tail call {{ <4 x float>, i8 }} @air.sample_texture_2d.v4f32(ptr addrspace(1) {attrs}%t, ptr addrspace(2) {attrs}%sel, <2 x float> splat (float 1.300000e+00), i1 true, <2 x i32> zeroinitializer, i1 true, float 0.000000e+00, float 0.000000e+00, i32 0) #1
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

!10 = !{{ptr @pick{n}, !13, !14}}
!11 = !{{i32 2, i32 8, i32 0}}
!12 = !{{!\"Metal\", i32 4, i32 0, i32 0}}
!13 = !{{}}
!14 = !{{{}}}
{}
",
        samplers.join(", "),
        incoming.join(", "),
        arg_list.join(", "),
        md.join("\n")
    )
}

fn tmp() -> std::path::PathBuf {
    let d = std::env::temp_dir().join(format!("m2v_sampler_phi_attrs_{}", std::process::id()));
    let _ = std::fs::create_dir_all(&d);
    d
}

// The repair the plugin's translator runs on a refused module (wrapper/src/lib.rs translate_with_pointer_retry):
// translate as written; on failure, inline and split, then translate again.
fn translate_with_retry(ll: &str) -> Result<Vec<u8>, String> {
    match translate_sanitized_native(ll, Stage::Kernel, &tmp()) {
        Ok(v) => Ok(v),
        Err(e) => {
            let Some(retry) = inline_and_forward_object_fields(ll) else { return Err(format!("{e}; no retry form")) };
            translate_sanitized_native(&retry, Stage::Kernel, &tmp()).map_err(|e2| format!("{e}; retry: {e2}"))
        }
    }
}

#[test]
fn the_split_finds_the_sampler_through_call_site_attributes() {
    for attrs in ATTRS {
        for n in [8usize, 9, 16] {
            let ll = pick_kernel(n, attrs);
            let out = inline_and_forward_object_fields(&ll).unwrap_or_else(|| panic!("n={n} attrs={attrs:?}: no change"));
            assert!(!out.contains("phi ptr addrspace(2)"), "n={n} attrs={attrs:?}:\n{out}");
            assert_eq!(out.matches("call { <4 x float>, i8 } @air.sample_texture_2d.v4f32(").count(), n, "n={n} attrs={attrs:?}");
            for j in 0..n {
                let operand = format!("ptr addrspace(2) {attrs}%s{j}, <2 x float>");
                assert_eq!(out.matches(&operand).count(), 1, "n={n} attrs={attrs:?}: one sample through %s{j}:\n{out}");
            }
        }
        // above a stage's 16 samplers the pick stays as written (and refused)
        let ll = pick_kernel(17, attrs);
        let out = inline_and_forward_object_fields(&ll).unwrap_or(ll);
        assert!(out.contains("phi ptr addrspace(2)"), "attrs={attrs:?}: 17 must stay refused");
    }
}

#[test]
fn a_kernel_sampling_through_a_picked_sampler_translates() {
    for attrs in ATTRS {
        for n in [8usize, 9, 16] {
            let ll = pick_kernel(n, attrs);
            match translate_with_retry(&ll) {
                Ok(_) => {}
                // this host has no spirv-val: the module was emitted and only its validation could not run
                Err(e) if e.ends_with("cannot run spirv-val (spirv-val) [no verdict]: No such file or directory (os error 2)")
                    && e.contains("; retry: ") && !e["; retry: ".len() + e.find("; retry: ").unwrap()..].contains("sampler") =>
                    eprintln!("n={n} attrs={attrs:?}: emitted, not validated (no spirv-val here)"),
                Err(e) => panic!("n={n} attrs={attrs:?}: {e}"),
            }
        }
    }
}

// The attribute between the type and the value can itself hold a comma (`captures(address, provenance)`); the
// operand must still be found, and an operand that is not the sampler (the texture) must not be taken for it.
#[test]
fn an_attribute_with_a_comma_inside_parentheses_is_one_attribute() {
    let ll = pick_kernel(8, "readonly captures(address, provenance) ");
    let out = inline_and_forward_object_fields(&ll).expect("split");
    assert!(!out.contains("phi ptr addrspace(2)"), "{out}");
    for j in 0..8 {
        assert!(out.contains(&format!("ptr addrspace(2) readonly captures(address, provenance) %s{j}, <2 x float>")), "{out}");
    }
}
