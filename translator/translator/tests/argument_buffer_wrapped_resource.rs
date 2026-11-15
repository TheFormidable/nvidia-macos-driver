use metal2vulkan::meta::parse_air_kernel_meta;
use metal2vulkan::passes::Stage;
use metal2vulkan::{disassemble, translate_sanitized_native};
use std::path::PathBuf;

fn tmp() -> PathBuf {
    let d = std::env::temp_dir().join(format!("m2v_wrapped_arg_{}", std::process::id()));
    let _ = std::fs::create_dir_all(&d);
    d
}

const FLAT: &str = r#"target triple = "air64_v28-apple-macosx26.5.0"

%Args = type <{ %"struct.metal::texture2d", i16, [6 x i8] }>
%"struct.metal::texture2d" = type { ptr addrspace(1) }

define void @k(ptr addrspace(2) %args, <2 x i32> %coord) local_unnamed_addr #0 {
entry:
  %field = getelementptr inbounds %Args, ptr addrspace(2) %args, i64 0, i32 0, i32 0
  %tex = load ptr addrspace(1), ptr addrspace(2) %field, align 8
  tail call void @air.write_texture_2d.v4f32(ptr addrspace(1) %tex, <2 x i32> %coord, <4 x float> zeroinitializer, i32 0, i32 2) #3
  ret void
}

declare void @air.write_texture_2d.v4f32(ptr addrspace(1), <2 x i32>, <4 x float>, i32, i32) local_unnamed_addr #3

attributes #0 = { convergent nounwind }
attributes #3 = { convergent nounwind memory(argmem: write) }

!air.kernel = !{!0}
!0 = !{ptr @k, !1, !2}
!1 = !{}
!2 = !{!3, !7}
!3 = !{i32 0, !"air.indirect_buffer", !"air.buffer_size", i32 16, !"air.location_index", i32 0, i32 1, !"air.read", !"air.address_space", i32 2, !"air.struct_type_info", !4, !"air.arg_type_name", !"Args", !"air.arg_name", !"args"}
!4 = !{i32 0, i32 8, i32 0, !"texture2d<float, write>", !"output", !"air.indirect_argument", !5, i32 8, i32 2, i32 0, !"short", !"radius", !"air.indirect_argument", !6}
!5 = !{i32 0, !"air.texture", !"air.location_index", i32 0, i32 1, !"air.write", !"air.arg_type_name", !"texture2d<float, write>", !"air.arg_name", !"output"}
!6 = !{i32 1, !"air.indirect_constant", !"air.location_index", i32 1, i32 1, !"air.arg_type_name", !"short", !"air.arg_name", !"radius"}
!7 = !{i32 1, !"air.thread_position_in_grid", !"air.arg_type_name", !"uint2", !"air.arg_name", !"coord"}
"#;

const WRAPPED: &str = r#"target triple = "air64_v28-apple-macosx26.5.0"

%Args = type <{ %Wrapper, i16, [6 x i8] }>
%Wrapper = type { %"struct.metal::texture2d" }
%"struct.metal::texture2d" = type { ptr addrspace(1) }

define void @k(ptr addrspace(2) %args, <2 x i32> %coord) local_unnamed_addr #0 {
entry:
  %field = getelementptr inbounds %Args, ptr addrspace(2) %args, i64 0, i32 0, i32 0, i32 0
  %tex = load ptr addrspace(1), ptr addrspace(2) %field, align 8
  tail call void @air.write_texture_2d.v4f32(ptr addrspace(1) %tex, <2 x i32> %coord, <4 x float> zeroinitializer, i32 0, i32 2) #3
  ret void
}

declare void @air.write_texture_2d.v4f32(ptr addrspace(1), <2 x i32>, <4 x float>, i32, i32) local_unnamed_addr #3

attributes #0 = { convergent nounwind }
attributes #3 = { convergent nounwind memory(argmem: write) }

