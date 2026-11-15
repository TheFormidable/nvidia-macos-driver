; ModuleID = 'nvmtl-air'
source_filename = "glsl shader"
target datalayout = "e-p:64:64:64-i1:8:8-i8:8:8-i16:16:16-i32:32:32-i64:64:64-f32:32:32-f64:64:64-v16:16:16-v24:32:32-v32:32:32-v48:64:64-v64:64:64-v96:128:128-v128:128:128-v192:256:256-v256:256:256-v512:512:512-v1024:1024:1024-n8:16:32"
target triple = "air64-apple-macosx"

declare float @air.convert.f.f32.s.i32(i32) #0

define { <4 x float> } @__main(<3 x float> %0, ptr addrspace(2) %1) #1 {
gldPluginBody:
  %2 = call { <4 x float> } @"#internal_main"(<3 x float> %0, ptr addrspace(2) %1)
  ret { <4 x float> } %2
}

define internal { <4 x float> } @"#internal_main"(<3 x float> %0, ptr addrspace(2) %1) #1 {
allocaBlock:
  br label %globalBlock

globalBlock:
  %2 = bitcast ptr addrspace(2) %1 to ptr addrspace(2)
  %3 = getelementptr <4 x float>, ptr addrspace(2) %2, i32 1
  br label %block

block:
  %4 = bitcast ptr addrspace(2) %3 to ptr addrspace(2)
  %5 = load <2 x i32>, ptr addrspace(2) %4, align 8
  %6 = extractelement <2 x i32> %5, i32 1
  %7 = call reassoc nsz arcp contract afn float @air.convert.f.f32.s.i32(i32 %6)
  %8 = fdiv reassoc nsz arcp contract afn float %7, 2.400000e+02
  %9 = extractelement <3 x float> %0, i32 0
  %10 = fadd float %9, %8
  %11 = insertelement <3 x float> %0, float %10, i32 0
  %12 = shufflevector <3 x float> %11, <3 x float> undef, <4 x i32> <i32 0, i32 1, i32 2, i32 2>
  %13 = insertelement <4 x float> %12, float 1.000000e+00, i64 3
  %14 = insertvalue { <4 x float> } undef, <4 x float> %13, 0
  ret { <4 x float> } %14
}

attributes #0 = { nounwind memory(none) }
attributes #1 = { nounwind }

!air.vertex = !{!0}
!air.version = !{!7}
!air.language_version = !{!8}
!llvm.module.flags = !{}

!0 = !{ptr @__main, !1, !3}
!1 = !{!2}
!2 = !{!"air.position", !"air.arg_type_name", !"float4", !"air.arg_name", !"#air_position"}
!3 = !{!4, !5}
!4 = !{i32 0, !"air.vertex_input", !"air.location_index", i32 0, i32 1, !"attrib_0", !"air.arg_type_name", !"float3", !"air.arg_name", !"attrib_0"}
!5 = !{i32 1, !"air.buffer", !"air.buffer_size", i32 32, !"air.location_index", i32 6, i32 1, !"air.read", !"air.arg_type_name", !"uchar", !"air.arg_name", !"vs_ubo_1"}
!7 = !{i32 2, i32 3, i32 0}
!8 = !{!"OpenGL GLSL", i32 1, i32 0, i32 0}
