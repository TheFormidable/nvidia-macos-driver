/* nvmtl_schedule_policy.h - how much of a command buffer's surface writes are published before it reports "scheduled".
   Modes: 3 two-plane surfaces written through private images, 2 every private image, 1 every surface write, 0 none.
   Pure, so tools/test_schedule_policy.c proves it on the host. */
#ifndef NVMTL_SCHEDULE_POLICY_H
#define NVMTL_SCHEDULE_POLICY_H
#include <string.h>

/* The Chromium family hands layers to another process from its scheduled callbacks, and that process has no queue
   dependency on ours, so these processes publish every surface write first. */
static inline int nvmtl_schedule_is_chromium_family(const char *process)
{
    if (!process) return 0;
    return strstr(process, "Google Chrome") || strstr(process, "Chromium") || strstr(process, "Microsoft Edge");
}
/* `setting` is NVMTL_SCHEDULE_AFTER_SURFACE_WRITES (may be NULL). Only a single digit 0..3 is an override; anything else
   is ignored rather than turned into a mode number that no code path knows. */
static inline int nvmtl_schedule_after_mode(const char *setting, const char *process)
{
    if (setting && setting[0] >= '0' && setting[0] <= '3' && !setting[1]) return setting[0] - '0';
    return nvmtl_schedule_is_chromium_family(process) ? 1 : 3;
}
/* Whether one surface a command buffer writes makes it report "scheduled" only after its writes are published, under
   `mode` (above). A surface written through a private image is published by its copy-out, which a reader's CPU lock does
   not wait for. A surface whose VRAM is shared is published when the kernel's page-off for a CPU lock waits for the
   pending write: that is the case for a single-plane surface as before, and for a two-plane surface in its planes' own
   VRAM only where `kernel_waits` says WindowServer's marks are taken and its page-off waits for them; where it does not,
   such a surface is held back like a private one (mode 3 exists for exactly these, the NV12 capture surfaces). */
static inline int nvmtl_schedule_after_surface(int mode, int has_surface, int vram_on, unsigned long planes, int kernel_waits)
{
    if (mode == 0) return 0;
    if (mode == 1) return 1;
    if (!has_surface) return 0;
    if (!vram_on) return mode != 3 || planes > 1;
    return planes > 1 && !kernel_waits;
}
#endif
