// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Function constants, none given (newFunctionWithName: without values): the translator reads each as 0, and since
// round 12 so does the frontend (rounds 1 to 11 refused it), so use_scale is false and a[i] gains 1.
#include <metal_stdlib>
using namespace metal;

constant bool use_scale [[function_constant(0)]];
constant float scale_fc [[function_constant(1)]];

kernel void fc_refuse(device float *a [[buffer(0)]], uint i [[thread_position_in_grid]])
{
   if (use_scale)
      a[i] *= scale_fc;
   else
      a[i] += 1.0f;
}
