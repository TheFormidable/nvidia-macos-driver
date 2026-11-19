//! The unit tests of pull request 524 of the original project for its translator part T3 (League of
//! Legends): which pointers retype a local variable (native/ir/alloca.rs: only one proven to
//! address the whole variable does) and the byte views a helper takes of a pointer-free local
//! (native/ir/alloca.rs and native/ir/pointer_pointees.rs). Their fixtures are synthetic. They hold
//! with NVMTL_WHOLE_VIEWS unset; tools/test_translator_whole_views.py --baseline runs this file
//! against an older translator, where 8 of them fail. whole_view_way_back_tests.rs holds
//! NVMTL_WHOLE_VIEWS=0 to what 3.0.4 inferred.
use super::*;

fn parsed(air: &str) -> LlModule {
    LlModule::parse(air).expect("fixture parses")
}

#[test]
fn helper_byte_views_only_reinterpret_proven_pointer_free_whole_allocas() {
    for (declared, setup, argument, view, expected_bytes) in [
        ("{ <4 x i32> }", "", "%a", "i8", Some(16)),
        ("{ i32, i32, i32, i32 }", "", "%a", "[16 x i8]", Some(16)),
        (
            "{ <4 x i32> }",
            "%alias = bitcast ptr %a to ptr",
            "%alias",
            "i8",
            Some(16),
        ),
        ("{ <4 x i32> }", "", "%a", "[8 x i8]", None),
        ("{ ptr, i64 }", "", "%a", "i8", None),
        (
            "{ [4 x i32] }",
            "%member = getelementptr %U, ptr %a, i32 0, i32 0",
            "%member",
            "i8",
            None,
        ),
        (
            "{ { <4 x i32> } }",
            "%member = getelementptr %U, ptr %a, i32 0, i32 0",
            "%member",
            "i8",
            None,
        ),
        (
            "{ i32, i32 }",
            "%member = getelementptr %U, ptr %a, i32 0, i32 0",
            "%member",
            "i8",
            None,
        ),
        (
            "{ i32, i32 }",
            "%member = getelementptr %U, ptr %a, i32 0, i32 1",
            "%member",
            "i8",
            None,
        ),
        (
            "{ ptr }",
            "%member = getelementptr %U, ptr %a, i32 0, i32 0",
            "%member",
            "i8",
            None,
        ),
        (
            "{ [4 x i32] }",
            "%member = getelementptr %U, ptr %a, i32 0, i32 0",
            "%member",
            "[4 x i32]",
            None,
        ),
    ] {
        let index = if view == "i8" {
            "i64 0"
        } else {
            "i64 0, i64 0"
        };
        let air = format!(
            r#"%U = type {declared}
define void @helper(ptr %p) {{
entry:
  %q = getelementptr {view}, ptr %p, {index}
  store i8 73, ptr %q
  ret void
}}
define void @k() {{
entry:
  %a = alloca %U
  {setup}
  call void @helper(ptr {argument})
  ret void
}}
"#
        );
        let module = parsed(&air);
        let expected = expected_bytes.map(|bytes| LlType::Array(Box::new(LlType::Int(8)), bytes));
        assert_eq!(
            module
                .local_alloca_pointees
                .get(&("k".to_string(), "%a".to_string())),
            expected.as_ref(),
            "{declared} / {view} / {argument}"
        );
    }
    let module = parsed("define void @guard() {\nentry:\n  ret void\n}\n");
    let member = LlType::Array(Box::new(LlType::Int(32)), 4);
    assert!(!module
        .is_local_alloca_reinterpret_candidate(&LlType::Struct(vec![member.clone()]), &member));
}

#[test]
fn mixed_helper_views_use_one_pointer_free_aggregate_extent() {
    for (declared, backing, expected) in [
        ("{ <4 x i32> }", "{ <4 x i32> }", Some(16)),
        ("{ <4 x i32> }", "{ i64 }", None),
        ("{ ptr, i64 }", "{ ptr, i64 }", None),
    ] {
        let air = format!(
            r#"%U = type {declared}
%Backing = type {backing}
define void @helper(ptr %p) {{
entry:
  %typed = getelementptr %U, ptr %p, i32 0, i32 0
  %byte = getelementptr i8, ptr %p, i64 0
  ret void
}}
define void @k() {{
entry:
  %a = alloca %Backing
  call void @helper(ptr %a)
  ret void
}}
"#
        );
        let module = parsed(&air);
        let expected = expected.map(|n| LlType::Array(Box::new(LlType::Int(8)), n));
        assert_eq!(
            module.local_alloca_pointees.get(&("k".into(), "%a".into())),
            expected.as_ref(),
            "{declared} / {backing}"
        );
    }
}

