/* test_zero_scan.c - the all-zero scan of NVMTL_SURFIN_ZERO (plugin/nvmtl_zero_scan.h): 1 only when every byte of the
   span is zero, at every length and alignment, and three broken copies of it are each caught by these arms.
   Build and run anywhere:  cc -O2 -Wall -I plugin tools/test_zero_scan.c -o /tmp/test_zero_scan && /tmp/test_zero_scan */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "nvmtl_zero_scan.h"

static int fails;
#define ARM(name, cond) do { int ok_ = (cond); printf("%s %s\n", ok_ ? "ok  " : "FAIL", name); if (!ok_) fails++; } while (0)

typedef int (*scan_fn)(const void *base, size_t row, uint32_t h);

static int real_rows(const void *b, size_t row, uint32_t h) { return nvmtl_zero_rows(b, row, h); }
static int real_span(const void *b, size_t row, uint32_t h) { return nvmtl_zero_span(b, row * h); }

/* control 1: the whole pass in 8-byte words only, the bytes past the last whole word not read */
static int bad_tail(const void *b, size_t row, uint32_t h)
{
    const unsigned char *p = b; const size_t n = row * h;
    for (size_t i = 0; i + 8 <= n; i += 8) { uint64_t w; memcpy(&w, p + i, 8); if (w) return 0; }
    return 1;
}
/* control 2: the edges and the 16 sampled rows, and nothing else */
static int bad_sampled(const void *b, size_t row, uint32_t h)
{
    const unsigned char *p = b; const size_t n = row * h, e = n < 4096 ? n : 4096, t = n - e < 4096 ? n - e : 4096;
    if (!nvmtl_zero_bytes_(p, e) || !nvmtl_zero_bytes_(p + n - t, t)) return 0;
    for (uint32_t k = 0; k < 16 && h > 2; k++) if (!nvmtl_zero_bytes_(p + (1 + (size_t)(h - 2) * k / 16) * row, row)) return 0;
    return 1;
}
/* control 3: the whole pass leaves out the last 64-byte block before the last 4 KB */
static int bad_last_block(const void *b, size_t row, uint32_t h)
{
    const unsigned char *p = b; const size_t n = row * h, e = n < 4096 ? n : 4096, t = n - e < 4096 ? n - e : 4096;
    if (!nvmtl_zero_bytes_(p, e) || !nvmtl_zero_bytes_(p + n - t, t)) return 0;
    const size_t mid = n - e - t;
    return nvmtl_zero_bytes_(p + e, mid > 64 ? mid - 64 : 0);
}

static unsigned char small[64 + 300 + 64];
static unsigned char *big;
enum { BIG_ROW = 8192, BIG_H = 1104, BIG_W_BYTES = 1920 * 4 };   /* 9,043,968 bytes, a 1920-wide capture with padding */

