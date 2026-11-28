// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// A buffer of structs with a float3 (16-byte aligned) and a packed_float3 field.
#include <metal_stdlib>
using namespace metal;

struct Particle {
   float3 pos;           // offset 0, 16 bytes
   packed_float3 vel;    // offset 16, 12 bytes
   float mass;           // offset 28
   uint id;              // offset 32; size 48
};

kernel void struct_buf(device const Particle *in [[buffer(0)]], device Particle *out [[buffer(1)]],
                       constant float &dt [[buffer(2)]], uint i [[thread_position_in_grid]])
{
   Particle p = in[i];
   float3 v = float3(p.vel);
   p.pos += v * dt;
   p.vel = packed_float3(v * 0.5f + float3(p.pos.y, p.pos.z, p.pos.x));
   p.mass = length(p.pos) + p.mass;
   p.id = p.id * 2u + 1u;
   out[i] = p;
}
