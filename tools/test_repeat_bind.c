/* test_repeat_bind.c - the render encoder's repeat-bind savings and their off switches.
   Build and run anywhere (no GPU, no Metal):  cc -I plugin tools/test_repeat_bind.c -o /tmp/test_repeat_bind && /tmp/test_repeat_bind
   Each arm is a sentence about a failure the rule must produce or refuse. */
#include <stdio.h>
#include "nvmtl_repeat_bind.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }

int main(void)
{
    int a = 0, b = 0, k1 = 0, k2 = 0;
    arm("an unset switch leaves the saving on", !nvmtl_switch_is_off(NULL));
    arm("an empty switch leaves the saving on", !nvmtl_switch_is_off(""));
    arm("a switch set to 0 leaves the saving on", !nvmtl_switch_is_off("0"));
    arm("a switch set to 1 turns the saving off", nvmtl_switch_is_off("1"));
    arm("a switch set to 00 or text turns it off (only the exact 0 means on)", nvmtl_switch_is_off("00") && nvmtl_switch_is_off("yes"));

    arm("the same pipeline inside a begun pass is skipped", nvmtl_pso_skip(1, &a, &a, 1, 0));
    arm("NVMTL_NO_PSO_SKIP binds it again", !nvmtl_pso_skip(0, &a, &a, 1, 0));
    arm("a different pipeline is always bound", !nvmtl_pso_skip(1, &b, &a, 1, 0));
    arm("before the pass has begun nothing is skipped", !nvmtl_pso_skip(1, &a, &a, 0, 0));
    arm("inside an open pass-time pool nothing is skipped", !nvmtl_pso_skip(1, &a, &a, 1, 1));
    arm("a nil pipeline is never treated as a repeat", !nvmtl_pso_skip(1, NULL, NULL, 1, 0));

    arm("the same pipeline and key reuse the last variant", nvmtl_sv_memo_hit(1, &a, &a, &k1, &k1, 1));
    arm("NVMTL_NO_SV_MEMO looks the variant up again", !nvmtl_sv_memo_hit(0, &a, &a, &k1, &k1, 1));
    arm("a changed sampler value snapshot misses", !nvmtl_sv_memo_hit(1, &a, &a, &k1, &k2, 1));
    arm("a different pipeline misses", !nvmtl_sv_memo_hit(1, &a, &b, &k1, &k1, 1));
    arm("with no choice recorded yet it misses", !nvmtl_sv_memo_hit(1, NULL, &a, NULL, &k1, 0));
    arm("a missing key never hits", !nvmtl_sv_memo_hit(1, &a, &a, NULL, NULL, 1));

    /* setDepthStencilState: s is the state, d another; the record says s was applied in epoch 5 of command buffer 9,
       with a pipeline that had stencil */
    int s = 0, d = 0;
    arm("the state this encoder last applied, again in the same pass, is skipped",
        nvmtl_ds_skip(1, 1, &s, &s, &s, 1, 1, 1, 5, 5, 9, 9));
    arm("NVMTL_DS_SKIP=0 applies it again", !nvmtl_ds_skip(0, 1, &s, &s, &s, 1, 1, 1, 5, 5, 9, 9));
    arm("another state is applied", !nvmtl_ds_skip(1, 1, &d, &s, &s, 1, 1, 1, 5, 5, 9, 9));
    arm("a state set while it was not applied (no record of it) is applied", !nvmtl_ds_skip(1, 1, &s, &s, NULL, 1, 1, 1, 5, 5, 9, 9));
    arm("a record of another state does not cover this one", !nvmtl_ds_skip(1, 1, &s, &s, &d, 1, 1, 1, 5, 5, 9, 9));
    arm("a record of this state does not cover it once the encoder holds another one",
        !nvmtl_ds_skip(1, 1, &s, &d, &s, 1, 1, 1, 5, 5, 9, 9));
    arm("a nil state is always applied (it turns the depth test off)", !nvmtl_ds_skip(1, 1, NULL, NULL, NULL, 1, 1, 1, 5, 5, 9, 9));
    arm("after a new render pass began it is applied again", !nvmtl_ds_skip(1, 1, &s, &s, &s, 1, 1, 1, 5, 6, 9, 9));
    arm("in another VkCommandBuffer (a restart between encoders, epochs start over) it is applied again",
        !nvmtl_ds_skip(1, 1, &s, &s, &s, 1, 1, 1, 0, 0, 9, 10));
    arm("outside a begun pass it is applied (and the apply itself does nothing then)", !nvmtl_ds_skip(1, 0, &s, &s, &s, 1, 1, 1, 5, 5, 9, 9));
    arm("a pipeline without depth is never skipped", !nvmtl_ds_skip(1, 1, &s, &s, &s, 0, 0, 0, 5, 5, 9, 9));
    arm("a pipeline with stencil after a record made without it applies the stencil state",
        !nvmtl_ds_skip(1, 1, &s, &s, &s, 1, 1, 0, 5, 5, 9, 9));
    arm("a depth-only pipeline after a depth-only record is skipped", nvmtl_ds_skip(1, 1, &s, &s, &s, 1, 0, 0, 5, 5, 9, 9));

    printf(fails ? "FAILED %d\n" : "ok\n", fails);
    return fails != 0;
}
