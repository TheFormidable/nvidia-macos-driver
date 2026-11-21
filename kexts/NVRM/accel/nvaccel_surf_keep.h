/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

// An import that is kept for longer than one frame has to know that the surface still has the block it imported. The
// two rules for that, as plain functions a host test can call (tools/test_surf_keep.py). The escape is in
// nvrm-accel.cpp, the interface in nvrm_surfshare_abi.h (`pad`).
#pragma once

// What a VRAM grant puts in `pad`: the name of the block that was shared, the low 32 bits of its allocation cookie, or
// 0 for a grant that was refused. A caller that reads 0 was told nothing and imports again for every frame, as every
// caller did before 3.0.7. A cookie is 64 bits, counts up from 1 for every allocation since boot and is never given
// twice. Its low half is 0 for one allocation in 2^32, whose grant then names nothing (that block is imported for
// every frame), and it repeats only after 2^32 allocations. A caller takes the name once, when it imports, and sends
// it with every mark for as long as it keeps the import, which can be a context's life: a mark for a block that is
// gone is taken for the surface's new one only if 2^32 allocations, to the count, lie between the two.
static inline unsigned nvaccel_keep_cookie(unsigned status, unsigned long long cookie)
{
    return status == 0 ? (unsigned)cookie : 0u;
}

// Whether a dirty mark has to be refused with NVRM_SS_MOVED. pad: what the caller sent (0: it does not keep imports,
// nothing is compared). haveVram: the surface has VRAM wired now, after a parked block was put back. cookie: that
// block's allocation cookie. A parked block comes back with the cookie it had, so "parked and wired again" is the same
// block; a block the family released and allocated again has a new one.
static inline int nvaccel_keep_moved(unsigned pad, int haveVram, unsigned long long cookie)
{
    return pad != 0 && (!haveVram || (unsigned)cookie != pad);
}