/* every arm, against fn; returns how many failed (quiet when quiet is set, for the controls) */
static int arms(scan_fn fn, int quiet)
{
    int bad = 0;
#define A(name, cond) do { int ok_ = (cond); if (!quiet) printf("%s %s\n", ok_ ? "ok  " : "FAIL", name); if (!ok_) bad++; } while (0)
    int all = 1, one = 1;
    for (size_t off = 0; off < 64; off++)
        for (size_t len = 0; len <= 300; len++) {
            memset(small, 0, sizeof small);
            if (!fn(small + off, len, 1)) all = 0;
            for (size_t at = 0; at < len; at++) {
                small[off + at] = (unsigned char)(at & 1 ? 0x80 : 0x01);
                if (fn(small + off, len, 1)) one = 0;
                small[off + at] = 0;
            }
        }
    A("every length 0..300 at every offset 0..63, all zero, answers 1", all);
    A("one set bit at every byte position of those answers 0", one);
    memset(big, 0, (size_t)BIG_ROW * BIG_H);
    A("a zero 9,043,968-byte surface answers 1", fn(big, BIG_ROW, BIG_H));
    static const struct { const char *what; size_t at; } spots[] = {
        { "the first byte", 0 },
        { "the last byte", (size_t)BIG_ROW * BIG_H - 1 },
        { "the last byte of a padding column (row 500)", (size_t)500 * BIG_ROW + BIG_ROW - 1 },
        { "the first byte of the last row", (size_t)(BIG_H - 1) * BIG_ROW },
        { "a pixel of a row no sample reads (row 2)", (size_t)2 * BIG_ROW + 100 },
        { "the last byte before the last 4 KB", (size_t)BIG_ROW * BIG_H - 4096 - 1 },
        { "the first byte after the first 4 KB", 4096 },
        { "the byte past the pixels in the middle row", (size_t)552 * BIG_ROW + BIG_W_BYTES },
    };
    for (unsigned i = 0; i < sizeof spots / sizeof spots[0]; i++) {
        char name[160]; snprintf(name, sizeof name, "one byte set at %s answers 0", spots[i].what);
        big[spots[i].at] = 0x10; A(name, !fn(big, BIG_ROW, BIG_H)); big[spots[i].at] = 0;
    }
    memset(small, 0, sizeof small);
    A("h = 1, row = 32: zero answers 1", fn(small, 32, 1));
    small[31] = 1; A("h = 1, row = 32: its last byte set answers 0", !fn(small, 32, 1)); small[31] = 0;
    small[32 + 5] = 1; A("h = 2, row = 32: a byte of the second row answers 0", !fn(small, 32, 2)); small[32 + 5] = 0;
    small[64 + 31] = 1; A("h = 3, row = 32: the last byte of the third row answers 0", !fn(small, 32, 3)); small[64 + 31] = 0;
#undef A
    return bad;
}

int main(void)
{
    big = calloc((size_t)BIG_ROW * BIG_H, 1);
    if (!big) { puts("FAIL no memory"); return 1; }
    printf("nvmtl_zero_rows:\n"); fails += arms(real_rows, 0);
    printf("nvmtl_zero_span:\n"); fails += arms(real_span, 0);

    int st = -1;
    memset(big, 0, (size_t)BIG_ROW * BIG_H); big[0] = 1;
    ARM("a set first byte is answered at the edge", !nvmtl_zero_rows_at(big, BIG_ROW, BIG_H, &st) && st == NVMTL_ZERO_AT_EDGE);
    big[0] = 0; big[(size_t)552 * BIG_ROW + 1] = 1;
    st = -1; nvmtl_zero_rows_at(big, BIG_ROW, BIG_H, &st);
    ARM("a set byte in a sampled row is answered there", st == NVMTL_ZERO_AT_ROW);
    big[(size_t)552 * BIG_ROW + 1] = 0; big[(size_t)2 * BIG_ROW + 1] = 1;
    ARM("a set byte only the whole pass reads is answered there (the census counts it)",
        !nvmtl_zero_rows_at(big, BIG_ROW, BIG_H, &st) && st == NVMTL_ZERO_AT_FULL);
    big[(size_t)2 * BIG_ROW + 1] = 0;
    ARM("a zero span is answered by the whole pass", nvmtl_zero_rows_at(big, BIG_ROW, BIG_H, &st) && st == NVMTL_ZERO_AT_FULL);
    ARM("a span whose size does not fit answers 0 without reading", !nvmtl_zero_rows((const void *)16, SIZE_MAX / 2, 3));
    ARM("an empty span answers 1", nvmtl_zero_rows(big, 0, 5) && nvmtl_zero_rows(big, 64, 0) && nvmtl_zero_span(big, 0));

    ARM("control: a scan that skips the bytes past the last word is caught", arms(bad_tail, 1) > 0);
    ARM("control: a scan that stops at the sampled rows is caught", arms(bad_sampled, 1) > 0);
    ARM("control: a scan that leaves out the last block is caught", arms(bad_last_block, 1) > 0);
    free(big);
    printf("%s: %d failed\n", fails ? "FAIL" : "PASS", fails);
    return fails ? 1 : 0;
}
