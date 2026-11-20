/* nvmtl_repeat_bind.h - when a render encoder may skip work it already did.
   Per-draw savings on the render encoder, each with its own switch so a rendering glitch can be A/B'd against it
   without a new build (the switches are read from the environment, which a release plugin fills from nvrm610.conf):
     NVMTL_NO_PSO_SKIP=1  set the pipeline again even when it is the one already bound inside the begun pass
     NVMTL_NO_SV_MEMO=1   look the sampler variant up on every draw instead of reusing this encoder's last choice
   and a third, written like the newer switches (on unless set to 0):
     NVMTL_DS_SKIP=0      apply a depth-stencil state again even when it is the one this encoder last applied
   The decisions are pure so tools/test_repeat_bind.c proves them without Metal. The idea comes from the NvGfx project
   (watchthelight-labs/NvGfx); this implementation is our own. */
#ifndef NVMTL_REPEAT_BIND_H
#define NVMTL_REPEAT_BIND_H
#include <stddef.h>

/* a switch that turns a saving OFF: set, non-empty, and not "0" */
static inline int nvmtl_switch_is_off(const char *value)
{
    return value && *value && !(value[0] == '0' && value[1] == 0);
}

/* The same pipeline again inside a begun pass changes nothing. Not inside an open pass-time pool, where every bind
   belongs to the pool. */
static inline int nvmtl_pso_skip(int skipOn, const void *incoming, const void *bound, int passBegun, int passTimePoolOpen)
{
    return skipOn && incoming && incoming == bound && passBegun && !passTimePoolOpen;
}

/* The encoder's last sampler-variant choice stands while the pipeline and the value-snapshot key are the same objects.
   A refused variant is remembered too (the caller stores a sentinel), so a draw that cannot be specialised does not
   retry the translator on every call. */
static inline int nvmtl_sv_memo_hit(int memoOn, const void *lastPipeline, const void *pipeline,
                                    const void *lastKey, const void *key, int haveChoice)
{
    return memoOn && haveChoice && pipeline && key && lastPipeline == pipeline && lastKey == key;
}

/* setDepthStencilState with the state object this encoder last applied changes nothing while the command buffer still
   holds what that application recorded: the same VkCommandBuffer (cbId, its begin ordinal), no render pass begun since
   (epoch, which every pass begin advances), a pipeline with a depth attachment, and the same stencil attachment as then
   (so the same calls would be made). Depth bounds and the stencil reference are applied whenever they change, and the
   stencil export emulation restores what it changes, so nothing else can have moved this state in between. A nil state
   is never skipped. NVMTL_DS_SKIP=0 applies it again every time. */
static inline int nvmtl_ds_skip(int skipOn, int passBegun, const void *incoming, const void *current, const void *applied,
                                int hasDepth, int hasStencil, int appliedStencil,
                                unsigned appliedEpoch, unsigned epoch, unsigned appliedCb, unsigned cbId)
{
    return skipOn && passBegun && incoming && incoming == current && incoming == applied && hasDepth
        && hasStencil == appliedStencil && appliedEpoch == epoch && appliedCb == cbId;
}

#endif
