/* nvmtl_a8_fetch.h - framebuffer-fetch reads of A8 colour attachments, remapped in the fragment shader's SPIR-V.
   Metal's A8Unorm is held as Vulkan R8. A sampled view swizzles R into A, but an input attachment's view must have
   identity component mappings (VkWriteDescriptorSet-00336), so an A8 attachment's [[color(n)]] read returns (r,0,0,1)
   where Metal returns (0,0,0,r). nvmtl_spirv_a8_fetch puts an OpVectorShuffle behind every OpImageRead of an input
   attachment the translator bound at set NVMTL_FETCH_SET, binding NVMTL_FETCH_BINDING + n, for each n in mask (the
   pipeline's A8 attachments, not the format of the texture that is bound later). Pure, so
   tools/test_a8_framebuffer_fetch.py runs it on the host. From the HighDelay/nvidia-macos-driver-nvk fork.
   Returns 0 with *result NULL when nothing needs a remap, 1 with a malloc'd module in *result, -1 for a module it
   cannot read (the pipeline is then refused, as a read it cannot remap would return the wrong channel). */
#ifndef NVMTL_A8_FETCH_H
#define NVMTL_A8_FETCH_H
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#if !defined(NVMTL_FETCH_SET) || !defined(NVMTL_FETCH_BINDING) || !defined(NVMTL_NCOL)
#error "nvmtl_a8_fetch.h needs NVMTL_FETCH_SET, NVMTL_FETCH_BINDING and NVMTL_NCOL"
#endif

static int nvmtl_spirv_a8_fetch(const void *code, size_t bytes, uint32_t mask, uint32_t **result, size_t *result_bytes)
{
    *result = NULL; *result_bytes = 0;
    if (!mask) return 0;
    const uint32_t *w = code;
    if (!w || bytes % 4 || bytes < 20 || w[0] != 0x07230203u) return -1;
    const size_t nw = bytes / 4;
    const uint32_t bound = w[3];
    if (!bound || bound > (1u << 24)) return -1;
    /* per id: the attachment slot + 1 (low 4 bits), a subpass-data image type, a 4-component vector type, set 3 */
    enum { SLOT = 15, SUBPASS = 32, VEC4 = 64, SET = 128 };
    uint8_t *ids = calloc(bound, 1);
    if (!ids) return -1;
    size_t count = 0;
    for (size_t i = 5; i < nw; ) {
        uint32_t wc = w[i] >> 16, op = w[i] & 0xffff;
        if (!wc || wc > nw - i) goto fail;
        if (op == 71 && wc == 4) {   /* OpDecorate: DescriptorSet (34), Binding (33) */
            uint32_t id = w[i + 1]; if (id >= bound) goto fail;
            if (w[i + 2] == 34 && w[i + 3] == NVMTL_FETCH_SET) ids[id] |= SET;
            if (w[i + 2] == 33 && w[i + 3] >= NVMTL_FETCH_BINDING && w[i + 3] < NVMTL_FETCH_BINDING + NVMTL_NCOL)
                ids[id] |= (uint8_t)(w[i + 3] - NVMTL_FETCH_BINDING + 1);
        } else if ((op == 23 && wc == 4) || (op == 25 && wc >= 9)) {   /* OpTypeVector, OpTypeImage */
            uint32_t id = w[i + 1]; if (id >= bound) goto fail;
            if (op == 23 && w[i + 3] == 4) ids[id] |= VEC4;
            if (op == 25 && w[i + 3] == 6) ids[id] |= SUBPASS;   /* Dim SubpassData */
        }
        i += wc;
    }
    for (uint32_t id = 0; id < bound; id++)
        if (!(ids[id] & SET)) ids[id] &= (uint8_t)~SLOT;   /* a binding in another set is not an input attachment */
    for (size_t i = 5; i < nw; i += w[i] >> 16) {
        uint32_t wc = w[i] >> 16, op = w[i] & 0xffff;
        /* OpLoad of a subpass image, OpAccessChain / OpInBoundsAccessChain, OpCopyObject carry the slot along */
        if ((op == 61 && wc >= 4) || ((op == 65 || op == 66) && wc >= 4) || (op == 83 && wc == 4)) {
            uint32_t type = w[i + 1], id = w[i + 2], from = w[i + 3];
            if (type >= bound || id >= bound || from >= bound) goto fail;
            if (op != 61 || (ids[type] & SUBPASS)) ids[id] |= ids[from] & SLOT;
        } else if (op == 98 && wc >= 5) {   /* OpImageRead */
            uint32_t type = w[i + 1], id = w[i + 2], image = w[i + 3];
            if (type >= bound || id >= bound || image >= bound) goto fail;
            uint32_t slot = ids[image] & SLOT;
            if (slot && (mask & (1u << (slot - 1)))) {
                if (!(ids[type] & VEC4)) goto fail;
                count++;
            }
        }
    }
    if (!count) { free(ids); return 0; }
    if (count > UINT32_MAX - bound || count > (SIZE_MAX / 4 - nw) / 9) goto fail;
    size_t out_words = nw + count * 9, at = 5;
    uint32_t *out = malloc(out_words * 4);
    if (!out) goto fail;
    memcpy(out, w, 20); out[3] = bound + (uint32_t)count;
    uint32_t fresh = bound;
    for (size_t i = 5; i < nw; ) {
        uint32_t wc = w[i] >> 16, op = w[i] & 0xffff;
        memcpy(out + at, w + i, wc * 4);
        uint32_t slot = op == 98 && wc >= 5 ? ids[w[i + 3]] & SLOT : 0;
        if (slot && (mask & (1u << (slot - 1)))) {
            uint32_t type = w[i + 1], original = w[i + 2];
            out[at + 2] = fresh; at += wc;
            /* the read's result id becomes (0,0,0,r): components 1, 1, 1 of the read (zeros) and its component 0 */
            const uint32_t shuffle[] = { (9u << 16) | 79u, type, original, fresh, fresh, 1, 1, 1, 0 };
            memcpy(out + at, shuffle, sizeof shuffle); at += 9; fresh++;
        } else at += wc;
        i += wc;
    }
    free(ids); *result = out; *result_bytes = out_words * 4; return 1;
fail:
    free(ids); return -1;
}
#endif