!air.kernel = !{!0}
!0 = !{ptr @k, !1, !2}
!1 = !{}
!2 = !{!3, !7}
!3 = !{i32 0, !"air.indirect_buffer", !"air.buffer_size", i32 16, !"air.location_index", i32 0, i32 1, !"air.read", !"air.address_space", i32 2, !"air.struct_type_info", !4, !"air.arg_type_name", !"Args", !"air.arg_name", !"args"}
!4 = !{!"air.struct_type_info", !8, i32 0, i32 8, i32 0, !"texture2d_wrapper", !"wrapper", !"air.indirect_argument", i32 7, i32 8, i32 2, i32 0, !"short", !"radius", !"air.indirect_argument", !6}
!8 = !{i32 0, i32 8, i32 0, !"texture2d<float, write>", !"output", !"air.indirect_argument", !5}
!5 = !{i32 0, !"air.texture", !"air.location_index", i32 3, i32 1, !"air.write", !"air.arg_type_name", !"texture2d<float, write>", !"air.arg_name", !"output"}
!6 = !{i32 1, !"air.indirect_constant", !"air.location_index", i32 1, i32 1, !"air.arg_type_name", !"short", !"air.arg_name", !"radius"}
!7 = !{i32 1, !"air.thread_position_in_grid", !"air.arg_type_name", !"uint2", !"air.arg_name", !"coord"}
"#;

const WRAPPED_SAMPLE: &str = r#"target triple = "air64_v28-apple-macosx26.5.0"

%Args = type <{ %Wrapper }>
%Wrapper = type { %"struct.metal::texture2d" }
%"struct.metal::texture2d" = type { ptr addrspace(1) }

define <4 x float> @k(ptr addrspace(2) %args, ptr addrspace(2) %smp) local_unnamed_addr #0 {
entry:
  %tf = getelementptr inbounds %Args, ptr addrspace(2) %args, i64 0, i32 0, i32 0, i32 0
  %tex = load ptr addrspace(1), ptr addrspace(2) %tf, align 8
  %s = call { <4 x float>, i8 } @air.sample_texture_2d.v4f32(ptr addrspace(1) %tex, ptr addrspace(2) %smp, <2 x float> zeroinitializer, i1 true, <2 x i32> zeroinitializer, i1 false, float 0.000000e+00, float 0.000000e+00, i32 0)
  %c = extractvalue { <4 x float>, i8 } %s, 0
  ret <4 x float> %c
}

declare { <4 x float>, i8 } @air.sample_texture_2d.v4f32(ptr addrspace(1), ptr addrspace(2), <2 x float>, i1, <2 x i32>, i1, float, float, i32)

attributes #0 = { convergent nounwind }

!air.fragment = !{!0}
!0 = !{ptr @k, !1, !2}
!1 = !{!9}
!9 = !{!"air.render_target", i32 0, i32 0, !"air.arg_type_name", !"float4"}
!2 = !{!3, !6}
!3 = !{i32 0, !"air.indirect_buffer", !"air.buffer_size", i32 8, !"air.location_index", i32 0, i32 1, !"air.read", !"air.address_space", i32 2, !"air.struct_type_info", !4, !"air.arg_type_name", !"Args", !"air.arg_name", !"args"}
!4 = !{!"air.struct_type_info", !8, i32 0, i32 8, i32 0, !"texture2d_wrapper", !"wrapper", !"air.indirect_argument", i32 0}
!8 = !{i32 0, i32 8, i32 0, !"texture2d<float, sample>", !"tex", !"air.indirect_argument", !5}
!5 = !{i32 0, !"air.texture", !"air.location_index", i32 0, i32 1, !"air.sample", !"air.arg_type_name", !"texture2d<float, sample>", !"air.arg_name", !"tex"}
!6 = !{i32 1, !"air.sampler", !"air.location_index", i32 0, i32 1, !"air.arg_type_name", !"sampler", !"air.arg_name", !"smp"}
"#;

const WRAPPED_1D: &str = r#"target triple = "air64_v28-apple-macosx26.5.0"

