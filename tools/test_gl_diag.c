/* test_gl_diag.c - the once-per-process lines of the OpenGL path (plugin/nvmtl_gl_diag.h).
   Build and run anywhere (no GPU, no Metal):  cc -std=c11 -Wall -Wextra -I plugin tools/test_gl_diag.c -o /tmp/test_gl_diag && /tmp/test_gl_diag
   Each arm is a sentence about what a field report must be able to read from the system log, or what the vblank wait
   must keep doing. */
#include <stdio.h>
#include <string.h>
#include "nvmtl_gl_diag.h"
#include "nvmtl_log_failure.h"

static int fails;
static void arm(const char *what, int ok) { printf("%s  %s\n", ok ? "ok  " : "FAIL", what); if (!ok) fails++; }

static const uint8_t k1581[NVMTL_GL_CAPS_BYTES] = { 0x40, 0x01, 0, 0, 0, 0, 0, 0, 0, 0, 0x80, 0x42, 0, 0, 0x80, 0x41, 0, 0, 0, 0 };

/* as the release plugin writes it: nvlog_notice puts "NullMoth: " in front */
static int notice_only(const char *t)
{
    char full[700]; snprintf(full, sizeof full, "NullMoth: %s", t);
    return !nvmtl_log_failure(t) && !nvmtl_log_failure(full);
}

/* the rule nvglvbl_synchronize had inline, word for word */
static int old_rule(uint32_t n, uint32_t max, uint64_t period, uint64_t t0, uint64_t t1)
{
    return n >= 2 && n <= max && period && t1 > t0 && period < t1 - t0;
}

