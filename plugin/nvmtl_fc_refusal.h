/* nvmtl_fc_refusal.h - which function-constant variant refusals are remembered for the process.
   nvmtl_translate_with_fc_cached (NVMTLLibraryLoad.m) keeps every variant it translated, in memory and on disk, but a
   variant the translator refused was translated again on every request, on the caller's thread (an uber-shader variant
   takes 300 to 2000 ms, NVMTL_FCWARM_RECORD_MS). The translator's function-constant entry (nvmtl_translate_fc) is a pure
   function of the AIR text, the stage and the values, besides the process's environment and the translator build, which
   do not change within a process; the per-thread options the plugin can set (threadgroup lengths, stage-in layout) are
   set and cleared around other translations, never around this one. It has no timeout and no retry that depends on time,
   and a panic aborts rather than returning. So the same request gets the same refusal and the same message, and
   remembering it changes only how long the next request takes.
   Only that refusal is remembered: not the translator being absent, not a variant whose one-module route (NVMTL_FC_SPEC=1)
   declined first (that route reads a cache entry that can change), not with NVMTL_FCCACHE_OFF, which keeps nothing.
   At most NVMTL_FC_REFUSALS_MAX are kept; past that a refusal is translated again each time, as before.
   What a repeat no longer produces is the translator's "re-translate FAILED" line and the NVMTL_FC_FAIL_DUMP and
   NVMTL_FC_DUMP_ALL files; NVMTL_FC_REFUSAL_MEMO=0 keeps and looks up nothing, so every request reaches the translator
   as in 3.0.5, with the rest of the cache still on.
   Pure so tools/test_fc_refusal.c proves the rule without Metal. */
#ifndef NVMTL_FC_REFUSAL_H
#define NVMTL_FC_REFUSAL_H

#define NVMTL_FC_REFUSALS_MAX 4096u

/* The way out: on unless NVMTL_FC_REFUSAL_MEMO starts with 0. The caller passes the cache switch ANDed with it as
   cache_on below, so 0 here means nothing is remembered and nothing is answered from memory. */
static inline int nvmtl_fc_refusal_memo_env_on(const char *value)
{
    return !(value && value[0] == '0');
}

/* 1 when a failed variant translation is to be remembered under its cache key. refused: the translator itself refused
   the request (the deterministic branch); cache_on: NVMTL_FCCACHE_OFF is not set; have_key: the request has a key;
   kept: how many refusals the process already keeps. */
static inline int nvmtl_fc_refusal_keep(int refused, int cache_on, int have_key, unsigned long kept)
{
    return refused && cache_on && have_key && kept < NVMTL_FC_REFUSALS_MAX;
}

/* 1 when a request is answered from a remembered refusal instead of being translated: only with the cache on, so
   NVMTL_FCCACHE_OFF translates every request, as before. */
static inline int nvmtl_fc_refusal_use(int cache_on, int remembered)
{
    return cache_on && remembered;
}

#endif