%Args = type <{ %Wrapper }>
%Wrapper = type { %"struct.metal::texture1d" }
%"struct.metal::texture1d" = type { ptr addrspace(1) }

define <4 x float> @k(ptr addrspace(2) %args, ptr addrspace(2) %smp) local_unnamed_addr #0 {
entry:
  %tf = getelementptr inbounds %Args, ptr addrspace(2) %args, i64 0, i32 0, i32 0, i32 0
  %tex = load ptr addrspace(1), ptr addrspace(2) %tf, align 8
  %s = call { <4 x float>, i8 } @air.sample_texture_1d.v4f32(ptr addrspace(1) %tex, ptr addrspace(2) %smp, float 0.000000e+00, i1 false, float 0.000000e+00, i32 0)
  %c = extractvalue { <4 x float>, i8 } %s, 0
  ret <4 x float> %c
}

declare { <4 x float>, i8 } @air.sample_texture_1d.v4f32(ptr addrspace(1), ptr addrspace(2), float, i1, float, i32)

attributes #0 = { convergent nounwind }

!air.fragment = !{!0}
!0 = !{ptr @k, !1, !2}
!1 = !{!9}
!9 = !{!"air.render_target", i32 0, i32 0, !"air.arg_type_name", !"float4"}
!2 = !{!3, !6}
!3 = !{i32 0, !"air.indirect_buffer", !"air.buffer_size", i32 8, !"air.location_index", i32 0, i32 1, !"air.read", !"air.address_space", i32 2, !"air.struct_type_info", !4, !"air.arg_type_name", !"Args", !"air.arg_name", !"args"}
!4 = !{!"air.struct_type_info", !8, i32 0, i32 8, i32 0, !"texture1d_wrapper", !"wrapper", !"air.indirect_argument", i32 0}
!8 = !{i32 0, i32 8, i32 0, !"texture1d<float, sample>", !"tex", !"air.indirect_argument", !5}
!5 = !{i32 0, !"air.texture", !"air.location_index", i32 0, i32 1, !"air.sample", !"air.arg_type_name", !"texture1d<float, sample>", !"air.arg_name", !"tex"}
!6 = !{i32 1, !"air.sampler", !"air.location_index", i32 0, i32 1, !"air.arg_type_name", !"sampler", !"air.arg_name", !"smp"}
"#;

const WRAPPED_ARRAY: &str = r#"target triple = "air64_v28-apple-macosx26.5.0"

%Args = type <{ [2 x %Wrapper] }>
%Wrapper = type { %"struct.metal::texture2d" }
%"struct.metal::texture2d" = type { ptr addrspace(1) }

define <4 x float> @k(ptr addrspace(2) %args, ptr addrspace(2) %smp) local_unnamed_addr #0 {
entry:
  %tf = getelementptr inbounds %Args, ptr addrspace(2) %args, i64 0, i32 0, i32 0, i32 0, i32 0
  %tex = load ptr addrspace(1), ptr addrspace(2) %tf, align 8
  %s = call { <4 x float>, i8 } @air.sample_texture_2d.v4f32(ptr addrspace(1) %tex, ptr addrspace(2) %smp, <2 x float> zeroinitializer, i1 true, <2 x i32> zeroinitializer, i1 false, float 0.000000e+00, float 0.000000e+00, i32 0)
  %c = extractvalue { <4 x float>, i8 } %s, 0
  ret <4 x float> %c
}

declare { <4 x float>, i8 } @air.sample_texture_2d.v4f32(ptr addrspace(1), ptr addrspace(2), <2 x float>, i1, <2 x i32>, i1, float, float, i32)

attributes #0 = { convergent nounwind }

