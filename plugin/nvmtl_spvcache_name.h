/* nvmtl_spvcache_name.h - which shader-cache entries the stale sweep keeps. Entry names are
   <digest>.<stage>.<build tag><mode suffixes>.c3.spv, where the build tag names the translator and the suffixes
   (-u2, -p5, -as1, ...) name the modes the process translated in. Pure, so tools/test_app_profile.c proves it. */
#ifndef NVMTL_SPVCACHE_NAME_H
#define NVMTL_SPVCACHE_NAME_H
#include <stdio.h>
#include <string.h>
/* Room for the build tag (sha256-<64 hex>) and every mode suffix, with space to spare. */
#define NVMTL_SPVCACHE_TAG_MAX 160
/* Writes the cache tag for translator build `build` in the given modes. Returns 1, or 0 when the whole tag does not fit
   in `cap` (a cut tag would let two modes share entries); on 0 the caller must not cache. */
static inline int nvmtl_spvcache_mode_tag(char *tag, size_t cap, const char *build, int unroll, int canonical,
                                          int argbuf_samplers, int no_ptr_phi_load, int no_pixel_hw_filter)
{
    if (!tag || !cap || !build) return 0;
    const int k = snprintf(tag, cap, "%s%s%s%s%s%s", build, unroll ? "-u2" : "", canonical ? "-p5" : "",
                           argbuf_samplers ? "-as1" : "", no_ptr_phi_load ? "-np" : "", no_pixel_hw_filter ? "-nf" : "");
    return k > 0 && (size_t)k < cap;
}
/* Appends to `tag` the mark of the translator's way back from the whole-object views of pull request 524 of the
   original project: "-wv0" for NVMTL_WHOLE_VIEWS=0 (`whole_views` is the variable's value after the settings file
   was read, NULL when unset). The translator reads it once (translator/translator/src/native/view_switches.rs) and,
   as here, a value that starts with 0 turns the change off, so a module translated with it off is never served to a
   process that has it on, nor the other way. Returns 1, or 0 when the tag with its mark does not fit in `cap`; on 0
   the tag is left as it was and the caller must not cache. */
static inline int nvmtl_spvcache_escape_tag(char *tag, size_t cap, const char *whole_views)
{
    if (!tag || !cap) return 0;
    const size_t n = strnlen(tag, cap);
    if (n == cap) return 0;
    const int k = snprintf(tag + n, cap - n, "%s", whole_views && whole_views[0] == '0' ? "-wv0" : "");
    if (k < 0 || (size_t)k >= cap - n) { tag[n] = 0; return 0; }
    return 1;
}
/* Appends to `tag` the mark of the translator's way back from its device-address construction of a module refused for a
   pointer select between two buffer roots: "-ar0" for NVMTL_XLATE_ADDR_RETRY=0 (`addr_retry` is the variable's value
   after the settings file was read, NULL when unset). The translator reads it once (translator/translator/src/
   construction.rs, cross_root_select_refusal) and, as here, a value that starts with 0 turns the retry off. Without the
   mark a module translated with the retry on would still be served from the cache after it was turned off. Returns 1,
   or 0 when the tag with its mark does not fit in `cap`; on 0 the tag is left as it was and the caller must not cache. */
static inline int nvmtl_spvcache_addr_retry_tag(char *tag, size_t cap, const char *addr_retry)
{
    if (!tag || !cap) return 0;
    const size_t n = strnlen(tag, cap);
    if (n == cap) return 0;
    const int k = snprintf(tag + n, cap - n, "%s", addr_retry && addr_retry[0] == '0' ? "-ar0" : "");
    if (k < 0 || (size_t)k >= cap - n) { tag[n] = 0; return 0; }
    return 1;
}
/* 1 when `name` is an entry of translator build `build` (any mode); 0 for another build's entry or a foreign file. */
static inline int nvmtl_spvcache_same_build(const char *name, const char *build)
{
    const size_t n = strlen(name), bn = strlen(build);
    if (!bn || n < 7 || strcmp(name + n - 7, ".c3.spv")) return 0;
    for (const char *at = strstr(name, build); at; at = strstr(at + 1, build))
        if (at > name && at[-1] == '.' && (at[bn] == '-' || at[bn] == '.')) return 1;
    return 0;
}
/* The library-index record's file name: idx2-<library sha>.<full cache tag>.plist. Returns 0 when it does not fit. */
static inline int nvmtl_idx_record_name(char *out, size_t cap, const char *libkey, const char *tag)
{
    if (!out || !cap || !libkey || !tag || !*tag) return 0;
    const int k = snprintf(out, cap, "%s.%s.plist", libkey, tag);
    return k > 0 && (size_t)k < cap;
}
#endif
