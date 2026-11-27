target datalayout = "e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32"
target triple = "air64-apple-macosx14.0.0"
@tg = internal addrspace(3) global [64 x float] undef, align 4
@lut = internal addrspace(2) constant [4 x i32] [i32 1, i32 2, i32 3, i32 5], align 4
define void @reduce(ptr addrspace(1) %in, ptr addrspace(1) %out, ptr addrspace(1) %counter, ptr addrspace(2) %params, i32 %lid, i32 %gid, <3 x i32> %tgid) {
entry:
  %n = load i32, ptr addrspace(2) %params, align 4
  br label %loop
loop:
  %k = phi i32 [0, %entry], [%k1, %body]
  %acc = phi float [0.0, %entry], [%acc1, %body]
  %c = icmp ult i32 %k, %n
  br i1 %c, label %body, label %done
body:
  %base = shl i32 %gid, 2
  %idx = add i32 %base, %k
  %idx64 = zext i32 %idx to i64
  %p = getelementptr float, ptr addrspace(1) %in, i64 %idx64
  %v = load float, ptr addrspace(1) %p, align 4
  %acc1 = fadd float %acc, %v
  %k1 = add i32 %k, 1
  br label %loop
done:
  %lid64 = zext i32 %lid to i64
  %slot = getelementptr [64 x float], ptr addrspace(3) @tg, i64 0, i64 %lid64
  store float %acc, ptr addrspace(3) %slot, align 4
  call void @air.wg.barrier(i32 2, i32 1)
  %first = icmp eq i32 %lid, 0
  br i1 %first, label %sum, label %exit
sum:
  %j = phi i32 [0, %done], [%j1, %sum]
  %t = phi float [0.0, %done], [%t1, %sum]
  %j64 = zext i32 %j to i64
  %q = getelementptr [64 x float], ptr addrspace(3) @tg, i64 0, i64 %j64
  %w = load float, ptr addrspace(3) %q, align 4
  %t1 = fadd float %t, %w
  %j1 = add i32 %j, 1
  %more = icmp ult i32 %j1, 64
  br i1 %more, label %sum, label %tail
tail:
  %g = extractelement <3 x i32> %tgid, i32 0
  %sel = and i32 %g, 3
  %sel64 = zext i32 %sel to i64
  %lp = getelementptr [4 x i32], ptr addrspace(2) @lut, i64 0, i64 %sel64
  %m = load i32, ptr addrspace(2) %lp, align 4
  %mf = call float @air.convert.f.f32.u.i32(i32 %m)
  %r = fmul float %t1, %mf
  switch i32 %sel, label %dflt [ i32 0, label %s0
                                 i32 1, label %s1 ]
s0:
  br label %store
s1:
  %r2 = fneg float %r
  br label %store
dflt:
  %r3 = call float @air.fast_fmax.f32(float %r, float 0.0)
  br label %store
store:
  %rv = phi float [%r, %s0], [%r2, %s1], [%r3, %dflt]
  %g64 = zext i32 %g to i64
  %op = getelementptr float, ptr addrspace(1) %out, i64 %g64
  store float %rv, ptr addrspace(1) %op, align 4
  %old = call i32 @air.atomic.global.add.u.i32(ptr addrspace(1) %counter, i32 1, i32 0, i32 2, i1 true)
  br label %exit
exit:
  ret void
}
declare void @air.wg.barrier(i32, i32)
declare float @air.convert.f.f32.u.i32(i32)
declare float @air.fast_fmax.f32(float, float)
declare i32 @air.atomic.global.add.u.i32(ptr addrspace(1), i32, i32, i32, i1)
!air.kernel = !{!0}
!0 = !{ptr @reduce, !1, !2}
!1 = !{}
!2 = !{!3, !4, !5, !6, !7, !8, !9}
!3 = !{i32 0, !"air.buffer", !"air.location_index", i32 0, i32 1, !"air.read", !"air.address_space", i32 1, !"air.arg_type_size", i32 4, !"air.arg_type_align_size", i32 4}
!4 = !{i32 1, !"air.buffer", !"air.location_index", i32 1, i32 1, !"air.read_write", !"air.address_space", i32 1, !"air.arg_type_size", i32 4, !"air.arg_type_align_size", i32 4}
!5 = !{i32 2, !"air.buffer", !"air.location_index", i32 2, i32 1, !"air.read_write", !"air.address_space", i32 1, !"air.arg_type_size", i32 4, !"air.arg_type_align_size", i32 4}
!6 = !{i32 3, !"air.buffer", !"air.location_index", i32 3, i32 1, !"air.read", !"air.address_space", i32 2, !"air.arg_type_size", i32 4, !"air.arg_type_align_size", i32 4}
!7 = !{i32 4, !"air.thread_index_in_threadgroup", !"air.arg_type_name", !"uint"}
!8 = !{i32 5, !"air.thread_position_in_grid", !"air.arg_type_name", !"uint"}
!9 = !{i32 6, !"air.threadgroup_position_in_grid", !"air.arg_type_name", !"uint3"}