!air.fragment = !{!0}
!0 = !{ptr @k, !1, !2}
!1 = !{!9}
!9 = !{!"air.render_target", i32 0, i32 0, !"air.arg_type_name", !"float4"}
!2 = !{!3, !6}
!3 = !{i32 0, !"air.indirect_buffer", !"air.buffer_size", i32 16, !"air.location_index", i32 0, i32 1, !"air.read", !"air.address_space", i32 2, !"air.struct_type_info", !4, !"air.arg_type_name", !"Args", !"air.arg_name", !"args"}
!4 = !{!"air.struct_type_info", !8, i32 0, i32 8, i32 2, !"texture2d_wrapper", !"wrappers", !"air.indirect_argument", i32 0}
!8 = !{i32 0, i32 8, i32 0, !"texture2d<float, sample>", !"tex", !"air.indirect_argument", !5}
!5 = !{i32 0, !"air.texture", !"air.location_index", i32 0, i32 1, !"air.sample", !"air.arg_type_name", !"texture2d<float, sample>", !"air.arg_name", !"tex"}
!6 = !{i32 1, !"air.sampler", !"air.location_index", i32 0, i32 1, !"air.arg_type_name", !"sampler", !"air.arg_name", !"smp"}
"#;

const FLAT_C_ARRAY: &str = r#"target triple = "air64_v28-apple-macosx26.5.0"

%Args = type { [32 x %"struct.metal::texture2d"] }
%"struct.metal::texture2d" = type { ptr addrspace(1) }

define <4 x float> @k(ptr addrspace(2) %args, ptr addrspace(2) %smp, i32 %slice) local_unnamed_addr #0 {
entry:
  %w = zext i32 %slice to i64
  %tf = getelementptr inbounds %Args, ptr addrspace(2) %args, i64 0, i32 0, i64 %w, i32 0
  %tex = load ptr addrspace(1), ptr addrspace(2) %tf, align 8
  %s = call { <4 x float>, i8 } @air.sample_texture_2d.v4f32(ptr addrspace(1) %tex, ptr addrspace(2) %smp, <2 x float> zeroinitializer, i1 true, <2 x i32> zeroinitializer, i1 false, float 0.000000e+00, float 0.000000e+00, i32 0)
  %c = extractvalue { <4 x float>, i8 } %s, 0
  ret <4 x float> %c
}

declare { <4 x float>, i8 } @air.sample_texture_2d.v4f32(ptr addrspace(1), ptr addrspace(2), <2 x float>, i1, <2 x i32>, i1, float, float, i32)

attributes #0 = { convergent nounwind }

!air.fragment = !{!0}
!0 = !{ptr @k, !1, !2}
!1 = !{!9}
!9 = !{!"air.render_target", i32 0, i32 0, !"air.arg_type_name", !"float4"}
!2 = !{!3, !6, !7}
!3 = !{i32 0, !"air.indirect_buffer", !"air.buffer_size", i32 256, !"air.location_index", i32 0, i32 1, !"air.read", !"air.address_space", i32 2, !"air.struct_type_info", !4, !"air.arg_type_name", !"Args", !"air.arg_name", !"args"}
!4 = !{i32 0, i32 8, i32 32, !"texture2d<float, sample>", !"slices", !"air.indirect_argument", !5}
!5 = !{i32 0, !"air.texture", !"air.location_index", i32 0, i32 1, !"air.sample", !"air.arg_type_name", !"texture2d<float, sample>", !"air.arg_name", !"slices"}
!6 = !{i32 1, !"air.sampler", !"air.location_index", i32 0, i32 1, !"air.arg_type_name", !"sampler", !"air.arg_name", !"smp"}
!7 = !{i32 2, !"air.fragment_input", !"generated(5slicej)", !"air.flat", !"air.arg_type_name", !"uint", !"air.arg_name", !"slice"}
"#;

const FLAT_TEXTURE_BUFFER: &str = r#"target triple = "air64_v28-apple-macosx26.5.0"

%Args = type { %"struct.metal::texture_buffer" }
%"struct.metal::texture_buffer" = type { ptr addrspace(1) }

