/* test_fc_refusal.c - which function-constant variant refusals the process remembers (plugin/nvmtl_fc_refusal.h).
   Build and run anywhere (no GPU, no Metal):  cc -std=c11 -Wall -Wextra -I plugin tools/test_fc_refusal.c -o /tmp/test_fc_refusal && /tmp/test_fc_refusal
   Each arm is a sentence about a failure the rule must remember or must leave to be translated again. */
#include <stdio.h>
#include "nvmtl_fc_refusal.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }

int main(void)
{
    arm("a refusal by the translator is remembered", nvmtl_fc_refusal_keep(1, 1, 1, 0));
    arm("any other failure (no translator, no AIR, the one-module route declined first) is not", !nvmtl_fc_refusal_keep(0, 1, 1, 0));
    arm("with NVMTL_FCCACHE_OFF nothing is remembered", !nvmtl_fc_refusal_keep(1, 0, 1, 0));
    arm("a request without a cache key is not remembered", !nvmtl_fc_refusal_keep(1, 1, 0, 0));
    arm("the last place below the limit is still taken", nvmtl_fc_refusal_keep(1, 1, 1, NVMTL_FC_REFUSALS_MAX - 1));
    arm("at the limit nothing more is remembered (translated again, as before)", !nvmtl_fc_refusal_keep(1, 1, 1, NVMTL_FC_REFUSALS_MAX));
    arm("a remembered refusal answers the request", nvmtl_fc_refusal_use(1, 1));
    arm("a request with nothing remembered is translated", !nvmtl_fc_refusal_use(1, 0));
    arm("with NVMTL_FCCACHE_OFF every request is translated, remembered or not", !nvmtl_fc_refusal_use(0, 1));
    printf(fails ? "FAILED %d\n" : "ok\n", fails);
    return fails != 0;
}
