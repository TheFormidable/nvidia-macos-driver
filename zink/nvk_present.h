/* SPDX-License-Identifier: MIT */
#ifndef NVKZINK_PRESENT_H
#define NVKZINK_PRESENT_H
#include <IOSurface/IOSurface.h>
struct pipe_screen;
struct pipe_resource;
/* Synchronous copy on the Zink device. 1 = published to the surface, 0 = safe
 * failure, -1 = uncertain completion (caller must quarantine its screen). */
int nvk_zink_copy_vram(struct pipe_screen *, struct pipe_resource *, IOSurfaceRef);
#endif