define <4 x float> @k(ptr addrspace(2) %args) local_unnamed_addr #0 {
entry:
  %tf = getelementptr inbounds %Args, ptr addrspace(2) %args, i64 0, i32 0, i32 0
  %tex = load ptr addrspace(1), ptr addrspace(2) %tf, align 8
  %r = tail call { <4 x float>, i8 } @air.read_texture_buffer_1d.v4f32(ptr addrspace(1) %tex, i32 3, i32 1)
  %c = extractvalue { <4 x float>, i8 } %r, 0
  ret <4 x float> %c
}

declare { <4 x float>, i8 } @air.read_texture_buffer_1d.v4f32(ptr addrspace(1), i32, i32)

attributes #0 = { convergent nounwind }

!air.fragment = !{!0}
!0 = !{ptr @k, !1, !2}
!1 = !{!9}
!9 = !{!"air.render_target", i32 0, i32 0, !"air.arg_type_name", !"float4"}
!2 = !{!3}
!3 = !{i32 0, !"air.indirect_buffer", !"air.buffer_size", i32 8, !"air.location_index", i32 0, i32 1, !"air.read", !"air.address_space", i32 2, !"air.struct_type_info", !4, !"air.arg_type_name", !"Args", !"air.arg_name", !"args"}
!4 = !{i32 0, i32 8, i32 0, !"texture_buffer<float, read>", !"texels", !"air.indirect_argument", !5}
!5 = !{i32 0, !"air.texture", !"air.location_index", i32 0, i32 1, !"air.read", !"air.arg_type_name", !"texture_buffer<float, read>", !"air.arg_name", !"texels"}
"#;

#[test]
fn a_flat_embedded_texture_still_binds() {
    let spv = translate_sanitized_native(FLAT, Stage::Kernel, &tmp()).expect("translate");
    let asm = disassemble(&spv).expect("disassemble");
    assert!(
        asm.contains("Binding 480") && asm.contains("OpImageWrite"),
        "the flat member is surfaced as a storage image and written:\n{asm}"
    );
    let meta = parse_air_kernel_meta(FLAT).expect("parse");
    assert!(
        meta.unsurfaced_embedded_resources.is_empty(),
        "nothing about the flat form is unsurfaced: {:?}",
        meta.unsurfaced_embedded_resources
    );
}

#[test]
fn a_wrapped_embedded_texture_binds_at_the_summed_argument_id() {
    let meta = parse_air_kernel_meta(WRAPPED).expect("parse");
    assert!(
        meta.unsurfaced_embedded_resources.is_empty(),
        "a wrapped 2D texture is surfaced, not refused: {:?}",
        meta.unsurfaced_embedded_resources
    );
    let texture = match meta.embedded_textures.as_slice() {
        [only] => *only,
        other => panic!("exactly one embedded texture, got {other:?}"),
    };
    assert_eq!(texture.argument_index, 10);
    assert_eq!(texture.field_offset, 0);
    assert_eq!(texture.field_ordinal, 0);

    let spv = translate_sanitized_native(WRAPPED, Stage::Kernel, &tmp()).expect("translate");
    let asm = disassemble(&spv).expect("disassemble");
    assert!(
        asm.contains("Binding 480") && asm.contains("OpImageWrite"),
        "the wrapped member is surfaced as a storage image and written:\n{asm}"
    );
}

#[test]
fn a_wrapped_embedded_texture_samples_its_own_image() {
    let spv =
        translate_sanitized_native(WRAPPED_SAMPLE, Stage::Fragment, &tmp()).expect("translate");
    let asm = disassemble(&spv).expect("disassemble");
    assert!(
        asm.contains("OpSampledImage") && asm.contains("OpImageSample"),
        "the wrapped texture is sampled through its own descriptor:\n{asm}"
    );
}

#[test]
fn a_wrapped_texture_keeps_the_dimension_its_type_name_names() {
    let spv = translate_sanitized_native(WRAPPED_1D, Stage::Fragment, &tmp()).expect("translate");
    let asm = disassemble(&spv).expect("disassemble");
    assert!(
        asm.contains(" 1D 0 0 0 1 Unknown") && asm.contains("OpImageSample"),
        "the wrapped 1D texture is sampled through a 1D image:\n{asm}"
    );
}

