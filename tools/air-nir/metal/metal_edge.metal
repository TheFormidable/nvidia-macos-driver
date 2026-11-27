// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// Metal's results where the translator rewrites a function to them (passes/air_calls), one kernel each: NaN-aware
// precise min/max/clamp, round half away from zero, pow of a negative base, and an exact float-to-half narrowing
#include <metal_stdlib>
using namespace metal;

kernel void minmax_nan(device const float *f [[buffer(0)]], device float *out [[buffer(1)]], uint i [[thread_position_in_grid]])
{
   const float x = f[i], y = f[i ^ 1];
   device float *o = out + i * 8;
   o[0] = precise::fmin(x, y);
   o[1] = precise::fmax(x, y);
   o[2] = precise::clamp(x, -1.0f, 1.0f);
   o[3] = precise::fmin3(x, y, 0.25f);
   o[4] = precise::fmax3(x, y, 0.25f);
   o[5] = precise::fmedian3(x, y, 0.25f);
   o[6] = fast::fmin(x, 2.0f) * 0.0f;   // the fast ones: only that they compile and run (no NaN promise)
   o[7] = precise::fmin(x, INFINITY);
}

kernel void round_edge(device const float *f [[buffer(0)]], device float *out [[buffer(1)]], device half *hout [[buffer(2)]],
                       uint i [[thread_position_in_grid]])
{
   out[i] = round(f[i]);
   hout[i] = round(half(f[i] * 0.25f));
}

kernel void pow_edge(device const float *f [[buffer(0)]], device float *out [[buffer(1)]], uint i [[thread_position_in_grid]])
{
   out[i] = precise::pow(f[i * 2], f[i * 2 + 1]);
}

kernel void narrow_exact(device const float *f [[buffer(0)]], device float *out [[buffer(1)]], uint i [[thread_position_in_grid]])
{
   const float x = f[i];
   out[i * 2] = float(half(x * 3.0f)) - x * 3.0f;   // the rounding the narrowing makes, not folded away
   out[i * 2 + 1] = float(half(x)) * 2.0f;
}
