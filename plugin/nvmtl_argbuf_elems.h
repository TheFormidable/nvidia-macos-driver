/* nvmtl_argbuf_elems.h - how many [[id]]s an argument-buffer member that holds resource handles takes, and where each
   element's handle word is, from the member's air.struct_type_info tuple and its leaf node. Pure, so
   tools/test_argbuf_elems.c proves it; plugin/NVMTLLibraryLoad.m (nvmtl_air_argbuf_walk) uses it.

   Apple's compiler writes a member as `i32 offset, i32 size, i32 count, !"type", !"name", !"air.indirect_argument",
   leaf`, and an array of handles two ways:
     - a C array, `texture2d<float> t [[id(0)]] [8]`: count 8, size per element (8), and the leaf's location pair
       `!"air.location_index", i32 0, i32 1`;
     - a metal::array, `array<texture2d<float>, 8> t [[id(0)]]`: count 0, size of the whole array (64), and the
       length in the leaf's location pair, `i32 0, i32 8` (the translator reads it there too: meta/embedded.rs
       repeats_a_handle_by_location, Blender's array<sampler, 17>).
   Element k takes [[id(base + k)]] and its handle word is at offset + 8k: the shader reads it through the member's own
   LLVM type, `[N x ptr addrspace(1|2)]` under a datalayout with p:64:64:64, as the translator's reflection places it
   (meta/embedded.rs HANDLE_BYTES) and as -newArgumentEncoderWithArguments: lays out an arrayLength. */
#ifndef NVMTL_ARGBUF_ELEMS_H
#define NVMTL_ARGBUF_ELEMS_H
#include <stdlib.h>
#include <string.h>

#define NVMTL_ARGBUF_HANDLE_BYTES 8u
/* one field dictionary per element: a larger array keeps its first element only, as before (a bindless table of
   thousands of handles wants a lookup by range instead, not this) */
#define NVMTL_ARGBUF_MAX_ELEMENTS 4096u

/* The two integers after `!"air.location_index", ` in a leaf node line: the member's [[id]] inside its struct and its
   element count. 0 when the node has no such pair; *count is 1 when only the first integer is there. */
static inline int nvmtl_air_location_pair(const char *leaf, long *id, long *count)
{
    static const char key[] = "!\"air.location_index\", i32 ";
    const char *p = leaf ? strstr(leaf, key) : NULL;
    if (!p) return 0;
    char *end = NULL;
    *id = strtol(p + sizeof key - 1, &end, 10);
    *count = 1;
    if (end && !strncmp(end, ", i32 ", 6)) *count = strtol(end + 6, NULL, 10);
    return 1;
}

/* Elements of a handle member: 1 (and stride 0) unless the handle words of count x location-count elements fill the
   member exactly under one reading of its size (per element, as a C array's is, or the whole member, as a
   metal::array's is); anything else keeps one field, today's layout. */
static inline unsigned nvmtl_argbuf_handle_elements(long count, long size, long loc_count, unsigned *stride)
{
    *stride = 0;
    if (count < 0 || size <= 0 || loc_count < 1) return 1;
    const unsigned long c = count > 1 ? (unsigned long)count : 1ul, l = (unsigned long)loc_count;
    if (c > NVMTL_ARGBUF_MAX_ELEMENTS || l > NVMTL_ARGBUF_MAX_ELEMENTS) return 1;
    const unsigned long n = c * l, words = n * NVMTL_ARGBUF_HANDLE_BYTES, s = (unsigned long)size;
    if (n < 2 || n > NVMTL_ARGBUF_MAX_ELEMENTS) return 1;
    if (!(s == words || (count > 1 && s * c == words))) return 1;
    *stride = NVMTL_ARGBUF_HANDLE_BYTES;
    return (unsigned)n;
}
#endif
