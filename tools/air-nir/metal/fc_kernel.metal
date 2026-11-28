// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Function constants of several types: a bool, a float, an int deciding a switch, a uint with a default when it is
// not given (is_function_constant_defined), and a buffer argument that exists only under a bool constant. airrun
// runs it with two sets of values, and checks that a set leaving out a constant a live path reads is refused.
#include <metal_stdlib>
using namespace metal;

constant bool use_scale [[function_constant(0)]];
constant float scale_fc [[function_constant(1)]];
constant int mode_fc [[function_constant(2)]];
constant uint bias_fc [[function_constant(3)]];
constant uint bias_v = is_function_constant_defined(bias_fc) ? bias_fc : 1000u;
constant bool has_extra [[function_constant(4)]];

kernel void fc_kernel(device const float *in [[buffer(0)]], device float *out [[buffer(1)]],
                      device const uint *extra [[buffer(2), function_constant(has_extra)]],
                      device uint *iout [[buffer(3)]], uint i [[thread_position_in_grid]])
{
   float x = in[i];
   if (use_scale)
      x *= scale_fc;
   switch (mode_fc) {
   case 0: x += 1.0f; break;
   case 1: x = -x; break;
   default: x = x * x; break;
   }
   out[i] = x;
   uint u = i + bias_v;
   if (has_extra)
      u += extra[i];
   iout[i] = u;
}
