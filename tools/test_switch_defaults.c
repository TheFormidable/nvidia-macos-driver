/* test_switch_defaults.c - the 3.0.1 defaults of NVMTL_ASYNC_TEX_UPLOAD and NVMTL_PRETRANSLATE
   (plugin/nvmtl_switch_defaults.h): on by default where the rules say, and each one's way back.
   Build and run anywhere:  cc -O2 -Wall -I plugin tools/test_switch_defaults.c -o /tmp/test_switch_defaults && /tmp/test_switch_defaults */
#include <stdio.h>
#include "nvmtl_switch_defaults.h"

static int fails;
#define ARM(name, cond) do { int ok_ = (cond); printf("%s %s\n", ok_ ? "ok  " : "FAIL", name); if (!ok_) fails++; } while (0)

int main(void)
{
    ARM("asynchronous region uploads are on by default in an app",
        nvmtl_async_tex_upload_default(NULL, "java") && nvmtl_async_tex_upload_default(NULL, "Safari"));
    ARM("WindowServer keeps the synchronous upload by default", !nvmtl_async_tex_upload_default(NULL, "WindowServer"));
    ARM("a process with no name counts as an app", nvmtl_async_tex_upload_default(NULL, NULL));
    ARM("NVMTL_ASYNC_TEX_UPLOAD=0 is the way back, in every process",
        !nvmtl_async_tex_upload_default("0", "java") && !nvmtl_async_tex_upload_default("0", "WindowServer"));
    ARM("=1 still turns it on anywhere, WindowServer included (as before)",
        nvmtl_async_tex_upload_default("1", "WindowServer") && nvmtl_async_tex_upload_default("1", "java"));
    ARM("any other value, the empty one too, turns it off (as 3.0 read it)",
        !nvmtl_async_tex_upload_default("", "java") && !nvmtl_async_tex_upload_default("yes", "java"));
    ARM("3.0 read only the first character: \"10\" and \"1 \" turn it on",
        nvmtl_async_tex_upload_default("10", "java") && nvmtl_async_tex_upload_default("1 ", "java")
        && nvmtl_async_tex_upload_default("10", "WindowServer"));
    ARM("and \"01\" turns it off", !nvmtl_async_tex_upload_default("01", "java"));
    ARM("only the exact name WindowServer is left out",
        nvmtl_async_tex_upload_default(NULL, "WindowServer2") && nvmtl_async_tex_upload_default(NULL, "windowserver"));

    ARM("pre-translation is on by default in a game", nvmtl_pretranslate_default(NULL, 1));
    ARM("and off by default anywhere else", !nvmtl_pretranslate_default(NULL, 0));
    ARM("NVMTL_PRETRANSLATE=0 is the way back in a game", !nvmtl_pretranslate_default("0", 1));
    ARM("=1 still turns it on in any process (as before)", nvmtl_pretranslate_default("1", 0) && nvmtl_pretranslate_default("1", 1));
    ARM("any other value turns it off, as 3.0's strcmp(\"1\") did",
        !nvmtl_pretranslate_default("", 1) && !nvmtl_pretranslate_default("10", 1) && !nvmtl_pretranslate_default("on", 1));

    printf("%d failed\n", fails);
    return fails != 0;
}
