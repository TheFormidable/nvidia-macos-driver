/* test_surf_private.c - why a single-plane surface took a private image, the once-per-reason note, and when a bind may
   pass this process's own copy-out of a private image (plugin/nvmtl_surf_private.h).
   Build and run anywhere (no GPU, no Metal):  cc -std=c11 -Wall -Wextra -I plugin tools/test_surf_private.c -o /tmp/test_surf_private && /tmp/test_surf_private
   Each arm is a sentence about a decision the rule must make or refuse. */
#include <stdio.h>
#include <string.h>
#include "nvmtl_surf_private.h"
#include "nvmtl_log_failure.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }

int main(void)
{
    /* a 1920x1080 BGRA8 window surface with a 7680-byte row */
    const uint32_t W = 1920, H = 1080; const size_t ROW = 7680, SH = 1080;
    arm("sharing off is named before anything about the surface",
        nvmtl_spv_reason(0, W, H, 4, 1, 1, NVMTL_SPV_STAGE_IMPORT, 2) == NVMTL_SPV_SHARE_OFF);
    arm("a texture wider than the surface's row is the size reason",
        nvmtl_spv_reason(1, W + 1, H, 4, ROW, SH, NVMTL_SPV_STAGE_NONE, 0) == NVMTL_SPV_LARGER);
    arm("a texture taller than the surface is the size reason", nvmtl_spv_reason(1, W, H + 1, 4, ROW, SH, NVMTL_SPV_STAGE_NONE, 0) == NVMTL_SPV_LARGER);
    arm("bpp 0 counts as 4 bytes, as the image creation does",
        nvmtl_spv_reason(1, W, H, 0, ROW - 4, SH, NVMTL_SPV_STAGE_NONE, 0) == NVMTL_SPV_LARGER
        && nvmtl_spv_reason(1, W, H, 0, ROW, SH, NVMTL_SPV_STAGE_IMPORT, 2) == NVMTL_SPV_IMPORT);
    arm("a row that is not a multiple of 32 bytes is named", nvmtl_spv_reason(1, W, H, 4, ROW + 16, SH, NVMTL_SPV_STAGE_NONE, 0) == NVMTL_SPV_ROW32);
    arm("the size check comes before the row check, as in the image creation",
        nvmtl_spv_reason(1, W, H, 4, 7004, SH, NVMTL_SPV_STAGE_NONE, 0) == NVMTL_SPV_LARGER);
    arm("an import without a descriptor is named as such", nvmtl_spv_reason(1, W, H, 4, ROW, SH, NVMTL_SPV_STAGE_IMPORT, 1) == NVMTL_SPV_NO_FD);
    arm("an import NVK refused is named as such", nvmtl_spv_reason(1, W, H, 4, ROW, SH, NVMTL_SPV_STAGE_IMPORT, 2) == NVMTL_SPV_IMPORT);
    arm("a refused buffer over the import is named as such", nvmtl_spv_reason(1, W, H, 4, ROW, SH, NVMTL_SPV_STAGE_IMPORT, 3) == NVMTL_SPV_BUFFER);
    arm("an import that said nothing still counts as the import", nvmtl_spv_reason(1, W, H, 4, ROW, SH, NVMTL_SPV_STAGE_IMPORT, 0) == NVMTL_SPV_IMPORT);
    arm("a refused linear image is named", nvmtl_spv_reason(1, W, H, 4, ROW, SH, NVMTL_SPV_STAGE_ALIAS, 0) == NVMTL_SPV_ALIAS);
    arm("a stale import answer does not hide a later stage", nvmtl_spv_reason(1, W, H, 4, ROW, SH, NVMTL_SPV_STAGE_ALIAS, 1) == NVMTL_SPV_ALIAS);
    arm("a missing storage view is named", nvmtl_spv_reason(1, W, H, 4, ROW, SH, NVMTL_SPV_STAGE_NO_STORAGE, 0) == NVMTL_SPV_NO_STORAGE);
    arm("a linear import that is not a colour attachment is named", nvmtl_spv_reason(1, W, H, 4, ROW, SH, NVMTL_SPV_STAGE_NOT_RT, 0) == NVMTL_SPV_NOT_RT);
    arm("a missing tiled image is named", nvmtl_spv_reason(1, W, H, 4, ROW, SH, NVMTL_SPV_STAGE_NO_TILED, 0) == NVMTL_SPV_NO_TILED);

    int texts = 1;
    for (int r = 0; r < NVMTL_SPV_COUNT; r++) {
        const char *t = nvmtl_spv_text(r);
        if (!t || !*t || !strcmp(t, "unknown")) texts = 0;
        for (int q = 0; q < r; q++) if (!strcmp(t, nvmtl_spv_text(q))) texts = 0;
    }
    arm("every reason has a text of its own", texts);
    arm("an out-of-range reason reads unknown", !strcmp(nvmtl_spv_text(-1), "unknown") && !strcmp(nvmtl_spv_text(NVMTL_SPV_COUNT), "unknown"));
    arm("the tag the log search uses is fixed", !strcmp(NVMTL_SPV_TAG, "private-surface:"));
    int kept = 1; size_t longest = 0;
    for (int r = 0; r < NVMTL_SPV_COUNT; r++) {
        char line[1024];
        const int n = snprintf(line, sizeof line, NVMTL_SPV_FORMAT, 4294967295u, 16384u, 16384u, (size_t)16384, (size_t)16384,
                               (size_t)65536, 1000156000u, nvmtl_spv_text(r));
        if (n < 0 || !nvmtl_log_failure(line) || strncmp(line, NVMTL_SPV_TAG, strlen(NVMTL_SPV_TAG))) kept = 0;
        if (n > 0 && (size_t)n > longest) longest = (size_t)n;
    }
    arm("a release plugin keeps every reason's line in the system log (nvmtl_log_failure)", kept);
    arm("the longest line fits nvlog_every's 640 characters, so the reason is never cut", longest < 640);

    uint32_t seen = 0;
    arm("a reason is said the first time", nvmtl_spv_first(&seen, NVMTL_SPV_IMPORT));
    arm("the same reason is not said again (never once per frame)", !nvmtl_spv_first(&seen, NVMTL_SPV_IMPORT) && !nvmtl_spv_first(&seen, NVMTL_SPV_IMPORT));
    arm("another reason is still said once", nvmtl_spv_first(&seen, NVMTL_SPV_MARK_REFUSED) && !nvmtl_spv_first(&seen, NVMTL_SPV_MARK_REFUSED));
    arm("every reason fits in the one word", NVMTL_SPV_COUNT <= 32);
    arm("an out-of-range reason is never said and sets nothing",
        !nvmtl_spv_first(&seen, NVMTL_SPV_COUNT) && !nvmtl_spv_first(&seen, -1) && !nvmtl_spv_first(NULL, 0));

    arm("a private, synced image of an application's own surface may be passed", nvmtl_spv_out_passable(1, 0, 0, 0, 1));
    arm("NVMTL_SURFIN_SKIP_OWN_OUT=0 never lets a bind pass", !nvmtl_spv_out_passable(0, 0, 0, 0, 1));
    arm("WindowServer's copy-outs are never passed", !nvmtl_spv_out_passable(1, 1, 0, 0, 1));
    arm("a surface in the family's VRAM (a refused mark copied out) is never passed", !nvmtl_spv_out_passable(1, 0, 1, 0, 1));
    arm("a surface another process draws is never passed (the bind waits for that process)", !nvmtl_spv_out_passable(1, 0, 0, 1, 1));
    arm("an image that was not in step before the copy-out is never passed", !nvmtl_spv_out_passable(1, 0, 0, 0, 0));

    arm("a bind during a passable copy-out returns when nothing moved", nvmtl_spv_bind_skips(1, 0, 7, 7));
    arm("no copy-out in flight: the bind goes through nvmtlSurfaceIn", !nvmtl_spv_bind_skips(0, 0, 7, 7));
    arm("a lost zero fill makes the bind wait and decide again", !nvmtl_spv_bind_skips(1, 1, 7, 7));
    arm("another writer's VRAM publication since the copy-out began makes the bind wait", !nvmtl_spv_bind_skips(1, 0, 8, 7));

    printf(fails ? "FAILED %d\n" : "ok\n", fails);
    return fails != 0;
}
