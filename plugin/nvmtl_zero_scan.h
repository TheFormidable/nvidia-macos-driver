// nvmtl_zero_scan.h: whether a span of memory is all zero, for NVMTL_SURFIN_ZERO (NVMTLObjects.m). A surface whose CPU
// pages are all zero is not uploaded; its VRAM is zero-filled on the GPU instead, so the answer must be exact: every
// byte of the span is read before 1 is returned. A written surface usually answers after a few cache lines: the first
// and last 4 KB are read first, then (for rows) 16 rows spread over the span, then the whole span in 64-byte blocks,
// stopping at the first block with a set bit. Pure, so tools/test_zero_scan.c holds every answer on the host.
#ifndef NVMTL_ZERO_SCAN_H
#define NVMTL_ZERO_SCAN_H
#include <stddef.h>
#include <stdint.h>

typedef uint64_t __attribute__((__may_alias__)) nvmtl_zero_word_;

static inline int nvmtl_zero_bytes_(const unsigned char *p, size_t n)
{
    unsigned char acc = 0;
    for (size_t i = 0; i < n; i++) acc |= p[i];
    return acc == 0;
}
// the n bytes at p, in 8-byte words once p is aligned, eight words (one 64-byte block) per test
static inline int nvmtl_zero_block_(const unsigned char *p, size_t n)
{
    size_t head = (size_t)(-(uintptr_t)p & 7);
    if (head > n) head = n;
    if (!nvmtl_zero_bytes_(p, head)) return 0;
    p += head; n -= head;
    const nvmtl_zero_word_ *w = (const nvmtl_zero_word_ *)(const void *)p;
    for (; n >= 64; w += 8, n -= 64)
        if (w[0] | w[1] | w[2] | w[3] | w[4] | w[5] | w[6] | w[7]) return 0;
    for (; n >= 8; w++, n -= 8)
        if (*w) return 0;
    return nvmtl_zero_bytes_((const unsigned char *)(const void *)w, n);
}

// How the last answer was reached, for the census: 0 the first or last 4 KB, 1 a sampled row, 2 the whole span.
enum { NVMTL_ZERO_AT_EDGE = 0, NVMTL_ZERO_AT_ROW = 1, NVMTL_ZERO_AT_FULL = 2 };

// 1 when the n bytes at p are all zero. *stage (may be NULL) says which pass gave the answer.
static inline int nvmtl_zero_span_at(const void *base, size_t n, int *stage)
{
    const unsigned char *p = (const unsigned char *)base;
    const size_t e = n < 4096 ? n : 4096, t = n - e < 4096 ? n - e : 4096;
    if (stage) *stage = NVMTL_ZERO_AT_EDGE;
    if (!nvmtl_zero_block_(p, e) || !nvmtl_zero_block_(p + n - t, t)) return 0;
    if (stage) *stage = NVMTL_ZERO_AT_FULL;
    return nvmtl_zero_block_(p + e, n - e - t);
}
static inline int nvmtl_zero_span(const void *p, size_t n) { return nvmtl_zero_span_at(p, n, NULL); }

// 1 when the span row * h at base is all zero (every byte of every row, the padding past the pixels too). 16 rows
// spread over the span are read after the edges and before the rest. A span whose size does not fit a size_t is 0.
static inline int nvmtl_zero_rows_at(const void *base, size_t row, uint32_t h, int *stage)
{
    if (stage) *stage = NVMTL_ZERO_AT_EDGE;
    if (!row || !h) return 1;
    if (row > SIZE_MAX / h) return 0;
    const unsigned char *p = (const unsigned char *)base;
    const size_t n = row * h, e = n < 4096 ? n : 4096, t = n - e < 4096 ? n - e : 4096;
    if (!nvmtl_zero_block_(p, e) || !nvmtl_zero_block_(p + n - t, t)) return 0;
    if (stage) *stage = NVMTL_ZERO_AT_ROW;
    for (uint32_t k = 0; k < 16 && h > 2; k++) {
        const size_t y = 1 + (size_t)(h - 2) * k / 16;
        if (!nvmtl_zero_block_(p + y * row, row)) return 0;
    }
    if (stage) *stage = NVMTL_ZERO_AT_FULL;
    return nvmtl_zero_block_(p + e, n - e - t);
}
static inline int nvmtl_zero_rows(const void *base, size_t row, uint32_t h)
{
    return nvmtl_zero_rows_at(base, row, h, NULL);
}
#endif
