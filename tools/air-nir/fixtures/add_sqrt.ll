target datalayout = "e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32"
target triple = "air64-apple-macosx14.0.0"
define void @add(ptr addrspace(1) noundef %a, ptr addrspace(1) noundef %b, ptr addrspace(1) %out, i32 %gid) {
  %i = zext i32 %gid to i64
  %pa = getelementptr inbounds float, ptr addrspace(1) %a, i64 %i
  %pb = getelementptr inbounds float, ptr addrspace(1) %b, i64 %i
  %va = load float, ptr addrspace(1) %pa, align 4
  %vb = load float, ptr addrspace(1) %pb, align 4
  %s = fadd fast float %va, %vb
  %r = call fast float @air.fast_sqrt.f32(float %s)
  %po = getelementptr inbounds float, ptr addrspace(1) %out, i64 %i
  store float %r, ptr addrspace(1) %po, align 4
  ret void
}
declare float @air.fast_sqrt.f32(float)
!air.kernel = !{!0}
!0 = !{ptr @add, !1, !2}
!1 = !{}
!2 = !{!3, !4, !5, !6}
!3 = !{i32 0, !"air.buffer", !"air.location_index", i32 0, i32 1, !"air.read", !"air.address_space", i32 1, !"air.arg_type_size", i32 4, !"air.arg_type_align_size", i32 4}
!4 = !{i32 1, !"air.buffer", !"air.location_index", i32 1, i32 1, !"air.read", !"air.address_space", i32 1, !"air.arg_type_size", i32 4, !"air.arg_type_align_size", i32 4}
!5 = !{i32 2, !"air.buffer", !"air.location_index", i32 2, i32 1, !"air.read_write", !"air.address_space", i32 1, !"air.arg_type_size", i32 4, !"air.arg_type_align_size", i32 4}
!6 = !{i32 3, !"air.thread_position_in_grid", !"air.arg_type_name", !"uint"}
