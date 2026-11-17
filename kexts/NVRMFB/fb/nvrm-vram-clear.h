/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

/* nvrm-vram-clear.h - what the framebuffer does with NVRM's answer when it asks the GPU's copy engine to clear a new
   block of video memory (nvrm_vram_abi.h NVRM_VRAM_FN_CLEAR, 3.0.1).

   Why. A block that has to be zero (every block after the GPU's first standby, or after a block read as not zero
   although the RM clears) was filled from the processor through BAR1, at about 330 MB/s: 27 ms for the 9 MB of one
   captured frame of a screen share, with the display gate held on the thread that asked (nvrm-vram-pool.h). The copy
   engine clears it in a fraction of a millisecond and the processor only reads the probes back.

   The rules, for a block that has to be zero:
   - The engine is asked only while the clear is on: not with boot argument nvfbceclear=0 (NVRM_VC_OFF), and not
     after it stood down for this boot (NVRM_VC_DOWN).
   - An NVRM that does not know the name (an older one: the call goes up the providers and nobody answers) ends the
     asking for this boot, and the block is filled from the processor.
   - Nothing was sent (the GPU is not running, the engine's queue is paused, the block is not the RM's video memory):
     the block is as it was and is filled from the processor; the next block asks again.
   - Work was sent and its end was not seen, or the answer is one this header does not know: the engine may still write
     zeros into the block. It is kept until restart, neither handed out nor freed, and the asking ends for this boot:
     a block freed now could be someone else's memory when the zeros land.
   - The engine finished: the same places the RM's clearing is checked at are read (NVRM_VP_PROBES and the end). Zero
     there: the block is zero. Not zero: the engine did not do what it said, the block is filled from the processor
     and the asking ends for this boot.
   - The engine cleared less than the block (the RM's memory is shorter than the grant): filled from the processor.

   Pure: tools/test_vram_clear.c holds every answer against these rules. The answers' values are NVRM's own, so
   nvrm_vram_abi.h comes first. */
#pragma once

#ifndef NVRM_VRAM_CLEAR_DONE
#error "include nvrm_vram_abi.h before nvrm-vram-clear.h"
#endif

enum { NVRM_VC_OFF = 0, NVRM_VC_ON = 1, NVRM_VC_DOWN = 2 };   /* the clear for this boot: boot-arg off, on, stood down */
enum { NVRM_VC_ZERO = 0, NVRM_VC_FILL = 1, NVRM_VC_KEEP = 2, NVRM_VC_PROBE = 3 };   /* what to do with the block */

/* After the call. known: NVRM knew the name (anything but kIOReturnUnsupported). result, cleared: its answer
   (NVRM_VRAM_CLEAR_*, initialised to NOT_RUN before the call so that an NVRM that wrote nothing reads as nothing sent).
   Returns NVRM_VC_PROBE when the block is to be read at the probes (then nvrm_vc_probed), else what to do with it. */
static inline int nvrm_vc_answer(int *state, int known, unsigned result, unsigned long long cleared,
                                 unsigned long long bytes)
{
    if (!known) { *state = NVRM_VC_DOWN; return NVRM_VC_FILL; }
    if (result == NVRM_VRAM_CLEAR_NOT_RUN) return NVRM_VC_FILL;
    if (result != NVRM_VRAM_CLEAR_DONE) { *state = NVRM_VC_DOWN; return NVRM_VC_KEEP; }   /* LOST or unknown */
    if (cleared < bytes) return NVRM_VC_FILL;
    return NVRM_VC_PROBE;
}

/* After the probes of a block the engine says it cleared. */
static inline int nvrm_vc_probed(int *state, int readsZero)
{
    if (readsZero) return NVRM_VC_ZERO;
    *state = NVRM_VC_DOWN;
    return NVRM_VC_FILL;
}
