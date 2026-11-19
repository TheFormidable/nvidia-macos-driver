//! NVMTL_WHOLE_VIEWS=0 gives what 3.0.4 inferred (native/view_switches.rs). Each fixture is one of
//! whole_view_tests.rs whose result the whole-object views of pull request 524 of the original
//! project changed, and each expected value under the switch off is what the translator of 3.0.4
//! inferred for it (read from that translator with these fixtures). The same fixtures with the
//! switch on give the new results, so the switch is shown to choose between the two.
use super::*;
use crate::native::view_switches::test_override::with_whole_views;

/// A helper that writes one byte through `view` at the start of its parameter, called on a local
/// `%a` of type `declared` (after `setup`) passed as `argument`: the fixture of the first test of
/// whole_view_tests.rs.
fn helper_byte_view(declared: &str, setup: &str, argument: &str, view: &str) -> String {
    let index = if view == "i8" {
        "i64 0"
    } else {
        "i64 0, i64 0"
    };
    format!(
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
    )
}

/// A helper that views its parameter both as %U and as bytes, called on a local of type %Backing.
fn mixed_helper_view(declared: &str, backing: &str) -> String {
    format!(
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
    )
}

const MIXED_VIEW_FORWARDER: &str = r#"%U = type { <4 x i32> }
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

const PHI_WITH_NULL_ARM: &str = r#"define void @k(i1 %cond) {
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

const PHI_WITH_UNKNOWN_ARM: &str = r#"define void @k(i1 %cond, ptr %unknown) {
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

const UNPROVEN_DYNAMIC_GEP: &str = r#"define void @k(i64 %offset) {
entry:
  %a = alloca i32
  %alias = getelementptr i32, ptr %a, i64 %offset
  %p = getelementptr float, ptr %alias, i64 0
  ret void
}
"#;

const CYCLE_WITH_UNKNOWN_LEAF: &str = r#"define void @k(i1 %stop, ptr %unknown) {
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

const PHI_WITH_ROOTLESS_CYCLE: &str = r#"define void @k(i1 %cond) {
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

const MATRIX_MEMBER_GEP: &str = r#"%matrix = type { [4 x <4 x float>] }
define void @k() {
entry:
  %a = alloca %matrix, align 16
  %rows = getelementptr %matrix, ptr %a, i32 0, i32 0
  %row = getelementptr [4 x <4 x float>], ptr %rows, i32 0, i32 1
  ret void
}
"#;

fn alloca_pointee(module: &LlModule) -> Option<LlType> {
    module
        .local_alloca_pointees
        .get(&("k".to_string(), "%a".to_string()))
        .cloned()
}

fn bytes(n: u32) -> Option<LlType> {
    Some(LlType::Array(Box::new(LlType::Int(8)), n))
}

fn parsed(on: bool, air: &str) -> LlModule {
    with_whole_views(on, || LlModule::parse(air).expect("fixture parses"))
}

fn param_pointee(module: &LlModule, function: &str) -> Option<LlType> {
    module
        .ptr_pointees
        .get(&(function.to_string(), "%p".to_string()))
        .cloned()
}

#[test]
fn a_helper_byte_view_does_not_retype_its_local_with_whole_views_off() {
    let named = |name: &str| Some(LlType::Named(name.to_string()));
    for (declared, setup, argument, view, now, helper) in [
        (
            "{ <4 x i32> }",
            "",
            "%a",
            "i8",
            bytes(16),
            Some(LlType::Int(8)),
        ),
        (
            "{ i32, i32, i32, i32 }",
            "",
            "%a",
            "[16 x i8]",
            bytes(16),
            bytes(16),
        ),
        (
            "{ <4 x i32> }",
            "%alias = bitcast ptr %a to ptr",
            "%alias",
            "i8",
            bytes(16),
            Some(LlType::Int(8)),
        ),
        ("{ <4 x i32> }", "", "%a", "[8 x i8]", None, bytes(8)),
        ("{ ptr, i64 }", "", "%a", "i8", None, Some(LlType::Int(8))),
        (
            "{ [4 x i32] }",
            "%member = getelementptr %U, ptr %a, i32 0, i32 0",
            "%member",
            "i8",
            None,
            Some(LlType::Int(8)),
        ),
    ] {
        let air = helper_byte_view(declared, setup, argument, view);
        let off = parsed(false, &air);
        assert_eq!(
            alloca_pointee(&off),
            None,
            "3.0.4 left the local as declared: {declared} / {view} / {argument}"
        );
        assert_eq!(param_pointee(&off, "helper"), helper, "{declared} / {view}");
        assert_eq!(
            alloca_pointee(&parsed(true, &air)),
            now,
            "{declared} / {view} / {argument}"
        );
    }
    for (declared, backing, now) in [
        ("{ <4 x i32> }", "{ <4 x i32> }", bytes(16)),
        ("{ <4 x i32> }", "{ i64 }", None),
        ("{ ptr, i64 }", "{ ptr, i64 }", None),
    ] {
        let air = mixed_helper_view(declared, backing);
        let off = parsed(false, &air);
        assert_eq!(alloca_pointee(&off), None, "{declared} / {backing}");
        assert_eq!(
            param_pointee(&off, "helper"),
            named("%U"),
            "3.0.4 kept the helper's typed view: {declared} / {backing}"
        );
        assert_eq!(
            alloca_pointee(&parsed(true, &air)),
            now,
            "{declared} / {backing}"
        );
    }
    let off = parsed(false, MIXED_VIEW_FORWARDER);
    assert_eq!(alloca_pointee(&off), None);
    assert_eq!(param_pointee(&off, "forward"), named("%U"));
    assert_eq!(param_pointee(&off, "helper"), named("%U"));
    let on = parsed(true, MIXED_VIEW_FORWARDER);
    assert_eq!(
        (alloca_pointee(&on), param_pointee(&on, "forward")),
        (bytes(16), bytes(16))
    );
}

#[test]
fn a_pointer_that_is_not_a_whole_object_alias_retypes_its_local_with_whole_views_off() {
    for (what, air) in [
        ("a phi with a null arm", PHI_WITH_NULL_ARM),
        ("a phi with an unknown arm", PHI_WITH_UNKNOWN_ARM),
        ("a GEP with a dynamic offset", UNPROVEN_DYNAMIC_GEP),
        ("a cycle with an unknown leaf", CYCLE_WITH_UNKNOWN_LEAF),
        (
            "a phi with a cycle no alloca feeds",
            PHI_WITH_ROOTLESS_CYCLE,
        ),
    ] {
        let off = parsed(false, air);
        assert_eq!(
            alloca_pointee(&off),
            Some(LlType::Float),
            "3.0.4 retyped the local through {what}"
        );
        assert_eq!(off.local_alloca_pointees.len(), 1, "{what}");
        assert!(parsed(true, air).local_alloca_pointees.is_empty(), "{what}");
    }
    // The matrix whose rows are viewed through its one member was left as declared by 3.0.4 too: a
    // struct's only member is not a candidate to retype it (is_local_alloca_reinterpret_candidate).
    assert!(parsed(false, MATRIX_MEMBER_GEP)
        .local_alloca_pointees
        .is_empty());
    assert!(parsed(true, MATRIX_MEMBER_GEP)
        .local_alloca_pointees
        .is_empty());
}
