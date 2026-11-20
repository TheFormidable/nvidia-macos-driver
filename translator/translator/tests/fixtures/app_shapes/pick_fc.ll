; The walk kernel's loop with its next buffer picked by a function constant (pick_b, index 0) instead of by the data:
; `next = pick_b ? b : a`. With pick_b = 1 the select between the two kernel buffers stays and is refused under
; Logical addressing; with pick_b = 0 it folds to `a`. Attribute spellings as in Apple's AIR (tests/fixtures/gl/*.ll).
target datalayout = "e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32"
target triple = "spirv-unknown-vulkan1.2"

@_Z6pick_b.MTL_FC_INIT_0_b = internal unnamed_addr addrspace(2) externally_initialized constant i8 undef, section "air.fc_initializer", align 1

define void @pick(ptr addrspace(1) noundef readonly align 4 captures(none) "air-buffer-no-alias" %a, ptr addrspace(1) noundef readonly align 4 captures(none) "air-buffer-no-alias" %b, ptr addrspace(1) noundef writeonly align 4 captures(none) initializes((0, 4)) "air-buffer-no-alias" %o, ptr addrspace(2) noundef readonly align 4 captures(none) dereferenceable(4) "air-buffer-no-alias" %n) local_unnamed_addr #0 {
entry:
  %fc = load i8, ptr addrspace(2) @_Z6pick_b.MTL_FC_INIT_0_b, align 1, !range !34
  %useb = trunc i8 %fc to i1
  %nn = load i32, ptr addrspace(2) %n, align 4
  %z = icmp eq i32 %nn, 0
  br i1 %z, label %exit, label %loop

loop:
  %i = phi i32 [ 0, %entry ], [ %i1, %loop ]
  %s = phi i32 [ 0, %entry ], [ %s1, %loop ]
  %p = phi ptr addrspace(1) [ %a, %entry ], [ %next, %loop ]
  %ie = zext i32 %i to i64
  %gep = getelementptr inbounds i32, ptr addrspace(1) %p, i64 %ie
  %v = load i32, ptr addrspace(1) %gep, align 4
  %m = mul i32 %s, 31
  %s1 = add i32 %m, %v
  %next = select i1 %useb, ptr addrspace(1) %b, ptr addrspace(1) %a
  %i1 = add nuw nsw i32 %i, 1
  %lt = icmp ult i32 %i1, %nn
  %lt16 = icmp ult i32 %i1, 16
  %go = and i1 %lt, %lt16
  br i1 %go, label %loop, label %exit

exit:
  %r = phi i32 [ 0, %entry ], [ %s1, %loop ]
  store i32 %r, ptr addrspace(1) %o, align 4
  ret void
}

attributes #0 = { mustprogress nofree norecurse nosync nounwind willreturn "frame-pointer"="all" }

!air.kernel = !{!10}
!air.function_constants = !{!30}
!air.version = !{!11}
!air.language_version = !{!12}
!10 = !{ptr @pick, !13, !14}
!11 = !{i32 2, i32 8, i32 0}
!12 = !{!"Metal", i32 4, i32 0, i32 0}
!13 = !{}
!14 = !{!20, !21, !22, !23}
!20 = !{i32 0, !"air.buffer", !"air.location_index", i32 0, i32 1, !"air.read", !"air.address_space", i32 1, !"air.arg_type_size", i32 4, !"air.arg_type_align_size", i32 4, !"air.arg_type_name", !"uint", !"air.arg_name", !"a"}
!21 = !{i32 1, !"air.buffer", !"air.location_index", i32 1, i32 1, !"air.read", !"air.address_space", i32 1, !"air.arg_type_size", i32 4, !"air.arg_type_align_size", i32 4, !"air.arg_type_name", !"uint", !"air.arg_name", !"b"}
!22 = !{i32 2, !"air.buffer", !"air.location_index", i32 2, i32 1, !"air.read_write", !"air.address_space", i32 1, !"air.arg_type_size", i32 4, !"air.arg_type_align_size", i32 4, !"air.arg_type_name", !"uint", !"air.arg_name", !"o"}
!23 = !{i32 3, !"air.buffer", !"air.buffer_size", i32 4, !"air.location_index", i32 3, i32 1, !"air.read", !"air.address_space", i32 2, !"air.arg_type_size", i32 4, !"air.arg_type_align_size", i32 4, !"air.arg_type_name", !"uint", !"air.arg_name", !"n"}
!30 = !{ptr addrspace(2) @_Z6pick_b.MTL_FC_INIT_0_b, !"bool", !"pick_b", i32 0, i1 true}
!34 = !{i8 0, i8 2}
