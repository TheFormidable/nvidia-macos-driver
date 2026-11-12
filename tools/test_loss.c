/* test_loss.c - the device-loss cause line and the answer for work a channel rebuild dropped, proven on the host.
   Build and run anywhere:  cc -std=c11 -Wall -Wextra -Werror -I plugin tools/test_loss.c -o /tmp/test_loss && /tmp/test_loss */
#include "nvmtl_loss.h"
#include "nvmtl_log_failure.h"
static int fails;
#define ARM(name, cond) do { const int ok = !!(cond); printf("%s  %s\n", ok ? "ok  " : "FAIL", name); if (!ok) fails++; } while (0)

/* The rules for a command buffer whose fence signalled, given NVK's answer to "was it dropped" and the opt-out. Returns
   how many rules the three functions break, so the controls below can show each rule catches a wrong version. */
static int drop_rules(int (*seen)(int), int (*parse)(const char *), int (*result)(int, int))
{
    int bad = 0;
    bad += seen(1) != 1;                          /* NVK says dropped */
    bad += seen(0) != 0;                          /* it ran */
    bad += seen(-1) != 0;                         /* not NVK's fence, or a fence nothing was dropped from: as before */
    bad += parse(NULL) != 1;                      /* unset: reported */
    bad += parse("") != 1;
    bad += parse("1") != 1;
    bad += parse("yes") != 1;
    bad += parse("0") != 0;                       /* the opt-out */
    bad += parse("00") != 0;
    bad += result(0, 1) != 0 || result(0, 0) != 0;   /* work that ran completes, whatever the switch */
    bad += result(1, 1) != -1;                    /* dropped: retired with an error, never -2 (device lost) */
    bad += result(1, 0) != 0;                     /* opt-out: completed */
    return bad;
}
static int bad_seen(int a) { return a != 0; }
static int bad_parse(const char *e) { return e && e[0] == '1'; }
static int bad_result_lost(int d, int r) { return d && r ? -2 : 0; }
static int bad_result_ok(int d, int r) { (void)d; (void)r; return 0; }
int main(void)
{
    char s[600];
    nvmtl_loss_cause(s, sizeof s, "fence", -4, 1200, "present", 90, "WindowServer");
    ARM("a lost fence names the process, the result, the wait, the caller and the uptime",
        strstr(s, "WindowServer") && strstr(s, "VK_ERROR_DEVICE_LOST") && strstr(s, "1200 ms") && strstr(s, "present") && strstr(s, "90 s"));
    ARM("a fence loss says the GPU faulted while the work ran", strstr(s, "faulted or was reset while this work ran"));
    nvmtl_loss_cause(s, sizeof s, "submit", -4, 0, "composite", 5, "WindowServer");
    ARM("a submit refused as lost says the channel was already dead", strstr(s, "already dead"));
    nvmtl_loss_cause(s, sizeof s, "fence", 2, 50000, "app", 700, "Game");
    ARM("a fence that never answers is called a stalled channel", strstr(s, "stalled channel") && strstr(s, "VK_TIMEOUT"));
    nvmtl_loss_cause(s, sizeof s, "submit", -2, 0, NULL, 1, NULL);
    ARM("running out of device memory is not blamed on a GPU fault", strstr(s, "ran out of memory") && !strstr(s, "faulted"));
    nvmtl_loss_cause(s, sizeof s, "submit", -99, 0, "x", 1, "p");
    ARM("an unknown result is still named", strstr(s, "an unknown VkResult") && strstr(s, "(-99)"));
    char tiny[24]; nvmtl_loss_cause(tiny, sizeof tiny, "fence", -4, 1, "x", 1, "p");
    ARM("a small buffer is cut, not overrun", strlen(tiny) < sizeof tiny);

    /* work a channel rebuild dropped */
    ARM("the drop rules hold", drop_rules(nvmtl_drop_seen, nvmtl_drop_report_parse, nvmtl_drop_result) == 0);
    ARM("control: a plugin that took \"not NVK's fence\" (-1) for dropped is caught", drop_rules(bad_seen, nvmtl_drop_report_parse, nvmtl_drop_result) > 0);
    ARM("control: an opt-out read as an opt-in (only \"1\" reports) is caught", drop_rules(nvmtl_drop_seen, bad_parse, nvmtl_drop_result) > 0);
    ARM("control: a drop answered as a lost device (-2) is caught", drop_rules(nvmtl_drop_seen, nvmtl_drop_report_parse, bad_result_lost) > 0);
    ARM("control: a drop answered as success is caught", drop_rules(nvmtl_drop_seen, nvmtl_drop_report_parse, bad_result_ok) > 0);
    ARM("the first dropped command buffer and every 256th are logged, the others not",
        nvmtl_drop_log_due(1) && !nvmtl_drop_log_due(2) && !nvmtl_drop_log_due(255) && nvmtl_drop_log_due(256) && nvmtl_drop_log_due(512) && !nvmtl_drop_log_due(0));

    uint64_t st[NVMTL_REC_N] = { 0 };
    st[NVMTL_REC_VERSION] = 1; st[NVMTL_REC_ENABLED] = 1; st[NVMTL_REC_KIND] = 4; st[NVMTL_REC_XID] = 119; st[NVMTL_REC_WHY] = 2;
    st[NVMTL_REC_REPLAYS] = 2; st[NVMTL_REC_DROP_ALLS] = 1; st[NVMTL_REC_DROPPED] = 5; st[NVMTL_REC_REPLAYED] = 9; st[NVMTL_REC_GAVE_UP] = 1;
    ARM("the loss line names a firmware loss, its Xid and the reason it was not rebuilt",
        nvmtl_loss_recovery_line(s, sizeof s, st, NVMTL_REC_N) == 1 && strstr(s, "GSP") && strstr(s, "Xid 119") && strstr(s, "not rebuilt")
        && strstr(s, "3 rebuilt (2 replayed, 1 dropped all)") && strstr(s, "5 flushes dropped") && strstr(s, "1 given up"));
    st[NVMTL_REC_KIND] = 5; nvmtl_loss_recovery_line(s, sizeof s, st, NVMTL_REC_N);
    ARM("a bus loss is named", strstr(s, "BUS") != NULL);
    st[NVMTL_REC_KIND] = 6; nvmtl_loss_recovery_line(s, sizeof s, st, NVMTL_REC_N);
    ARM("a failed wake is named", strstr(s, "RESUME") != NULL);
    st[NVMTL_REC_KIND] = 3; nvmtl_loss_recovery_line(s, sizeof s, st, NVMTL_REC_N);
    ARM("an ECC loss is named", strstr(s, "MEMORY") != NULL);
    st[NVMTL_REC_KIND] = 99; st[NVMTL_REC_WHY] = 99; nvmtl_loss_recovery_line(s, sizeof s, st, NVMTL_REC_N);
    ARM("a kind or reason from a newer NVK is printed as unknown, not read past the table", strstr(s, "last loss ?") && strstr(s, "because: ?"));
    ARM("every kind 0..7 and reason 0..13 has its own name",
        strcmp(nvmtl_rec_kind_name(7), "?") && strcmp(nvmtl_rec_why_name(13), "?") && !strcmp(nvmtl_rec_kind_name(8), "?") && !strcmp(nvmtl_rec_why_name(14), "?"));
    st[NVMTL_REC_ENABLED] = 0;
    ARM("recovery switched off is said so", nvmtl_loss_recovery_line(s, sizeof s, st, NVMTL_REC_N) == 1 && strstr(s, "off"));
    strcpy(s, "x"); st[NVMTL_REC_VERSION] = 2;
    ARM("an unknown statistics version writes nothing", nvmtl_loss_recovery_line(s, sizeof s, st, NVMTL_REC_N) == 0 && s[0] == 0);
    strcpy(s, "x"); st[NVMTL_REC_VERSION] = 1;
    ARM("too few statistics write nothing", nvmtl_loss_recovery_line(s, sizeof s, st, NVMTL_REC_WHY) == 0 && s[0] == 0);
    ARM("no statistics (an older NVK) write nothing", nvmtl_loss_recovery_line(s, sizeof s, NULL, 0) == 0 && s[0] == 0);
    st[NVMTL_REC_ENABLED] = 1; st[NVMTL_REC_XID] = 13;
    nvmtl_drop_line(s, sizeof s, 1, 40, "Game", 1, st, NVMTL_REC_N);
    ARM("the drop line says it did not run, that it is reported as failed and that the device stays up, with NVK's counters",
        strstr(s, "dropped this command buffer") && strstr(s, "did not run") && strstr(s, "as failed") && strstr(s, "stays up")
        && strstr(s, "Xid 13") && strstr(s, "Game") && strstr(s, "40 ms"));
    ARM("a release build keeps the drop line in the system log", nvmtl_log_failure(s));
    nvmtl_drop_line(s, sizeof s, 2, 40, "Game", 0, NULL, 0);
    ARM("the drop line under the opt-out says completed and names the switch, without counters from an older NVK",
        strstr(s, "as completed (NVMTL_DROP_REPORT=0)") && !strstr(s, "NVK:"));
    char small[32]; nvmtl_drop_line(small, sizeof small, 1, 1, "p", 1, st, NVMTL_REC_N); nvmtl_loss_recovery_line(tiny, sizeof tiny, st, NVMTL_REC_N);
    ARM("the drop and recovery lines are cut, not overrun", strlen(small) < sizeof small && strlen(tiny) < sizeof tiny);
    return fails ? 1 : 0;
}
