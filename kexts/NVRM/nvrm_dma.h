/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

// nvrm_dma.h: bus addresses for the GPU when a DMA mapper (VT-d / AppleVTD) translates it. What the rest of NVRM.kext
// calls in the DMA layer (nvrm_dma.cpp), and the two helpers the DMA layer needs from the allocator (os-xnu2.cpp).
//
// Without a mapper (mode NVRM_DMA_NONE) a bus address is the physical address and nothing here changes what the driver
// does. With one, the RM keeps physical addresses for the CPU side and asks the OS layer for the bus address of each
// allocation (nv_requires_dma_remap, nv_dma_map_alloc, nv_dma_unmap_alloc: the RM's own interface in nv.h, defined in
// nvrm_dma.cpp). A third state covers what cannot be made safe: a mapper is there and the driver may not or cannot
// use it. Then the GPU is not started, because starting it would put untranslated addresses on a translated bus.
//
// Locks: the DMA layer has one IOLock of its own (the device table and the list of live mappings). It is a leaf: held
// for list walks and pointer updates only, never across an IOKit call, an allocation, a log line or a call into the
// RM. Callers may hold the RM's API and GPU locks. The layer never calls into the RM and touches no hardware.
// Everything that maps or unmaps can sleep and so needs thread context; the cheap readers below say so where they
// do not.

#pragma once
#include "nv-xnu.h"
#ifndef NVRM_SS_KERNEL_ABI
#define NVRM_SS_KERNEL_ABI 1       // enum nvrm_dma_mode and struct NVRMSurfDma are in the kernel half of this header
#endif
#include "nvrm_surfshare_abi.h"

class IOMemoryDescriptor;

extern "C" {

// ---- nvrm_dma.cpp, called from the kext's start and stop paths ----

// Decide, once per GPU, how its addresses reach the bus. Thread context; it allocates, asks IOKit for the mapper and,
// when a mapper will be used, maps a two-page buffer through it and reads both pages back (a failed read-back turns
// the mode into NVRM_DMA_REFUSE). The first call also reads the boot-arg nvrmdma and registers the debug.nvrm_dma_*
// sysctls. It logs one line and sets the "nvrm-dma" property on the PCI nub.
// Call it while starting a GPU, only when nv->dma_dev is NULL, BEFORE rm_init_private_state (the RM asks
// nv_requires_dma_remap inside it) and before the boot guard starts counting this start, and store the result in
// nv->dma_dev. Then read nvrm_dma_mode_of(nv):
//   NVRM_DMA_REFUSE   decline to start this GPU (not a failed boot), logging nvrm_dma_why(nv); nothing else to undo
//                     here, nvrm_dma_detach() in the stop path frees the record
//   any other mode    carry on
// Returns NULL only when out of memory: nothing is known about the bus, so decline the start the same way.
struct nv_dma_device *nvrm_dma_attach(nv_state_t *nv, IOPCIDevice *pci);

// The mode of a GPU, as enum nvrm_dma_mode. Any context: no lock, no sleep. NVRM_DMA_NONE for NULL, for the control
// state and for a GPU that was never attached or has been detached.
int nvrm_dma_mode_of(const nv_state_t *nv);

// One line for the log and the IORegistry: the mode and, for a refusal, why. Any context. The string is static: it
// stays valid after a detach.
const char *nvrm_dma_why(const nv_state_t *nv);

// 1 when any attached GPU is translated by a mapper the driver uses (mode SYSTEM or DEVICE), else 0. Any context: no
// lock. The page allocator asks before each allocation: when it answers 1, a request for physically contiguous memory
// must also ask IOKit for host-contiguous memory, so that the first page address is again the start of a real run.
int nvrm_dma_any_remap(void);

// Undo nvrm_dma_attach. Thread context; may sleep. Call it in the stop path and after a start that failed, AFTER
// rm_free_private_state (the RM has then unmapped what it is going to unmap) and after the kext's own queues were
// flushed. Mappings the RM left behind are removed here (counted and logged; the GPU is stopped, so the translations
// can go), the mapper is released, the record is freed and nv->dma_dev becomes NULL. Does nothing for a GPU that was
// never attached, so it is safe to call twice and after a declined start.
void nvrm_dma_detach(nv_state_t *nv);

// ---- nvrm_dma.cpp, called from the surface-sharing paths ----

// The platform function "nvSurfDma" (nvrm_surfshare_abi.h: ops, fields and status values are specified there). Thread
// context; MAP and UNMAP may sleep. Call it BEFORE the power-management guard of callPlatformFunction: it touches no
// hardware and must not wake the GPU. Returns 0 when it answered in q->status, which is every call with a request to
// write into; the caller turns 0 into kIOReturnSuccess and anything else into kIOReturnIOError.
int nvrm_dma_surf(struct NVRMSurfDma *q, nv_state_t *nv);

// May this list of page addresses be given to the copy engine as bus addresses? Thread context (takes the DMA lock).
// 1 in mode NVRM_DMA_NONE, always: the addresses are physical and so is the bus. In modes SYSTEM and DEVICE, 1 only when
// every page lies inside one live mapping that nvrm_dma_surf made on this GPU's mapper. 0 otherwise (counted, the
// first few logged): the caller must not start the copy and answers NVRM_SS_NO_DMA. This is the one place a raw
// address list enters the RM, so every caller of the page-off copy is covered by asking here first.
int nvrm_dma_pages_ok(nv_state_t *nv, const NvU64 *pages, NvU64 n);

// ---- os-xnu2.cpp, called from nvrm_dma.cpp ----

// The address width the RM last set for device memory (nv_set_dma_address_size); 64 until it sets one. Any context.
// The DMA layer reads it at every mapping, because the RM lowers it around single allocations.
unsigned nv_xnu_dma_bits_now(void);

// The memory behind the private data the RM passes to nv_dma_map_alloc: an allocation of nv_alloc_pages, or pages a
// process registered. Thread context; takes the allocator's own spin lock briefly and nothing else.
// Returns the descriptor RETAINED (the caller releases it), with *bytes its length, *force32 whether the RM asked for
// memory below 4 GB, and *user whether these are a process's pinned pages. Returns NULL, with the outputs untouched,
// when priv is neither, or is an allocation already freed or waiting in the free quarantine (an allocation is
// recognised by finding the pointer on the live list before it is dereferenced). The caller then refuses the mapping.
// The descriptor's pages stay alive for as long as any reference or sub-descriptor of it exists: a mapping keeps its
// own reference and its own wire count, and does not look at the allocation again.
IOMemoryDescriptor *nv_xnu_dma_source(void *priv, NvU64 *bytes, int *force32, int *user);

}