#[test]
fn an_arrayed_wrapper_still_refuses() {
    let error = translate_sanitized_native(WRAPPED_ARRAY, Stage::Fragment, &tmp())
        .expect_err("one of several declared textures must not be the only one bound");
    assert!(
        error.contains("does not surface") && error.contains("air.texture"),
        "the refusal names the resource that was missed: {error}"
    );
}

#[test]
fn a_flat_c_array_member_is_indexed_rather_than_collapsed_to_element_zero() {
    let meta = metal2vulkan::meta::parse_air_fragment_meta(FLAT_C_ARRAY).expect("parse");
    assert!(
        meta.unsurfaced_embedded_resources.is_empty(),
        "a C-array texture member is surfaced, not refused: {:?}",
        meta.unsurfaced_embedded_resources
    );
    let texture = match meta.embedded_textures.as_slice() {
        [only] => *only,
        other => panic!("exactly one embedded texture argument, got {other:?}"),
    };
    assert_eq!(texture.array_length, Some(32));

    let spv = translate_sanitized_native(FLAT_C_ARRAY, Stage::Fragment, &tmp()).expect("translate");
    let asm = disassemble(&spv).expect("disassemble");
    let module = Disassembly::parse(&asm);

    // With heap routing of fixed argument-buffer fields on (NVMTL_NO_TIER2_FIXED unset or 0), the array goes through
    // the image heap, each element by the handle its read loads: the stage layout has one descriptor per binding, so
    // a 32-element array at one binding would leave elements 1 to 31 without a descriptor. With the switch set, the
    // array is one array binding indexed at run time.
    let declares_32_element_array = module.results().any(|(_, op, args)| {
        op == "OpTypeArray" && args.get(1).is_some_and(|length| module.constant_value(length) == Some(32))
    });
    if metal2vulkan::reflect::bindless_fixed_fields_on() {
        assert!(!declares_32_element_array, "the heap route declares no 32-element descriptor array:\n{asm}");
        assert_heap_element_is_indexed_by_its_handle(&module, &asm);
        return;
    }
    let (array_ty, image_ty) = module
        .results()
        .find_map(|(id, op, args)| {
            let [element, length] = args else { return None };
            (op == "OpTypeArray" && module.constant_value(length) == Some(32))
                .then_some((id, *element))
        })
        .unwrap_or_else(|| panic!("a 32-element descriptor array is declared:\n{asm}"));
    assert_eq!(
        module.opcode(image_ty),
        Some("OpTypeImage"),
        "the array's element is the image type:\n{asm}"
    );
    let array_ptr = module
        .results()
        .find_map(|(id, op, args)| {
            (op == "OpTypePointer" && args.first() == Some(&array_ty)).then_some(id)
        })
        .unwrap_or_else(|| panic!("a pointer to the array type is declared:\n{asm}"));
    let array_var = module
        .results()
        .find_map(|(id, op, args)| {
            (op == "OpVariable" && args.first() == Some(&array_ptr)).then_some(id)
        })
        .unwrap_or_else(|| panic!("the array is bound as one variable, not one image:\n{asm}"));

    let element = module
        .results()
        .find_map(|(id, op, args)| match args {
            [_, base, index] if op == "OpAccessChain" && *base == array_var => {
                assert!(
                    module.constant_value(index).is_none(),
                    "the index is the runtime value, not a constant:\n{asm}"
                );
                Some(id)
            }
            _ => None,
        })
        .unwrap_or_else(|| panic!("the array is indexed before it is loaded:\n{asm}"));
    let loaded = module
        .results()
        .find_map(|(id, op, args)| (op == "OpLoad" && args.get(1) == Some(&element)).then_some(id))
        .unwrap_or_else(|| panic!("the selected element is loaded:\n{asm}"));
    assert!(
        module
            .results()
            .any(|(_, op, args)| op == "OpSampledImage" && args.get(1) == Some(&loaded)),
        "and the loaded element is what gets sampled:\n{asm}"
    );
}