int main(void)
{
    const char *progs[] = { "java", "WindowServer", "Minecraft Launcher", NULL };
    char t[NVMTL_GL_CAPS_OUTCOMES][400];
    int quiet = 1, found = 1, prog_named = 1;
    for (int p = 0; p < 4; p++) {
        for (int o = 0; o < NVMTL_GL_CAPS_OUTCOMES; o++) {
            nvmtl_gl_caps_text(t[o], sizeof t[o], (nvmtl_gl_caps_outcome)o, o == NVMTL_GL_CAPS_NO_SYMBOL ? NULL : k1581, 1, progs[p], "25G342");
            quiet &= notice_only(t[o]);
            found &= strstr(t[o], "GL provoking vertex: ") == t[o];
            prog_named &= strstr(t[o], progs[p] ? progs[p] : " in ? ") != NULL;
        }
    }
    arm("no provoking-vertex line names a word the failure rule forwards (so it is in the system log once)", quiet);
    arm("every provoking-vertex line starts with the text the log predicate looks for", found);
    arm("every provoking-vertex line names the process (hardened processes may differ), or ? without one", prog_named);

    int distinct = 1;
    for (int a = 0; a < NVMTL_GL_CAPS_OUTCOMES; a++)
        for (int b = a + 1; b < NVMTL_GL_CAPS_OUTCOMES; b++) distinct &= strcmp(nvmtl_gl_caps_what(a), nvmtl_gl_caps_what(b)) != 0;
    arm("the five outcomes read differently", distinct && NVMTL_GL_CAPS_OUTCOMES == 5);

    char line[400];
    nvmtl_gl_caps_text(line, sizeof line, NVMTL_GL_CAPS_DIFFERS, k1581, 0, "java", "25G342");
    arm("a table that differs is shown as its 20 bytes in hex, in order, with no blank at the end",
        strstr(line, "(NVIDIA profile 40 01 00 00 00 00 00 00 00 00 80 42 00 00 80 41 00 00 00 00)") != NULL);
    arm("the line names the macOS build the bytes belong to", strstr(line, " on macOS 25G342 ") != NULL);
    arm("a table that differs says the rewrite stays", strstr(line, "index rewrite") && strstr(line, "15.8.1"));
    nvmtl_gl_caps_text(line, sizeof line, NVMTL_GL_CAPS_NOT_WRITABLE, k1581, 2, "java", NULL);
    arm("a page that is not writable shows vm_protect's answer", strstr(line, ", kr 2)") != NULL);
    arm("an unknown build is shown as ?", strstr(line, " on macOS ? ") != NULL);
    nvmtl_gl_caps_text(line, sizeof line, NVMTL_GL_CAPS_PATCHED, k1581, 2, "java", "");
    arm("kr is shown only where vm_protect refused the page", strstr(line, "kr") == NULL);
    nvmtl_gl_caps_text(line, sizeof line, NVMTL_GL_CAPS_NO_SYMBOL, NULL, 0, "java", "25G342");
    arm("without a table there are no bytes to show", strstr(line, "(NVIDIA profile none)") != NULL);
    char small[16]; memset(small, 'x', sizeof small);
    nvmtl_gl_caps_text(small, sizeof small, NVMTL_GL_CAPS_DIFFERS, k1581, 0, "java", "25G342");
    arm("a short buffer is cut and still ends", memchr(small, 0, sizeof small) != NULL);

    /* the vblank branch: the same rule as before, over a grid that crosses every edge */
    const uint64_t times[] = { 0, 1, 99, 100, 101, 10000, 16666, 16667, 16668, UINT64_MAX - 1, UINT64_MAX };
    const uint64_t periods[] = { 0, 1, 100, 9999, 10000, 10001, 16667, UINT64_MAX };
    int same = 1, corrected = 0;
    for (uint32_t n = 0; n <= 6; n++)
        for (unsigned p = 0; p < sizeof periods / sizeof *periods; p++)
            for (unsigned a = 0; a < sizeof times / sizeof *times; a++)
                for (unsigned b = 0; b < sizeof times / sizeof *times; b++) {
                    const int was = old_rule(n, 4, periods[p], times[a], times[b]);
                    const nvglvbl_decision d = nvglvbl_decide(n, 4, periods[p], times[a], times[b]);
                    same &= was == (d == NVGLVBL_CORRECTED);
                    corrected += was;
                }
    arm("the wait takes the corrected branch exactly where the old inline rule did", same && corrected > 0);
    arm("fewer than two entries, or more than asked, leaves AppKit's wait",
        nvglvbl_decide(1, 4, 10000, 0, 16667) == NVGLVBL_FEW_ENTRIES && nvglvbl_decide(5, 4, 10000, 0, 16667) == NVGLVBL_FEW_ENTRIES);
    arm("a display without a refresh rate leaves AppKit's wait", nvglvbl_decide(4, 4, 0, 0, 16667) == NVGLVBL_NO_PERIOD);
    arm("a 100 Hz display under CoreAnimation's 1/60 s spacing is corrected", nvglvbl_decide(4, 4, 10000, 0, 16667) == NVGLVBL_CORRECTED);
    arm("spacing equal to the period, or going back, leaves AppKit's wait",
        nvglvbl_decide(4, 4, 16667, 0, 16667) == NVGLVBL_NOT_LONGER && nvglvbl_decide(4, 4, 10000, 16667, 0) == NVGLVBL_NOT_LONGER);

    int vquiet = 1, vfound = 1, vdistinct = 1;
    for (int d = 0; d < NVGLVBL_DECISIONS; d++) {
        nvglvbl_text(line, sizeof line, (nvglvbl_decision)d, 4, 10000, 16667, "java");
        vquiet &= notice_only(line);
        vfound &= strstr(line, "GL vblank wait: ") == line;
        for (int e = d + 1; e < NVGLVBL_DECISIONS; e++) vdistinct &= strcmp(nvglvbl_what(d), nvglvbl_what(e)) != 0;
    }
    arm("no vblank line names a word the failure rule forwards", vquiet);
    arm("every vblank line starts with the text the log predicate looks for", vfound);
    arm("the four branches read differently", vdistinct && NVGLVBL_DECISIONS == 4);
    nvglvbl_text(line, sizeof line, NVGLVBL_CORRECTED, 4, 10000, 16667, "java");
    arm("the corrected line names the process and both periods",
        !strcmp(line, "GL vblank wait: first wait in java waits by the display's refresh period (entries 4, display period "
                      "10000 us, CoreAnimation spacing 16667 us)"));
    nvglvbl_text(line, sizeof line, NVGLVBL_FEW_ENTRIES, 0, 0, 0, NULL);
    arm("a fallback without process name or periods still reads", strstr(line, " in ? left to AppKit") && strstr(line, "entries 0"));

    printf("%s\n", fails ? "FAIL" : "PASS");
    return fails != 0;
}
