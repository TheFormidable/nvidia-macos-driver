// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Function constants: their values come with the pipeline's specialization, which the request cannot carry yet, so
// the frontend must refuse this kernel rather than take the placeholders' contents (0 or undef) as the values.
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
