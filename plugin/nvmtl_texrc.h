/* nvmtl_texrc.h - the texture recycle cache: dead textures of a recurring shape keep their image and its memory.
   Apple's OpenGL renderer makes a new MTLTexture for every glBlitFramebuffer and drops it a few command buffers later.
   Each such texture cost four kernel round trips that have nothing to do with its content: the memory allocation and
   the image's own GPU virtual range (allocated and mapped when the memory is bound) on creation, and the two frees on
   destruction. A dead image whose memory is its own is therefore not destroyed once the image grace lets it go (nothing
   open or in flight can read it then): its VkImage stays bound to its VkDeviceMemory and the next texture created with
   exactly the same VkImageCreateInfo takes both. A new MTLTexture's content is undefined, so the old bytes are legal; the
   plugin's layout tracking of the new texture starts at UNDEFINED, which every transition may leave from.
   The cache is bounded in entries and bytes, lets an entry go after a second without reuse (looked at when the image
   grace reaps after a completed submit and at every park, so a process that stops submitting keeps what it parked, at
   most the byte bound, until it submits again), and is the first thing given back when an allocation meets the VRAM gate. Parked memory stays counted as allocated VRAM all the while.
   NVMTL_TEX_RECYCLE=0 destroys every dead image as before. WindowServer never parks: its textures follow its own
   pattern, and memory it held back would be a stall for the whole desktop to find.
   The rules are pure so tools/test_texrc.c proves them without a GPU. */
#ifndef NVMTL_TEXRC_H
#define NVMTL_TEXRC_H
#include <stdint.h>
#include <string.h>

#define NVMTL_TEXRC_N      32u                 /* parked images at once */
#define NVMTL_TEXRC_BYTES  (128ull << 20)      /* parked memory at once */
#define NVMTL_TEXRC_MAX1   (32ull << 20)       /* the largest one image's memory may be to be parked */
#define NVMTL_TEXRC_AGE_NS (1000000000ull)     /* an entry not taken within a second is let go */

/* Everything that makes two images interchangeable: the VkImageCreateInfo the plugin passed (its pNext is always NULL
   on this path, sharing is exclusive, tiling optimal, one sample, initial layout undefined). The memory follows from
   it: the same requirements, the device-local type, a dedicated allocation of the same size. */
typedef struct { uint32_t flags, type, format, w, h, d, mips, layers, usage; } nvmtl_texrc_key;
typedef struct { nvmtl_texrc_key key; void *img, *mem; uint64_t bytes, parked_ns; } nvmtl_texrc_entry;
/* entries in the order they were parked, the oldest first */
typedef struct { nvmtl_texrc_entry e[NVMTL_TEXRC_N]; uint32_t n; uint64_t bytes;
                 uint64_t parked, reused, aged, evicted, given; } nvmtl_texrc_state;

static inline nvmtl_texrc_key nvmtl_texrc_key_of(uint32_t flags, uint32_t type, uint32_t format, uint32_t w, uint32_t h, uint32_t d,
                                                 uint32_t mips, uint32_t layers, uint32_t usage)
{
    nvmtl_texrc_key k = { flags, type, format, w, h, d, mips, layers, usage };
    return k;
}
static inline int nvmtl_texrc_key_eq(const nvmtl_texrc_key *a, const nvmtl_texrc_key *b)
{
    return a->flags == b->flags && a->type == b->type && a->format == b->format && a->w == b->w && a->h == b->h && a->d == b->d
        && a->mips == b->mips && a->layers == b->layers && a->usage == b->usage;
}

/* On unless the variable is set to 0, and never in WindowServer. */
static inline int nvmtl_texrc_enabled(const char *env, const char *progname)
{
    if (env && env[0] == '0') return 0;
    return !(progname && !strcmp(progname, "WindowServer"));
}

/* A dead image may be parked when the cache is on, the image grace is (only the grace's release point knows that
   nothing can read it any more), the image and memory are still the very ones its recyclable creation made (a texture
   re-backed or migrated by the residency pager has others), the memory is its own and in VRAM, and it fits an entry. */
static inline int nvmtl_texrc_eligible(int on, int grace_on, const void *img, const void *mem, const void *made_img, const void *made_mem,
                                       int placed, int pooled, int sparse, int sysmem, uint64_t bytes)
{
    return on && grace_on && img && mem && img == made_img && mem == made_mem && !placed && !pooled && !sparse && !sysmem
        && bytes && bytes <= NVMTL_TEXRC_MAX1;
}

static inline void nvmtl_texrc_remove(nvmtl_texrc_state *s, uint32_t at)
{
    s->bytes -= s->e[at].bytes;
    memmove(&s->e[at], &s->e[at + 1], (size_t)(s->n - at - 1) * sizeof s->e[0]);
    s->n--;
}

/* Entries parked more than NVMTL_TEXRC_AGE_NS before now leave, into out (room for NVMTL_TEXRC_N); returns how many.
   The caller destroys them outside its lock. */
static inline uint32_t nvmtl_texrc_age(nvmtl_texrc_state *s, uint64_t now, nvmtl_texrc_entry *out)
{
    uint32_t k = 0;
    while (s->n && now > s->e[0].parked_ns && now - s->e[0].parked_ns > NVMTL_TEXRC_AGE_NS) {
        out[k++] = s->e[0]; nvmtl_texrc_remove(s, 0); s->aged++; }
    return k;
}

/* Parks e (bytes no more than NVMTL_TEXRC_MAX1: nvmtl_texrc_eligible). What has to leave to make room - the aged
   entries, then the oldest while the count or the bytes would pass their bound - goes to out (room for NVMTL_TEXRC_N);
   returns how many. */
static inline uint32_t nvmtl_texrc_put(nvmtl_texrc_state *s, const nvmtl_texrc_entry *e, uint64_t now, nvmtl_texrc_entry *out)
{
    uint32_t k = nvmtl_texrc_age(s, now, out);
    while (s->n && (s->n >= NVMTL_TEXRC_N || s->bytes + e->bytes > NVMTL_TEXRC_BYTES)) {
        out[k++] = s->e[0]; nvmtl_texrc_remove(s, 0); s->evicted++; }
    s->e[s->n] = *e; s->e[s->n].parked_ns = now; s->bytes += e->bytes; s->n++; s->parked++;
    return k;
}

/* Takes the most recently parked entry of exactly this key into *out: 1, or 0 when there is none. */
static inline int nvmtl_texrc_take(nvmtl_texrc_state *s, const nvmtl_texrc_key *key, nvmtl_texrc_entry *out)
{
    for (uint32_t i = s->n; i-- > 0; )
        if (nvmtl_texrc_key_eq(&s->e[i].key, key)) { *out = s->e[i]; nvmtl_texrc_remove(s, i); s->reused++; return 1; }
    return 0;
}

/* Everything parked leaves, into out (room for NVMTL_TEXRC_N): the VRAM gate wants memory. Returns how many. */
static inline uint32_t nvmtl_texrc_give_back(nvmtl_texrc_state *s, nvmtl_texrc_entry *out)
{
    const uint32_t k = s->n;
    memcpy(out, s->e, (size_t)k * sizeof s->e[0]);
    s->n = 0; s->bytes = 0; s->given += k;
    return k;
}

#endif