#[test]
fn mixed_byte_view_propagates_through_equal_extent_typed_forwarder() {
    let air = r#"%U = type { <4 x i32> }
define void @helper(ptr %p) {
entry:
  %typed = getelementptr %U, ptr %p, i32 0, i32 0
  %byte = getelementptr i8, ptr %p, i64 0
  ret void
}
define void @forward(ptr %p) {
entry:
  %typed = getelementptr %U, ptr %p, i32 0, i32 0
  call void @helper(ptr %p)
  ret void
}
define void @k() {
entry:
  %a = alloca %U
  call void @forward(ptr %a)
  ret void
}
"#;
    let module = parsed(air);
    let bytes = LlType::Array(Box::new(LlType::Int(8)), 16);
    assert_eq!(
        module.ptr_pointees.get(&("forward".into(), "%p".into())),
        Some(&bytes)
    );
    assert_eq!(
        module.local_alloca_pointees.get(&("k".into(), "%a".into())),
        Some(&bytes)
    );
}

#[test]
fn mixed_byte_view_preserves_metadata_extent_and_pointer_bearing_contracts() {
    let air = r#"%U = type { <4 x i32> }
define void @helper(ptr %p) {
entry:
  %typed = getelementptr %U, ptr %p, i32 0, i32 0
  %byte = getelementptr i8, ptr %p, i64 0
  ret void
}
"#;
    let key = ("helper".to_string(), "%p".to_string());
    let mut module = parsed(air);
    let declared = LlType::Array(Box::new(LlType::Int(8)), 8);
    module.ptr_pointees.insert(key.clone(), declared.clone());
    module.metadata_pointee_params.insert(key.clone());
    module.metadata_pointee_sizes.insert(key.clone(), 8);
    module.infer_pointer_pointees();
    assert_eq!(module.ptr_pointees.get(&key), Some(&declared));
    let mut module = parsed(air);
    let declared = LlType::Struct(vec![LlType::Ptr(0), LlType::Int(64)]);
    module.ptr_pointees.insert(key.clone(), declared.clone());
    module.infer_pointer_pointees();
    assert_eq!(module.ptr_pointees.get(&key), Some(&declared));
}

#[test]
fn mixed_helper_views_reject_conflicting_aggregate_extents() {
    let air = r#"%U = type { <4 x i32> }
%V = type { <2 x i32> }
define void @helper(ptr %p) {
entry:
  %u = getelementptr %U, ptr %p, i32 0, i32 0
  %v = getelementptr %V, ptr %p, i32 0, i32 0
  %byte = getelementptr i8, ptr %p, i64 0
  ret void
}
define void @k() {
entry:
  %a = alloca %U
  call void @helper(ptr %a)
  ret void
}
"#;
    assert!(parsed(air).local_alloca_pointees.is_empty());
}

#[test]
fn nested_same_size_member_gep_does_not_reinterpret_its_alloca() {
    let air = concat!(
        "%matrix = type { [4 x <4 x float>] }\n",
        "define void @k() {\n",
        "entry:\n",
        "  %a = alloca %matrix, align 16\n",
        "  %rows = getelementptr %matrix, ptr %a, i32 0, i32 0\n",
        "  %row = getelementptr [4 x <4 x float>], ptr %rows, i32 0, i32 1\n",
        "  ret void\n",
        "}\n",
    );
    assert!(parsed(air).local_alloca_pointees.is_empty());
}

#[test]
fn zero_index_gep_preserves_whole_alloca_reinterpret() {
    let air = r#"define void @k() {
entry:
  %a = alloca i32
  %alias = getelementptr i32, ptr %a, i64 0
  %p = getelementptr float, ptr %alias, i64 0
  ret void
}
"#;
    assert_eq!(
        parsed(air)
            .local_alloca_pointees
            .get(&("k".to_string(), "%a".to_string())),
        Some(&LlType::Float)
    );
}

