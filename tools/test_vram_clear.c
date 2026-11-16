/* test_vram_clear.c - what the framebuffer does with NVRM's answer to a copy-engine clear of a new block of video
   memory (kexts/NVRMFB/fb/nvrm-vram-clear.h), held against its rules for every answer.
   Build and run anywhere:  cc -O2 -Wall -I kexts/NVRMFB/fb tools/test_vram_clear.c -o /tmp/test_vram_clear && /tmp/test_vram_clear

   The arms that matter most: a block the engine may still write into is never filled-and-handed-out (it is kept), an
   older NVRM and a block that reads as not zero end the asking for the boot, and nothing that ends the asking is
   ever undone by a later answer. */
#include <stdio.h>
#include "nvrm-vram-clear.h"

#define DONE 0u
#define NOT_RUN 1u
#define LOST 2u

static int fails;
#define ARM(name, cond) do { int ok_ = (cond); printf("%s %s\n", ok_ ? "ok  " : "FAIL", name); if (!ok_) fails++; } while (0)

/* One whole ask, as nvrm-fb.cpp's vramClear runs it: the call, then the probes when the answer wants them. */
static int ask(int *state, int known, unsigned result, unsigned long long cleared, unsigned long long bytes, int readsZero)
{
    int what = nvrm_vc_answer(state, known, result, cleared, bytes);
    if (what == NVRM_VC_PROBE) what = nvrm_vc_probed(state, readsZero);
    return what;
}

int main(void)
{
    const unsigned long long B = 9043968ull;
    int s;

    s = NVRM_VC_ON;
    ARM("the engine finished and the probes read zero: the block is zero and the clear stays on",
        ask(&s, 1, DONE, B, B, 1) == NVRM_VC_ZERO && s == NVRM_VC_ON);
    s = NVRM_VC_ON;
    ARM("the engine finished and a probe is not zero: filled from the processor, and no more asking this boot",
        ask(&s, 1, DONE, B, B, 0) == NVRM_VC_FILL && s == NVRM_VC_DOWN);
    s = NVRM_VC_ON;
    ARM("the engine says it finished but cleared less than the block: filled from the processor, still on",
        ask(&s, 1, DONE, B - 65536, B, 1) == NVRM_VC_FILL && s == NVRM_VC_ON);
    s = NVRM_VC_ON;
    ARM("the engine cleared more than the block (the RM's memory is longer): it is zero where it counts",
        ask(&s, 1, DONE, B + 65536, B, 1) == NVRM_VC_ZERO && s == NVRM_VC_ON);
    s = NVRM_VC_ON;
    ARM("nothing was sent (GPU in standby, queue paused): filled from the processor, the next block asks again",
        ask(&s, 1, NOT_RUN, 0, B, 1) == NVRM_VC_FILL && s == NVRM_VC_ON);
    s = NVRM_VC_ON;
    ARM("work was sent and its end not seen: the block is kept until restart, and no more asking this boot",
        ask(&s, 1, LOST, 0, B, 1) == NVRM_VC_KEEP && s == NVRM_VC_DOWN);
    s = NVRM_VC_ON;
    ARM("an answer this header does not know counts as lost: kept, and the asking ends",
        ask(&s, 1, 7u, B, B, 1) == NVRM_VC_KEEP && s == NVRM_VC_DOWN);
    s = NVRM_VC_ON;
    ARM("an older NVRM that does not know the name: filled from the processor, and no more asking this boot",
        ask(&s, 0, NOT_RUN, 0, B, 1) == NVRM_VC_FILL && s == NVRM_VC_DOWN);
    s = NVRM_VC_ON;
    ARM("an older NVRM is not asked about its (untouched) result: unknown name wins over any result",
        ask(&s, 0, DONE, B, B, 1) == NVRM_VC_FILL && s == NVRM_VC_DOWN);

    /* Every answer, from every state: KEEP only for lost/unknown work of an NVRM that knows the name, ZERO only after
       zero probes of a finished whole clear, and a stood-down clear never comes back on. */
    int keepOk = 1, zeroOk = 1, stickyOk = 1, offOk = 1;
    const unsigned results[] = { DONE, NOT_RUN, LOST, 3u, 0xffffffffu };
    const unsigned long long clears[] = { 0, B - 1, B, B + 1 };
    for (int st0 = NVRM_VC_OFF; st0 <= NVRM_VC_DOWN; st0++)
        for (int known = 0; known <= 1; known++)
            for (unsigned r = 0; r < sizeof results / sizeof results[0]; r++)
                for (unsigned c = 0; c < sizeof clears / sizeof clears[0]; c++)
                    for (int z = 0; z <= 1; z++) {
                        s = st0;
                        const int what = ask(&s, known, results[r], clears[c], B, z);
                        if (what == NVRM_VC_KEEP && !(known && results[r] != DONE && results[r] != NOT_RUN)) keepOk = 0;
                        if (known && results[r] != DONE && results[r] != NOT_RUN && what != NVRM_VC_KEEP) keepOk = 0;
                        if (what == NVRM_VC_ZERO && !(known && results[r] == DONE && clears[c] >= B && z)) zeroOk = 0;
                        if (st0 == NVRM_VC_DOWN && s != NVRM_VC_DOWN) stickyOk = 0;
                        if (s == NVRM_VC_ON && st0 != NVRM_VC_ON) offOk = 0;
                        if (what == NVRM_VC_PROBE) zeroOk = 0;
                    }
    ARM("kept exactly when work may still be in the engine (lost or unknown, from an NVRM that knows the name)", keepOk);
    ARM("zero only for a whole clear the engine finished and the probes read as zero", zeroOk);
    ARM("a clear that stood down never comes back on within the boot", stickyOk);
    ARM("no answer turns the clear on (off by boot argument stays off)", offOk);

    printf("%d failed\n", fails);
    return fails != 0;
}