// The element's handle is read from the argument buffer at the runtime index, its slot bits index the image heap,
// and that heap element is what gets sampled.
fn assert_heap_element_is_indexed_by_its_handle(module: &Disassembly, asm: &str) {
    let def = |id: u32| module.results().find(|(def, ..)| *def == id);
    let heap = module
        .results()
        .find_map(|(id, op, args)| {
            let (_, _, pointer) = def(*args.first()?)?;
            let (_, array_op, array) = def(*pointer.first()?)?;
            (op == "OpVariable"
                && array_op == "OpTypeRuntimeArray"
                && module.opcode(*array.first()?) == Some("OpTypeImage"))
            .then_some(id)
        })
        .unwrap_or_else(|| panic!("the texture array goes through the image heap:\n{asm}"));
    let (element, index) = module
        .results()
        .find_map(|(id, op, args)| match args {
            [_, base, index] if op == "OpAccessChain" && *base == heap => Some((id, *index)),
            _ => None,
        })
        .unwrap_or_else(|| panic!("the heap is indexed:\n{asm}"));
    let handle = match def(index) {
        Some((_, "OpBitwiseAnd", [_, handle, _])) => *handle,
        other => panic!("the heap index is the slot bits of a handle, not {other:?}:\n{asm}"),
    };
    let handle_address = match def(handle) {
        Some((_, "OpLoad", [_, address])) => *address,
        other => panic!("the handle is loaded from the argument buffer, not {other:?}:\n{asm}"),
    };
    match def(handle_address) {
        Some((_, "OpAccessChain" | "OpInBoundsAccessChain", [_, _, indices @ ..])) => assert!(
            indices.last().is_some_and(|last| module.constant_value(last).is_none()),
            "the handle's word is chosen by the runtime index, not a constant:\n{asm}"
        ),
        other => panic!("the handle is read through an access chain, not {other:?}:\n{asm}"),
    }
    let loaded = module
        .results()
        .find_map(|(id, op, args)| (op == "OpLoad" && args.get(1) == Some(&element)).then_some(id))
        .unwrap_or_else(|| panic!("the selected heap element is loaded:\n{asm}"));
    assert!(
        module
            .results()
            .any(|(_, op, args)| op == "OpSampledImage" && args.get(1) == Some(&loaded)),
        "and the loaded element is what gets sampled:\n{asm}"
    );
}

struct Disassembly {
    defs: Vec<(u32, String, Vec<u32>)>,
    constants: std::collections::HashMap<u32, u32>,
}

impl Disassembly {
    fn parse(asm: &str) -> Self {
        let mut defs = vec![];
        let mut constants = std::collections::HashMap::new();
        for line in asm.lines() {
            let Some((result, rest)) = line.split_once('=') else {
                continue;
            };
            let Some(result) = result
                .trim()
                .strip_prefix('%')
                .and_then(|id| id.parse().ok())
            else {
                continue;
            };
            let mut tokens = rest.split_whitespace();
            let Some(opcode) = tokens.next() else {
                continue;
            };
            if opcode == "OpConstant" {
                if let Some(value) = tokens.clone().nth(1).and_then(|tok| tok.parse().ok()) {
                    constants.insert(result, value);
                }
            }
            let ids = tokens
                .filter_map(|tok| tok.strip_prefix('%')?.parse().ok())
                .collect();
            defs.push((result, opcode.to_string(), ids));
        }
        Self { defs, constants }
    }

    fn results(&self) -> impl Iterator<Item = (u32, &str, &[u32])> {
        self.defs
            .iter()
            .map(|(id, op, args)| (*id, op.as_str(), args.as_slice()))
    }

    fn opcode(&self, id: u32) -> Option<&str> {
        self.results()
            .find(|(def, ..)| *def == id)
            .map(|(_, op, _)| op)
    }