#[test]
fn bitcast_then_zero_index_gep_preserves_whole_alloca_reinterpret() {
    let air = r#"define void @k() {
entry:
  %a = alloca i32
  %cast = bitcast ptr %a to ptr
  %alias = getelementptr i32, ptr %cast, i64 0
  %p = getelementptr float, ptr %alias, i64 0
  ret void
}
"#;
    assert_eq!(
        parsed(air)
            .local_alloca_pointees
            .get(&("k".to_string(), "%a".to_string())),
        Some(&LlType::Float)
    );
}

#[test]
fn whole_object_gep_before_member_does_not_reinterpret_matrix() {
    let air = r#"%matrix = type { [4 x <4 x float>] }
define void @k() {
entry:
  %a = alloca %matrix
  %whole = getelementptr %matrix, ptr %a, i64 0
  %rows = getelementptr %matrix, ptr %whole, i32 0, i32 0
  %row = getelementptr [4 x <4 x float>], ptr %rows, i32 0, i32 1
  ret void
}
"#;
    assert!(parsed(air).local_alloca_pointees.is_empty());
}

#[test]
fn same_root_whole_alias_phi_resolves_on_later_fixed_point_pass() {
    let air = r#"define void @k(i1 %cond) {
entry:
  %a = alloca i32
  br i1 %cond, label %left, label %right
join:
  %merged = phi ptr [ %left_alias, %left ], [ %right_alias, %right ]
  %p = getelementptr float, ptr %merged, i64 0
  ret void
left:
  %left_alias = getelementptr i32, ptr %a, i64 0
  br label %join
right:
  %right_alias = bitcast ptr %a to ptr
  br label %join
}
"#;
    assert_eq!(
        parsed(air)
            .local_alloca_pointees
            .get(&("k".to_string(), "%a".to_string())),
        Some(&LlType::Float)
    );
}

#[test]
fn phi_with_unknown_arm_is_not_whole_object_alias() {
    let air = r#"define void @k(i1 %cond, ptr %unknown) {
entry:
  %a = alloca i32
  br i1 %cond, label %left, label %right
left:
  br label %join
right:
  br label %join
join:
  %merged = phi ptr [ %a, %left ], [ %unknown, %right ]
  %p = getelementptr float, ptr %merged, i64 0
  ret void
}
"#;
    assert!(parsed(air).local_alloca_pointees.is_empty());
}

#[test]
fn phi_with_member_arm_is_not_whole_object_alias() {
    let air = r#"%matrix = type { [4 x <4 x float>] }
define void @k(i1 %cond) {
entry:
  %a = alloca %matrix
  %member = getelementptr %matrix, ptr %a, i32 0, i32 0
  br i1 %cond, label %left, label %right
left:
  br label %join
right:
  br label %join
join:
  %merged = phi ptr [ %a, %left ], [ %member, %right ]
  %row = getelementptr [4 x <4 x float>], ptr %merged, i32 0, i32 1
  ret void
}
"#;
    assert!(parsed(air).local_alloca_pointees.is_empty());
}

#[test]
fn unproven_dynamic_gep_is_not_whole_object_alias() {
    let air = r#"define void @k(i64 %offset) {
entry:
  %a = alloca i32
  %alias = getelementptr i32, ptr %a, i64 %offset
  %p = getelementptr float, ptr %alias, i64 0
  ret void
}
"#;
    assert!(parsed(air).local_alloca_pointees.is_empty());
}

#[test]
fn self_loop_phi_preserves_whole_alloca_reinterpret() {
    let air = r#"define void @k(i1 %stop) {
entry:
  %a = alloca i32
  br label %loop
loop:
  %p = phi ptr [ %a, %entry ], [ %p, %loop ]
  %view = getelementptr float, ptr %p, i64 0
  br i1 %stop, label %exit, label %loop
exit:
  ret void
}
"#;
    assert_eq!(
        parsed(air)
            .local_alloca_pointees
            .get(&("k".to_string(), "%a".to_string())),
        Some(&LlType::Float)
    );
}

