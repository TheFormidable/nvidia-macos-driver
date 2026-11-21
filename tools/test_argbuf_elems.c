/* test_argbuf_elems.c - the [[id]]s and handle offsets of an argument-buffer array of handles (plugin/nvmtl_argbuf_elems.h),
   from member tuples and leaf nodes as Apple's compiler writes them (translator/translator/src/meta/embedded.rs tests,
   translator/translator/src/native/tests/buffers.rs, tools/card/xlate_paths.m case_abarray).
   Build and run anywhere:  cc -Wall -Wextra -I plugin tools/test_argbuf_elems.c -o /tmp/t && /tmp/t */
#include "nvmtl_argbuf_elems.h"
#include <stdio.h>
static int fails;
#define ARM(name, cond) do { const int ok = !!(cond); printf("%s  %s\n", ok ? "ok  " : "FAIL", name); if (!ok) fails++; } while (0)

static unsigned elems(long count, long size, const char *leaf, unsigned *stride, long *id)
{
    long lcount = 1; *id = -1;
    if (!nvmtl_air_location_pair(leaf, id, &lcount)) { *stride = 0; return 1; }
    return nvmtl_argbuf_handle_elements(count, size, lcount, stride);
}

int main(void)
{
    unsigned st; long id;
    /* abarray-c: texture2d<float> t [[id(0)]] [8]: count 8, size per element */
    unsigned n = elems(8, 8, "!30 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 0, i32 1, !\"air.read\", "
                             "!\"air.arg_type_name\", !\"texture2d<float, read>\", !\"air.arg_name\", !\"t\"}", &st, &id);
    ARM("a C array of 8 textures takes ids 0-7, 8 bytes apart", n == 8 && st == 8 && id == 0);
    /* abarray-a / -s: array<texture2d<float>, 8> t [[id(0)]]: count 0, size of the whole array, length in the pair */
    n = elems(0, 64, "!30 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 0, i32 8, !\"air.sample\", "
                     "!\"air.arg_type_name\", !\"array<texture2d<float, sample>, 8>\"}", &st, &id);
    ARM("a metal::array of 8 textures takes ids 0-7, 8 bytes apart", n == 8 && st == 8 && id == 0);
    /* meta/embedded.rs embedded_fixed_depth_array: array<depth2d_array<float, sample>, 2> at id 4, size 16 */
    n = elems(0, 16, "!20 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 4, i32 2, !\"air.sample\"}", &st, &id);
    ARM("array<depth2d_array, 2> at id 4: ids 4-5", n == 2 && st == 8 && id == 4);
    /* meta/embedded.rs: texture2d<float, sample> slices [32] */
    n = elems(32, 8, "!20 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 0, i32 1, !\"air.sample\"}", &st, &id);
    ARM("a C array of 32 textures", n == 32 && st == 8);
    /* meta/embedded.rs: half4 *bufs [2] [[id(?)]] (a buffer pointer array) */
    n = elems(2, 8, "!20 = !{i32 0, !\"air.buffer\", !\"air.location_index\", i32 12, i32 1, !\"air.read\"}", &st, &id);
    ARM("a C array of 2 buffer pointers at id 12: ids 12-13", n == 2 && st == 8 && id == 12);
    /* Blender: array<sampler, 17> */
    n = elems(0, 136, "!9 = !{i32 0, !\"air.sampler\", !\"air.location_index\", i32 3, i32 17}", &st, &id);
    ARM("array<sampler, 17> at id 3: 17 ids", n == 17 && st == 8 && id == 3);
    /* the other reading of size (the whole C array) is accepted too */
    n = elems(8, 64, "!30 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 0, i32 1}", &st, &id);
    ARM("a C array whose size is the whole array", n == 8 && st == 8);
    /* a C array of metal::arrays: array<texture2d, 4> t [2]: 8 handles */
    n = elems(2, 32, "!30 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 0, i32 4}", &st, &id);
    ARM("a C array of two metal::arrays of 4", n == 8 && st == 8);

    /* everything else keeps one field, today's layout */
    n = elems(0, 8, "!30 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 2, i32 1}", &st, &id);
    ARM("a single texture is one field", n == 1 && st == 0 && id == 2);
    n = elems(0, 8, "!30 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 2}", &st, &id);
    ARM("a location without a count is one field", n == 1);
    n = elems(0, 8, "!30 = !{i32 0, !\"air.texture\", !\"air.arg_name\", !\"t\"}", &st, &id);
    ARM("a leaf without a location is one field", n == 1);
    n = elems(0, 48, "!30 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 0, i32 8}", &st, &id);
    ARM("handle words that do not fill the member (48 of 64 bytes) keep one field", n == 1 && st == 0);
    n = elems(4, 16, "!30 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 0, i32 1}", &st, &id);
    ARM("a C array with 16-byte elements is not a handle array", n == 1);
    n = elems(0, 8L * 5000, "!30 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 0, i32 5000}", &st, &id);
    ARM("above NVMTL_ARGBUF_MAX_ELEMENTS: one field", n == 1);
    n = elems(-1, 8, "!30 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 0, i32 8}", &st, &id);
    ARM("a negative count is one field", n == 1);
    n = elems(0, 0, "!30 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 0, i32 8}", &st, &id);
    ARM("a zero size is one field", n == 1);
    n = elems(0, 64, "!30 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 0, i32 0}", &st, &id);
    ARM("a location count of 0 is one field", n == 1);
    ARM("a null leaf has no location", !nvmtl_air_location_pair(NULL, &id, &id));
    /* the cases the size check alone would not catch */
    {
        long lid = -1, lc = -1;
        ARM("a location without a count reads as count 1",
            nvmtl_air_location_pair("!30 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 2}", &lid, &lc) && lid == 2 && lc == 1);
    }
    n = elems(-1, 64, "!30 = !{i32 0, !\"air.texture\", !\"air.location_index\", i32 0, i32 8}", &st, &id);
    ARM("a negative count is one field even when the words would fill the member", n == 1 && st == 0);
    /* count x location count wraps to 4 in 64 bits: each factor is bounded before they are multiplied */
    ARM("a count whose product with the location count wraps is one field",
        nvmtl_argbuf_handle_elements((1L << 62) + 1, 32, 4, &st) == 1 && st == 0);
    printf("%s\n", fails ? "FAILED" : "all ok");
    return fails != 0;
}
