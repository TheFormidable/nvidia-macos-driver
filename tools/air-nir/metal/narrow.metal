// SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
// char and short buffers: loads, sign and zero extension, byte and halfword stores by neighbouring threads.
#include <metal_stdlib>
using namespace metal;

kernel void narrow(device const char *a [[buffer(0)]], device const short *b [[buffer(1)]],
                   device const uchar *c [[buffer(2)]], device const ushort *d [[buffer(3)]],
                   device short *os [[buffer(4)]], device char *oc [[buffer(5)]], device int *oi [[buffer(6)]],
                   uint i [[thread_position_in_grid]])
{
   char x = a[i];
   short y = b[i];
   os[i] = short(x * y);
   oc[i] = char(x + char(c[i]));
   oi[i * 3] = int(x) * 1000 + int(y);
   oi[i * 3 + 1] = int(c[i]) - int(d[i]);
   oi[i * 3 + 2] = int(short(d[i])) + int(uchar(x));
}