#[test]
fn indirect_identity_backedge_preserves_whole_alloca_reinterpret() {
    let air = r#"define void @k(i1 %stop) {
entry:
  %a = alloca i32
  br label %loop
loop:
  %p = phi ptr [ %a, %entry ], [ %back, %latch ]
  %view = getelementptr float, ptr %p, i64 0
  br i1 %stop, label %exit, label %latch
latch:
  %identity = bitcast ptr %p to ptr
  %back = getelementptr i32, ptr %identity, i64 0
  br label %loop
exit:
  ret void
}
"#;
    assert_eq!(
        parsed(air)
            .local_alloca_pointees
            .get(&("k".to_string(), "%a".to_string())),
        Some(&LlType::Float)
    );
}

#[test]
fn pure_pointer_cycle_is_not_whole_alloca_alias() {
    // An unseeded pointer cycle has no alloca terminal. Keep it unreachable so
    // the fixture does not invent a missing predecessor value at function entry.
    let air = r#"define void @k() {
entry:
  %a = alloca i32
  ret void
cycle:
  %p = phi ptr [ %back, %cycle ]
  %back = bitcast ptr %p to ptr
  %view = getelementptr float, ptr %p, i64 0
  br label %cycle
}
"#;
    assert!(parsed(air).local_alloca_pointees.is_empty());
}

#[test]
fn phi_with_different_alloca_roots_is_not_whole_object_alias() {
    let air = r#"define void @k(i1 %cond) {
entry:
  %a = alloca i32
  %b = alloca i32
  br i1 %cond, label %left, label %right
left:
  br label %join
right:
  br label %join
join:
  %merged = phi ptr [ %a, %left ], [ %b, %right ]
  %view = getelementptr float, ptr %merged, i64 0
  ret void
}
"#;
    assert!(parsed(air).local_alloca_pointees.is_empty());
}

#[test]
fn phi_with_null_arm_is_not_whole_object_alias() {
    let air = r#"define void @k(i1 %cond) {
entry:
  %a = alloca i32
  br i1 %cond, label %left, label %right
left:
  br label %join
right:
  br label %join
join:
  %merged = phi ptr [ %a, %left ], [ null, %right ]
  %view = getelementptr float, ptr %merged, i64 0
  ret void
}
"#;
    assert!(parsed(air).local_alloca_pointees.is_empty());
}

#[test]
fn alternate_zero_spellings_preserve_whole_alloca_reinterpret() {
    // The translator's parser represents these as SignedInt(0), Hex(0), Zero.
    for zero in ["-0", "0x0", "zeroinitializer"] {
        let air = format!(
            r#"define void @k() {{
entry:
  %a = alloca i32
  %alias = getelementptr i32, ptr %a, i64 {zero}
  %view = getelementptr float, ptr %alias, i64 0
  ret void
}}
"#
        );
        assert_eq!(
            parsed(&air)
                .local_alloca_pointees
                .get(&("k".to_string(), "%a".to_string())),
            Some(&LlType::Float),
            "zero spelling {zero}"
        );
    }
}

#[test]
fn indirect_pointer_cycle_with_unknown_leaf_is_not_whole_object_alias() {
    let air = r#"define void @k(i1 %stop, ptr %unknown) {
entry:
  %a = alloca i32
  br label %loop
loop:
  %p = phi ptr [ %a, %entry ], [ %q, %latch ]
  %q = phi ptr [ %unknown, %entry ], [ %p, %latch ]
  %view = getelementptr float, ptr %p, i64 0
  br i1 %stop, label %exit, label %latch
latch:
  br label %loop
exit:
  ret void
}
"#;
    assert!(parsed(air).local_alloca_pointees.is_empty());
}

#[test]
fn whole_phi_with_independent_rootless_cycle_is_not_an_alias() {
    let air = r#"define void @k(i1 %cond) {
entry:
  %a = alloca i32
  br label %loop
loop:
  %cycle = phi ptr [ %cycle, %loop ]
  %merged = phi ptr [ %a, %entry ], [ %cycle, %loop ]
  %q = getelementptr float, ptr %merged, i64 0
  br i1 %cond, label %exit, label %loop
exit:
  ret void
}
"#;
    assert!(parsed(air).local_alloca_pointees.is_empty());
}
