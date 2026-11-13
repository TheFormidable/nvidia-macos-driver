#ifndef NVMTL_LOG_FAILURE_H
#define NVMTL_LOG_FAILURE_H
#include <string.h>
#include <ctype.h>

static inline int nvmtl_log_fault_word(const char *text, const char *word)
{
    const char *p = text;
    while ((p = strstr(p, word))) {
        if ((p == text || !isalnum((unsigned char)p[-1])) &&
            !isalnum((unsigned char)p[5])) return 1;
        p += 5;
    }
    return 0;
}

/* NVK's own messages reach the log as "nvk-reason: <E|W> <id>: <message>" (nvmtl_vkmsg_cb in nvmtl_vk.c) and follow a
 * rule of their own instead of the words below: NVK's warnings name what it tried and what it fell back to, so words
 * like "missing" or "unsupported" are common in them and would put that chatter into the system log of every Metal
 * process. A release install keeps NVK's errors and the two lines of a GPU channel rebuild, which are warnings:
 * "nvrm channel slot N rebuilt after ..." (the GPU faulted, NVK made a new channel, frames may be missing) and
 * "nvrm channel slot N not rebuilt: ..." (the application lost its device). Both are written by
 * nvk/nvk-channel-recover.patch. */
#define NVMTL_NVK_REASON "nvk-reason: "
static inline int nvmtl_log_nvk_channel(const char *m)
{
    static const char slot[] = "nvrm channel slot ";
    if (strncmp(m, slot, sizeof slot - 1)) return 0;
    m += sizeof slot - 1;
    if (!isdigit((unsigned char)*m)) return 0;
    while (isdigit((unsigned char)*m)) m++;
    return !strncmp(m, " rebuilt after ", 15) || !strncmp(m, " not rebuilt: ", 14);
}
static inline int nvmtl_log_nvk_reason(const char *text)
{
    const char *p = text + (sizeof NVMTL_NVK_REASON - 1);
    if (p[0] == 'E' && p[1] == ' ') return 1;
    if (p[0] != 'W' || p[1] != ' ') return 0;
    /* the message starts after the first ": " that follows the id; Mesa's ids are "file.c:line", without a blank */
    const char *m = strstr(p + 2, ": ");
    return m && nvmtl_log_nvk_channel(m + 2);
}

/* Keep device/feature failures in release syslog. In particular, Vulkan errors
 * such as "vkCreateDevice -> -8" do not contain the word FAIL. */
static inline int nvmtl_log_failure(const char *text)
{
    if (text && !strncmp(text, NVMTL_NVK_REASON, sizeof NVMTL_NVK_REASON - 1)) return nvmtl_log_nvk_reason(text);
    return text && (strstr(text, "FAIL") || strstr(text, "REFUS") ||
                    nvmtl_log_fault_word(text, "fault") ||
                    nvmtl_log_fault_word(text, "FAULT") || strstr(text, "-> -") ||
                    strstr(text, "missing") || strstr(text, "cannot") ||
                    strstr(text, "unsupported") || strstr(text, "refused") ||
                    /* a draw the plugin dropped leaves the window blank or garbled with nothing else in the log
                     * (GTX 16 Terminal text on 2.2.0 was framebuffer fetch draws dropped this way) */
                    strstr(text, "draw is skipped") || strstr(text, "draw skipped") ||
                    strstr(text, "draws are being skipped") ||
                    /* a process denied a device: for WindowServer that is the "Failed to create MetalDevice" abort,
                     * and /tmp/nvmtl.log is gone after the reboot that rescues the machine */
                    strstr(text, "no device"));
}
#endif