    fn constant_value(&self, id: &u32) -> Option<u32> {
        self.constants.get(id).copied()
    }
}

#[test]
fn a_single_embedded_texture_buffer_reads_through_the_texel_heap() {
    let spv = translate_sanitized_native(FLAT_TEXTURE_BUFFER, Stage::Fragment, &tmp()).expect("translate");
    let asm = disassemble(&spv).expect("disassemble");
    let module = Disassembly::parse(&asm);
    let decorations = |id: u32| -> Vec<String> {
        let prefix = format!("OpDecorate %{id} ");
        asm.lines()
            .filter_map(|line| line.trim().strip_prefix(prefix.as_str()).map(str::to_string))
            .collect()
    };
    // The image variables: a pointer to an image, or to a runtime array of images (the heap).
    let def = |id: u32| module.results().find(|(def, ..)| *def == id);
    let image_of = |ty: u32| match def(ty) {
        Some((_, "OpTypeImage", _)) => Some(ty),
        Some((_, "OpTypeRuntimeArray" | "OpTypeArray", [element, ..])) => {
            (module.opcode(*element) == Some("OpTypeImage")).then_some(*element)
        }
        _ => None,
    };
    let image_vars: Vec<u32> = module
        .results()
        .filter(|(_, op, args)| {
            *op == "OpVariable"
                && args.first().and_then(|ptr| def(*ptr)).is_some_and(|(_, _, pointer)| {
                    pointer.first().and_then(|pointee| image_of(*pointee)).is_some()
                })
        })
        .map(|(id, ..)| id)
        .collect();
    let [texel_var] = image_vars[..] else {
        panic!("one image variable holds the texture buffer, not {image_vars:?}:\n{asm}");
    };
    let placed = decorations(texel_var);
    // The bindless heap's uniform texel buffers are descriptor set 2, binding 3 (reflect::BINDLESS_UTEXEL_BINDING);
    // the host puts a texture buffer's texel view there, so the field's handle selects the real buffer.
    if metal2vulkan::reflect::bindless_fixed_fields_on() {
        assert!(
            placed.iter().any(|d| d == "DescriptorSet 2") && placed.iter().any(|d| d == "Binding 3"),
            "the texture buffer field is read through the heap's texel binding, not {placed:?}:\n{asm}"
        );
    } else {
        assert!(
            !placed.iter().any(|d| d == "DescriptorSet 2"),
            "with NVMTL_NO_TIER2_FIXED set the field keeps a per-draw binding, not {placed:?}:\n{asm}"
        );
    }
    assert!(asm.contains("OpImageFetch"), "the texel is fetched:\n{asm}");
}

// The switch is read once per process (reflect::bindless_fixed_fields_on), so each mode runs in its own child: other
// tests of this binary translate concurrently and keep the production default.
#[test]
fn the_heap_route_tests_hold_with_the_switch_unset_and_set() {
    if std::env::var_os("M2V_TEST_TIER2_FIXED_CHILD").is_some() {
        return;
    }
    for value in ["0", "1"] {
        for test in [
            "a_flat_c_array_member_is_indexed_rather_than_collapsed_to_element_zero",
            "a_single_embedded_texture_buffer_reads_through_the_texel_heap",
        ] {
            let output = std::process::Command::new(std::env::current_exe().unwrap())
                .args(["--exact", test, "--nocapture", "--test-threads", "1"])
                .env("M2V_TEST_TIER2_FIXED_CHILD", "1")
                .env("NVMTL_NO_TIER2_FIXED", value)
                .output()
                .expect("run the test in a child process");
            let stdout = String::from_utf8_lossy(&output.stdout);
            assert!(
                output.status.success() && stdout.contains("1 passed"),
                "{test} with NVMTL_NO_TIER2_FIXED={value}: {stdout}{}",
                String::from_utf8_lossy(&output.stderr)
            );
        }
    }
}
