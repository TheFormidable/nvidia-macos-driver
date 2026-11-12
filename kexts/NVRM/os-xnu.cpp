/*
 * NullMoth NVIDIA driver for macOS
 * Copyright (c) 2026 NullMoth Systems.
 * SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
 */

#include "nv-xnu.h"
#include "nvrm_itree.h"

extern "C" void nv_xnu_power_state(bool *acPlugged, bool *haveBattery);   // nv_xnu_power.cpp
#include <sys/kauth.h>
#include <mach/mach_time.h>
#include <mach/semaphore.h>
#include <kern/task.h>
#include <kern/clock.h>
#include <sys/systm.h>
#include <sys/proc.h>
#include <sys/random.h>
#include <mach/vm_param.h>
#include <kern/locks.h>
#include <sys/sysctl.h>

#define NV_XNU_TRC do { static bool _t = false; if (!_t) { _t = true; kprintf("NVRM-xnu: >%s\n", __func__); } } while (0)

static inline void nvx_memset(void *d, int c, size_t n) { __asm__ __volatile__("rep stosb" : "+D"(d), "+c"(n) : "a"(c) : "memory"); }
static inline void nvx_memcpy(void *d, const void *s, size_t n) { __asm__ __volatile__("rep movsb" : "+D"(d), "+S"(s), "+c"(n) : : "memory"); }

extern "C" {

NvU64 os_page_size     = PAGE_SIZE;
NvU64 os_page_mask     = ~(NvU64)(PAGE_SIZE - 1);
NvU8  os_page_shift    = PAGE_SHIFT;
NvU64 os_max_page_size = PAGE_SIZE;
NV_STATUS NV_API_CALL os_alloc_mem(void **address, NvU64 size)
{ NV_XNU_TRC;
    *address = kern_os_malloc((size_t)size);
    if (*address == NULL) return NV_ERR_NO_MEMORY;
    nvx_memset(*address, 0, (size_t)size);
    return NV_OK;
}
void  NV_API_CALL os_free_mem(void *address) { NV_XNU_TRC; if (address) kern_os_free(address); }
void *NV_API_CALL os_mem_copy(void *dst, const void *src, NvU32 length) { static unsigned long n = 0; if (++n <= 4 || (n % 1000000) == 0) kprintf("NVRM-xnu: >os_mem_copy #%lu len=%u\n", n, length); nvx_memcpy(dst, src, length); return dst; }
NV_STATUS NV_API_CALL os_memcpy_from_user(void *dst, const void *src, NvU32 length) { NV_XNU_TRC; return copyin((user_addr_t)(uintptr_t)src, dst, length) ? NV_ERR_INVALID_ADDRESS : NV_OK; }
NV_STATUS NV_API_CALL os_memcpy_to_user(void *dst, const void *src, NvU32 length) { NV_XNU_TRC; return copyout(src, (user_addr_t)(uintptr_t)dst, length) ? NV_ERR_INVALID_ADDRESS : NV_OK; }
void *NV_API_CALL os_mem_set(void *dst, NvU8 c, NvU32 length)
{
    static unsigned long n = 0; n++;
    if (n <= 4 || (n % 1000000) == 0) kprintf("NVRM-xnu: >os_mem_set #%lu len=%u\n", n, length);
    nvx_memset(dst, c, length);
    return dst;
}
NvS32 NV_API_CALL os_mem_cmp(const NvU8 *a, const NvU8 *b, NvU32 length) { NV_XNU_TRC; return memcmp(a, b, length); }
NvU32 NV_API_CALL os_string_length(const char *s) { NV_XNU_TRC; return (NvU32)strlen(s); }
char *NV_API_CALL os_string_copy(char *dst, const char *src) { NV_XNU_TRC; char *d = dst; while ((*d++ = *src++) != 0) {} return dst; }
NvS32 NV_API_CALL os_string_compare(const char *a, const char *b) { NV_XNU_TRC; return strcmp(a, b); }
NvS32 NV_API_CALL os_snprintf(char *buf, NvU32 size, const char *fmt, ...)
{ NV_XNU_TRC;
    va_list ap; va_start(ap, fmt); int n = vsnprintf(buf, size, fmt, ap); va_end(ap); return n;
}
NvU32 NV_API_CALL os_get_cpu_count(void) { NV_XNU_TRC; return 4; }
NvU32 NV_API_CALL os_get_cpu_number(void) { NV_XNU_TRC; return 0; }

int NV_API_CALL nv_printf(NvU32 debuglevel, const char *printf_format, ...)
{ NV_XNU_TRC;
    char buf[512];
    va_list ap; va_start(ap, printf_format);
    vsnprintf(buf, sizeof buf, printf_format, ap);
    va_end(ap);
    buf[sizeof buf - 1] = 0;
    kprintf("%s", buf);
    IOLog("%s", buf);
    return 0;
}
int NV_API_CALL nv_dev_vprintf(struct nv_state_t *nv, NV_LOG_LEVEL level, const char *format, va_list ap)
{ NV_XNU_TRC;
    char buf[512];
    vsnprintf(buf, sizeof buf, format, ap);
    buf[sizeof buf - 1] = 0;
    kprintf("%s", buf);
    IOLog("%s", buf);
    return 0;
}
NV_STATUS NV_API_CALL nv_log_error(nv_state_t *nv, NvU32 error_number, const char *format, va_list ap)
{ NV_XNU_TRC;
    char buf[512];
    vsnprintf(buf, sizeof buf, format, ap);
    buf[sizeof buf - 1] = 0;
    kprintf("NVRM: Xid %u: %s\n", error_number, buf);
    IOLog("NVRM: Xid %u: %s\n", error_number, buf);
    return NV_OK;
}
#include <kern/thread_call.h>
#define NV_XNU_LOG_SLOTS 256
#define NV_XNU_LOG_LEN   256
static struct { volatile int ready; char text[NV_XNU_LOG_LEN]; } nv_xnu_log_ring[NV_XNU_LOG_SLOTS];
static volatile unsigned nv_xnu_log_head, nv_xnu_log_tail, nv_xnu_log_dropped;
static volatile int nv_xnu_log_draining;
static thread_call_t nv_xnu_log_call;

static void nv_xnu_log_drain(thread_call_param_t, thread_call_param_t)
{
    if (__atomic_exchange_n(&nv_xnu_log_draining, 1, __ATOMIC_ACQUIRE)) return;
    for (;;) {
        unsigned t = nv_xnu_log_tail;
        auto *slot = &nv_xnu_log_ring[t & (NV_XNU_LOG_SLOTS - 1)];
        if (!__atomic_load_n(&slot->ready, __ATOMIC_ACQUIRE)) break;
        IOLog("%s", slot->text);
        __atomic_store_n(&slot->ready, 0, __ATOMIC_RELEASE);
        nv_xnu_log_tail = t + 1;
    }
    unsigned d = __atomic_exchange_n(&nv_xnu_log_dropped, 0u, __ATOMIC_RELAXED);
    if (d) IOLog("NVRM-xnu: out_string ring full -- %u RM line(s) dropped (serial still has them)\n", d);
    __atomic_store_n(&nv_xnu_log_draining, 0, __ATOMIC_RELEASE);
}

void nv_xnu_log_init(void)
{
    if (!nv_xnu_log_call) nv_xnu_log_call = thread_call_allocate(nv_xnu_log_drain, NULL);
}

void NV_API_CALL out_string(const char *str)
{
    if (!str) return;
    kprintf("%s", str);
    if (!nv_xnu_log_call) return;
    unsigned h = __atomic_load_n(&nv_xnu_log_head, __ATOMIC_RELAXED);
    for (;;) {
        if (__atomic_load_n(&nv_xnu_log_ring[h & (NV_XNU_LOG_SLOTS - 1)].ready, __ATOMIC_ACQUIRE)) {
            __atomic_fetch_add(&nv_xnu_log_dropped, 1u, __ATOMIC_RELAXED); thread_call_enter(nv_xnu_log_call); return;
        }
        if (__atomic_compare_exchange_n(&nv_xnu_log_head, &h, h + 1, false, __ATOMIC_ACQ_REL, __ATOMIC_RELAXED)) break;
    }
    auto *slot = &nv_xnu_log_ring[h & (NV_XNU_LOG_SLOTS - 1)];
    unsigned i = 0; for (; i < NV_XNU_LOG_LEN - 1 && str[i]; i++) slot->text[i] = str[i];
    slot->text[i] = 0;
    __atomic_store_n(&slot->ready, 1, __ATOMIC_RELEASE);
    thread_call_enter(nv_xnu_log_call);
}
void NV_API_CALL os_dbg_init(void) { NV_XNU_TRC;}
void NV_API_CALL os_dbg_set_level(NvU32 level) { NV_XNU_TRC;}
void NV_API_CALL os_dbg_breakpoint(void) { NV_XNU_TRC; kprintf("NVRM-xnu: os_dbg_breakpoint\n"); }

NV_STATUS NV_API_CALL os_alloc_mutex(void **pp) { NV_XNU_TRC; *pp = IOLockAlloc(); return *pp ? NV_OK : NV_ERR_NO_MEMORY; }
void      NV_API_CALL os_free_mutex(void *p) { NV_XNU_TRC; if (p) IOLockFree((IOLock *)p); }
NV_STATUS NV_API_CALL os_acquire_mutex(void *p) { NV_XNU_TRC; IOLockLock((IOLock *)p); return NV_OK; }
NV_STATUS NV_API_CALL os_cond_acquire_mutex(void *p) { NV_XNU_TRC; return IOLockTryLock((IOLock *)p) ? NV_OK : NV_ERR_TIMEOUT_RETRY; }
void      NV_API_CALL os_release_mutex(void *p) { NV_XNU_TRC; IOLockUnlock((IOLock *)p); }

// The saved interrupt state is CARRIED BY THE CALLER, never stored in the lock.
//
// This used to hold the state in the lock object: acquire did `sp->s = IOSimpleLockLockDisableInterrupt(...)`
// and returned 0, and release passed `sp->s` back. That is a kernel panic, and it was reported 37 times in the
// Support logs as
//     panic [kalloc.type.varN.16]: element modified after free (off:8, val:0x0000000000000002, sz:16)
// with the trace NVRM::autoGo -> rm_init_rm -> RmInitRm -> nvlogInit -> tlsInitialize ->
// portSyncSpinlockCreate -> os_alloc_spinlock -> kern_os_malloc -> kalloc_ext. The figures identify it
// exactly: the old struct was 16 bytes, `s` sat at offset 8, and a saved interrupt mask is a small integer
// like 0x2 - so the allocator was reporting OUR write at offset 8 of a 16-byte block that had already been
// freed. Two ways it goes wrong, and both were live:
//   - two CPUs holding the same lock overwrite each other's saved state, so one of them restores the wrong
//     interrupt mask on release;
//   - a lock freed while a thread sits between acquire and release gets `sp->s` written after the free,
//     which is the poison the allocator catches at the next 16-byte allocation - usually the next spinlock
//     this very driver asks for during init, which is why the panic kept landing on NVRM's own start-up.
// The allocator reports corruption at the NEXT allocation of that size class, so a trace like the one above
// names the finder, not always the culprit; here they are the same code, and the numbers (size 16, offset 8,
// a small mask value) tie the report to this struct rather than to a guess.
//
// NVIDIA's own contract is plain in the signatures it declares for us: acquire RETURNS the state and release
// is handed it back as `old`. Honouring it removes the shared mutable field altogether, so the lock object is
// immutable once created and there is nothing for a late writer to corrupt.
struct nv_xnu_spin { IOSimpleLock *l; };
NV_STATUS NV_API_CALL os_alloc_spinlock(void **pp)
{ NV_XNU_TRC;
    nv_xnu_spin *sp = (nv_xnu_spin *)kern_os_malloc(sizeof *sp);
    if (!sp) return NV_ERR_NO_MEMORY;
    sp->l = IOSimpleLockAlloc();
    if (!sp->l) { kern_os_free(sp); return NV_ERR_NO_MEMORY; }
    *pp = sp; return NV_OK;
}
void  NV_API_CALL os_free_spinlock(void *p) { NV_XNU_TRC; nv_xnu_spin *sp = (nv_xnu_spin *)p; if (sp) { IOSimpleLockFree(sp->l); kern_os_free(sp); } }
NvU64 NV_API_CALL os_acquire_spinlock(void *p) { NV_XNU_TRC; nv_xnu_spin *sp = (nv_xnu_spin *)p; return (NvU64)IOSimpleLockLockDisableInterrupt(sp->l); }
void  NV_API_CALL os_release_spinlock(void *p, NvU64 old) { NV_XNU_TRC; nv_xnu_spin *sp = (nv_xnu_spin *)p; IOSimpleLockUnlockEnableInterrupt(sp->l, (IOInterruptState)old); }

void     *NV_API_CALL os_alloc_rwlock(void) { NV_XNU_TRC; return IORWLockAlloc(); }
void      NV_API_CALL os_free_rwlock(void *p) { NV_XNU_TRC; if (p) IORWLockFree((IORWLock *)p); }
NV_STATUS NV_API_CALL os_acquire_rwlock_read(void *p) { NV_XNU_TRC; IORWLockRead((IORWLock *)p); return NV_OK; }
NV_STATUS NV_API_CALL os_acquire_rwlock_write(void *p) { NV_XNU_TRC; IORWLockWrite((IORWLock *)p); return NV_OK; }
NV_STATUS NV_API_CALL os_cond_acquire_rwlock_read(void *p) { NV_XNU_TRC; return lck_rw_try_lock(IORWLockGetMachLock((IORWLock *)p), LCK_RW_TYPE_SHARED) ? NV_OK : NV_ERR_TIMEOUT_RETRY; }
NV_STATUS NV_API_CALL os_cond_acquire_rwlock_write(void *p) { NV_XNU_TRC; return lck_rw_try_lock(IORWLockGetMachLock((IORWLock *)p), LCK_RW_TYPE_EXCLUSIVE) ? NV_OK : NV_ERR_TIMEOUT_RETRY; }
void      NV_API_CALL os_release_rwlock_read(void *p) { NV_XNU_TRC; IORWLockUnlock((IORWLock *)p); }
void      NV_API_CALL os_release_rwlock_write(void *p) { NV_XNU_TRC; IORWLockUnlock((IORWLock *)p); }

void *NV_API_CALL os_alloc_semaphore(NvU32 initial)
{ NV_XNU_TRC;
    semaphore_t *s = (semaphore_t *)kern_os_malloc(sizeof *s);
    if (!s) return NULL;
    if (semaphore_create(kernel_task, s, SYNC_POLICY_FIFO, (int)initial) != KERN_SUCCESS) { kern_os_free(s); return NULL; }
    return s;
}
void      NV_API_CALL os_free_semaphore(void *p) { NV_XNU_TRC; semaphore_t *s = (semaphore_t *)p; if (s) { semaphore_destroy(kernel_task, *s); kern_os_free(s); } }
NV_STATUS NV_API_CALL os_acquire_semaphore(void *p) { NV_XNU_TRC; return semaphore_wait(*(semaphore_t *)p) == KERN_SUCCESS ? NV_OK : NV_ERR_GENERIC; }
NV_STATUS NV_API_CALL os_cond_acquire_semaphore(void *p) { NV_XNU_TRC; return semaphore_wait_noblock(*(semaphore_t *)p) == KERN_SUCCESS ? NV_OK : NV_ERR_TIMEOUT_RETRY; }
NV_STATUS NV_API_CALL os_release_semaphore(void *p) { NV_XNU_TRC; semaphore_signal(*(semaphore_t *)p); return NV_OK; }

NvBool NV_API_CALL os_semaphore_may_sleep(void) { NV_XNU_TRC; return NV_TRUE; }
volatile thread_t nv_xnu_isr_thread = NULL;
volatile int nv_xnu_in_isr = 0;
NvBool NV_API_CALL os_is_isr(void) { return (nv_xnu_isr_thread != NULL && nv_xnu_isr_thread == current_thread()) ? NV_TRUE : NV_FALSE; }
NvBool NV_API_CALL os_is_administrator(void) { NV_XNU_TRC; return (kauth_getuid() == 0) ? NV_TRUE : NV_FALSE; }
NV_STATUS NV_API_CALL os_get_euid(NvU32 *pSecToken) { NV_XNU_TRC; if (!pSecToken) return NV_ERR_INVALID_ARGUMENT; *pSecToken = (NvU32)kauth_getuid(); return NV_OK; }

NvU64 NV_API_CALL os_get_monotonic_time_ns(void)    { NV_XNU_TRC; uint64_t ns; absolutetime_to_nanoseconds(mach_absolute_time(), &ns); return ns; }
NvU64 NV_API_CALL os_get_monotonic_time_ns_hr(void) { NV_XNU_TRC; uint64_t ns; absolutetime_to_nanoseconds(mach_absolute_time(), &ns); return ns; }
NV_STATUS NV_API_CALL os_get_system_time(NvU32 *sec, NvU32 *usec) { NV_XNU_TRC; clock_sec_t s; clock_usec_t us; clock_get_calendar_microtime(&s, &us); *sec = (NvU32)s; *usec = (NvU32)us; return NV_OK; }
// A battery means a notebook, and the firmware asks both questions to decide whether to run its mobile policy at
// all. Both used to answer a flat no, so a laptop was treated as a desktop no matter what it was running on.
extern "C" NvBool nv_xnu_power_is_notebook(void);   // os-xnu2.cpp: the NVPCF device or a battery in ACPI, with a platform-power boot-arg set
NvBool NV_API_CALL nv_is_chassis_notebook(void)
{
    NV_XNU_TRC;
    bool ac, bat; nv_xnu_power_state(&ac, &bat);
    return (bat || nv_xnu_power_is_notebook()) ? NV_TRUE : NV_FALSE;
}
NvBool NV_API_CALL nv_acpi_is_battery_present(void) { NV_XNU_TRC; bool ac, bat; nv_xnu_power_state(&ac, &bat); return bat ? NV_TRUE : NV_FALSE; }
NvBool NV_API_CALL nv_platform_supports_s0ix(void)  { NV_XNU_TRC; return NV_FALSE; }
NvBool NV_API_CALL os_is_nvswitch_present(void)     { NV_XNU_TRC; return NV_FALSE; }

NV_STATUS NV_API_CALL os_delay(NvU32 ms) { NV_XNU_TRC; IOSleep(ms); return NV_OK; }
NV_STATUS NV_API_CALL os_delay_us(NvU32 us) { NV_XNU_TRC; IODelay(us); return NV_OK; }
NvU64 NV_API_CALL os_get_cpu_frequency(void) { NV_XNU_TRC; return 0; }
NvU32 NV_API_CALL os_get_current_process(void) { NV_XNU_TRC; return (NvU32)proc_selfpid(); }
void  NV_API_CALL os_get_current_process_name(char *buf, NvU32 len) { NV_XNU_TRC; proc_selfname(buf, (int)len); }
NV_STATUS NV_API_CALL os_get_current_thread(NvU64 *id) { NV_XNU_TRC; *id = (NvU64)(uintptr_t)current_thread(); return NV_OK; }
void *NV_API_CALL os_get_pid_info(void) { NV_XNU_TRC; return NULL; }
void  NV_API_CALL os_put_pid_info(void *pid_info) { NV_XNU_TRC;}
NV_STATUS NV_API_CALL os_find_ns_pid(void *pid_info, NvU32 *ns_pid) { NV_XNU_TRC; return NV_ERR_NOT_SUPPORTED; }
NV_STATUS NV_API_CALL os_get_random_bytes(NvU8 *buf, NvU16 n) { NV_XNU_TRC; read_random(buf, n); return NV_OK; }

#define NV_XNU_MAX_PCI 256
static nv_xnu_pci_slot nv_xnu_slots[NV_XNU_MAX_PCI];
static int nv_xnu_nslots;
void nv_xnu_register_pci(IOPCIDevice *pci, NvU32 domain, NvU8 bus, NvU8 slot, NvU8 function)
{
    if (!pci) return;
    for (int i = 0; i < nv_xnu_nslots; i++) {
        nv_xnu_pci_slot *s = &nv_xnu_slots[i];
        if (s->pci == pci ||
            (s->domain == domain && s->bus == bus && s->slot == slot && s->function == function)) {
            return; // already registered
        }
    }
    // Retained: RM keeps these handles for config-space access for as long as the driver runs, and a device behind
    // Thunderbolt or a dock can be terminated meanwhile. The table lives as long as the kext.
    if (nv_xnu_nslots < NV_XNU_MAX_PCI) { pci->retain(); nv_xnu_slots[nv_xnu_nslots++] = { pci, domain, bus, slot, function }; }
}

// The RM enumerates the whole PCI tree through os_pci_init_handle to find the
// host bridge (FHB), root ports and P2P bridges for its chipset object. Only the
// GPU used to be registered here, so that enumeration came up empty and the RM
// logged "FHB/P2P/3DCTRL not found in cached bus topology" + "Unable to get PCI
// port handles". Walk every IOPCIDevice in the IORegistry and register them all
// so the RM sees the real topology.
void nv_xnu_register_all_pci(void)
{
    OSDictionary *match = IOService::serviceMatching("IOPCIDevice");
    if (!match) return;
    OSIterator *it = IOService::getMatchingServices(match);
    if (it) {
        OSObject *obj;
        while ((obj = it->getNextObject()) != NULL) {
            IOPCIDevice *pd = OSDynamicCast(IOPCIDevice, obj);
            if (pd) {
                nv_xnu_register_pci(pd, 0, pd->getBusNumber(), pd->getDeviceNumber(), pd->getFunctionNumber());
            }
        }
        it->release();
    }
    match->release();
}
void *NV_API_CALL os_pci_init_handle(NvU32 domain, NvU8 bus, NvU8 slot, NvU8 function, NvU16 *vendor, NvU16 *device)
{ NV_XNU_TRC;
    for (int i = 0; i < nv_xnu_nslots; i++) {
        nv_xnu_pci_slot *s = &nv_xnu_slots[i];
        if (s->domain == domain && s->bus == bus && s->slot == slot && s->function == function) {
            if (vendor) *vendor = s->pci->configRead16(0x00);
            if (device) *device = s->pci->configRead16(0x02);
            return s;
        }
    }
    return NULL;
}
#define NV_PCI_H(h) do { if (!(h)) { static bool _w; if (!_w) { _w = true; kprintf("NVRM-xnu: %s: NULL pci handle\n", __func__); } return NV_ERR_INVALID_ARGUMENT; } } while (0)
NV_STATUS NV_API_CALL os_pci_read_byte(void *h, NvU32 off, NvU8 *v)   { NV_XNU_TRC; NV_PCI_H(h); *v = ((nv_xnu_pci_slot *)h)->pci->configRead8(off);  return NV_OK; }
NV_STATUS NV_API_CALL os_pci_read_word(void *h, NvU32 off, NvU16 *v)  { NV_XNU_TRC; NV_PCI_H(h); *v = ((nv_xnu_pci_slot *)h)->pci->configRead16(off); return NV_OK; }
// The GPU's Product Device Identifier lives in NVIDIA's DVSEC 3 in PCIe extended config space; RM builds the GPU UUID
// from it the way NVIDIA's own driver does (nv-pci.c). A board without that capability answers NOT_SUPPORTED and RM
// falls back to the PBI read. The walk is bounded the way nv-pci.c bounds it: offsets 0x100..0xfff, DWORD aligned,
// at most (0x1000 - 0x100) / 8 links, since a malformed chain must not loop.
NV_STATUS NV_API_CALL nv_pci_read_gpu_pdi_from_dvsec(nv_state_t *nv, NvU64 *pdi)
{ NV_XNU_TRC;
    if (!nv || !pdi || !nv->handle) return NV_ERR_INVALID_ARGUMENT;
    IOPCIDevice *pci = ((nv_xnu_pci_slot *)nv->handle)->pci;
    if (!pci) return NV_ERR_INVALID_ARGUMENT;
    NvU32 cap = 0x100;
    for (NvU32 i = 0; i < (0x1000 - 0x100) / 8; i++) {
        if (cap < 0x100 || cap >= 0x1000 || (cap & 3)) return NV_ERR_INVALID_OFFSET;
        const NvU32 hdr0 = pci->extendedConfigRead32(cap);
        if (hdr0 == 0xffffffffu) return NV_ERR_INVALID_READ;
        if ((hdr0 & 0xffff) == 0x0023) {                       // DVSEC
            const NvU32 hdr1 = pci->extendedConfigRead32(cap + 0x4);
            const NvU32 hdr2 = pci->extendedConfigRead32(cap + 0x8);
            if ((hdr1 & 0xffff) == 0x10de && (hdr2 & 0xffff) == 0x3) {   // NVIDIA, DVSEC 3 = GPU PDI
                const NvU32 len = (hdr1 >> 20) & 0xfff;
                if (len < 0x14 || cap > 0x1000 - 0x14) return NV_ERR_INVALID_DATA;
                const NvU64 v = ((NvU64)pci->extendedConfigRead32(cap + 0x10) << 32) | pci->extendedConfigRead32(cap + 0xc);
                if (v == 0 || v == ~0ull) return NV_ERR_NOT_SUPPORTED;
                *pdi = v;
                return NV_OK;
            }
        }
        cap = (hdr0 >> 20) & 0xfff;
        if (cap == 0) return NV_ERR_NOT_SUPPORTED;
    }
    return NV_ERR_INVALID_OFFSET;
}
NV_STATUS NV_API_CALL os_pci_read_dword(void *h, NvU32 off, NvU32 *v) { NV_XNU_TRC; NV_PCI_H(h); *v = ((nv_xnu_pci_slot *)h)->pci->configRead32(off); return NV_OK; }
NV_STATUS NV_API_CALL os_pci_write_byte(void *h, NvU32 off, NvU8 v)   { NV_XNU_TRC; NV_PCI_H(h); ((nv_xnu_pci_slot *)h)->pci->configWrite8(off, v);  return NV_OK; }
NV_STATUS NV_API_CALL os_pci_write_word(void *h, NvU32 off, NvU16 v)  { NV_XNU_TRC; NV_PCI_H(h); ((nv_xnu_pci_slot *)h)->pci->configWrite16(off, v); return NV_OK; }
NV_STATUS NV_API_CALL os_pci_write_dword(void *h, NvU32 off, NvU32 v) { NV_XNU_TRC; NV_PCI_H(h); ((nv_xnu_pci_slot *)h)->pci->configWrite32(off, v); return NV_OK; }

extern "C" { void *nv_pm_alias(NvU64, NvU64, void **); void nv_pm_alias_drop(void *); bool nv_pm_is_gpu(NvU64, NvU64);
             void nv_pm_foreign(NvU64, NvU64, NvU32, void *); }
// Every kernel mapping RM makes is tracked here (VA -> physical for the framebuffer's overlap and aperture checks).
// This was a fixed array searched from the start. At 1024 entries a resizable BAR1 filled it in about 20 minutes; at
// 8192 it could still fill, and a mapping that did not fit was handed out untracked and never released. Freeing an
// allocation of S pieces with N mappings live also cost S x N steps, because each piece scanned the whole table.
//
// Now each mapping is an entry of its own, in two interval trees (nvrm_itree.h): by kernel VA, and by physical
// address for the real mappings (an alias of RAM is not a mapping of the GPU and was never in the overlap check). There
// is no capacity: an entry is allocated before anything is mapped, and when there is no memory for it nothing is
// mapped. Lookups and changes take one simple lock with interrupts off, a leaf lock: nothing is allocated, logged or
// called into IOKit under it. So the lookups are safe from any context, the framebuffer's interrupt path included, and
// a release can never run beside a lookup of the same entry. tools/test_nvrm_itree.c checks that the trees answer
// exactly what the array answered.
struct nv_xnu_map {
    struct nvit_node va_node;    // [va, va + size); every entry
    struct nvit_node pa_node;    // [phys, phys + size); real mappings only
    IOMemoryDescriptor *md; IOMemoryMap *map; void *alias;
    NvU64 phys, size; void *va;
};
static inline nv_xnu_map *nv_xnu_map_of_va(struct nvit_node *n) { return (nv_xnu_map *)(void *)n; }   // va_node comes first
static struct nvit_tree nv_xnu_map_by_va, nv_xnu_map_by_pa;
static IOSimpleLock *nv_xnu_maps_slock;
static int64_t nv_xnu_kmap_live, nv_xnu_kmap_peak, nv_xnu_kmap_lookups, nv_xnu_kmap_unmap_miss, nv_xnu_kmap_nomem;
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_kmap_live, CTLFLAG_RD | CTLFLAG_LOCKED, &nv_xnu_kmap_live, "kernel mappings of the RM tracked now");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_kmap_peak, CTLFLAG_RD | CTLFLAG_LOCKED, &nv_xnu_kmap_peak, "most kernel mappings of the RM tracked at once");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_kmap_lookups, CTLFLAG_RD | CTLFLAG_LOCKED, &nv_xnu_kmap_lookups, "kernel VA to physical lookups");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_kmap_unmap_miss, CTLFLAG_RD | CTLFLAG_LOCKED, &nv_xnu_kmap_unmap_miss, "unmaps of an address that is not mapped");
SYSCTL_QUAD(_debug, OID_AUTO, nvrm_kmap_nomem, CTLFLAG_RD | CTLFLAG_LOCKED, &nv_xnu_kmap_nomem, "mappings refused for want of memory for their entry");
extern "C" bool nv_xnu_maps_init(void)
{
    if (!nv_xnu_maps_slock) {
        nv_xnu_maps_slock = IOSimpleLockAlloc();
        if (nv_xnu_maps_slock) {
            sysctl_register_oid(&sysctl__debug_nvrm_kmap_live); sysctl_register_oid(&sysctl__debug_nvrm_kmap_peak);
            sysctl_register_oid(&sysctl__debug_nvrm_kmap_lookups); sysctl_register_oid(&sysctl__debug_nvrm_kmap_unmap_miss);
            sysctl_register_oid(&sysctl__debug_nvrm_kmap_nomem);
        }
    }
    return nv_xnu_maps_slock != NULL;
}
// Fills `m` and puts it in the trees; returns how many entries are live.
static unsigned nv_xnu_map_insert(nv_xnu_map *m, IOMemoryDescriptor *md, IOMemoryMap *map,
                                  void *va, NvU64 phys, NvU64 size, void *alias)
{
    m->md = md; m->map = map; m->va = va; m->phys = phys; m->size = size; m->alias = alias;
    m->va_node.lo = (NvU64)(uintptr_t)va; m->va_node.end = (NvU64)(uintptr_t)va + size;
    m->pa_node.lo = phys; m->pa_node.end = phys + size;
    IOInterruptState s = IOSimpleLockLockDisableInterrupt(nv_xnu_maps_slock);
    nvit_insert(&nv_xnu_map_by_va, &m->va_node);
    if (!alias) nvit_insert(&nv_xnu_map_by_pa, &m->pa_node);
    const unsigned live = nv_xnu_map_by_va.count;
    if ((int64_t)live > nv_xnu_kmap_peak) nv_xnu_kmap_peak = live;
    nv_xnu_kmap_live = live;
    IOSimpleLockUnlockEnableInterrupt(nv_xnu_maps_slock, s);
    return live;
}
void *NV_API_CALL os_map_kernel_space(NvU64 start, NvU64 size, NvU32 mode)
{
    IOOptionBits cache = (mode == NV_MEMORY_CACHED) ? kIOMapDefaultCache : (mode == NV_MEMORY_WRITECOMBINED ? kIOMapWriteCombineCache : kIOMapInhibitCache);
    kprintf("NVRM-xnu: >os_map_kernel_space start=0x%llx size=0x%llx mode=%u\n", start, size, mode);
    if (!nv_xnu_maps_slock) return NULL;
    // The entry first: without memory for it nothing is mapped, so nothing can be handed out untracked.
    nv_xnu_map *m = (nv_xnu_map *)kern_os_malloc(sizeof *m);
    if (!m) {
        __atomic_fetch_add(&nv_xnu_kmap_nomem, 1, __ATOMIC_RELAXED);
        kprintf("NVRM-xnu:   no memory for the mapping's entry: 0x%llx (+0x%llx) not mapped\n", start, size);
        return NULL;
    }
    if (!nv_pm_is_gpu(start, size)) {
        void *cookie = NULL; void *ava = nv_pm_alias(start, size, &cookie);
        if (ava) {
            nv_xnu_map_insert(m, NULL, NULL, ava, start, size, cookie);
            return ava;
        }
        nv_pm_foreign(start, size, mode, __builtin_return_address(0));
    }
    IOMemoryDescriptor *md = IOMemoryDescriptor::withPhysicalAddress((IOPhysicalAddress)start, (IOByteCount)size, kIODirectionInOut);
    if (!md) { kprintf("NVRM-xnu:   withPhysicalAddress FAILED\n"); }
    IOMemoryMap *map = md ? md->map(cache) : NULL;
    if (md && !map) { kprintf("NVRM-xnu:   md->map(cache=0x%x) FAILED\n", (unsigned)cache); md->release(); md = NULL; }
    void *va = map ? (void *)map->getVirtualAddress() : NULL;
    if (!va) { kern_os_free(m); return NULL; }
    const unsigned live = nv_xnu_map_insert(m, md, map, va, start, size, NULL);
    kprintf("NVRM-xnu:   mapped -> 0x%llx (%u kernel mappings live)\n", (unsigned long long)(uintptr_t)va, live);
    return va;
}
NvU64 nv_xnu_phys_for_kernel_va(const void *va)
{
    if (!nv_xnu_maps_slock) return 0;
    __atomic_fetch_add(&nv_xnu_kmap_lookups, 1, __ATOMIC_RELAXED);
    NvU64 phys = 0;
    IOInterruptState s = IOSimpleLockLockDisableInterrupt(nv_xnu_maps_slock);
    struct nvit_node *n = nvit_stab(&nv_xnu_map_by_va, (NvU64)(uintptr_t)va);
    if (n) phys = nv_xnu_map_of_va(n)->phys + ((NvU64)(uintptr_t)va - n->lo);
    IOSimpleLockUnlockEnableInterrupt(nv_xnu_maps_slock, s);
    return phys;
}
void NV_API_CALL os_unmap_kernel_space(void *va, NvU64 size)
{ NV_XNU_TRC;
    if (!nv_xnu_maps_slock) return;
    // Two aliases of the same RAM can share a VA: the one of this size goes, else the first that was mapped.
    const NvU64 lo = (NvU64)(uintptr_t)va;
    nv_xnu_map *m = NULL;
    IOInterruptState s = IOSimpleLockLockDisableInterrupt(nv_xnu_maps_slock);
    struct nvit_node *first = nvit_first_lo(&nv_xnu_map_by_va, lo), *pick = NULL;
    for (struct nvit_node *n = first; n && n->lo == lo; n = nvit_next(n))
        if (nv_xnu_map_of_va(n)->size == size) { pick = n; break; }
    if (!pick) pick = first;
    if (pick) {
        m = nv_xnu_map_of_va(pick);
        nvit_remove(&nv_xnu_map_by_va, &m->va_node);
        if (!m->alias) nvit_remove(&nv_xnu_map_by_pa, &m->pa_node);
        nv_xnu_kmap_live = nv_xnu_map_by_va.count;
    }
    IOSimpleLockUnlockEnableInterrupt(nv_xnu_maps_slock, s);
    if (!m) {
        if (__atomic_fetch_add(&nv_xnu_kmap_unmap_miss, 1, __ATOMIC_RELAXED) < 4)
            kprintf("NVRM-xnu: unmap of 0x%llx (+0x%llx), which is not mapped\n", (unsigned long long)lo, size);
        return;
    }
    if (m->alias) nv_pm_alias_drop(m->alias);
    else { if (m->map) m->map->release(); if (m->md) m->md->release(); }
    kern_os_free(m);
}
int nv_xnu_kmap_overlap(NvU64 phys, NvU64 len)
{
    if (!nv_xnu_maps_slock) return 0;
    IOInterruptState s = IOSimpleLockLockDisableInterrupt(nv_xnu_maps_slock);
    const int hit = nvit_overlap(&nv_xnu_map_by_pa, phys, phys + len) != NULL;
    IOSimpleLockUnlockEnableInterrupt(nv_xnu_maps_slock, s);
    return hit;
}
void  NV_API_CALL os_io_write_byte(NvU32 addr, NvU8 v)   { NV_XNU_TRC; __asm__ __volatile__("outb %0, %1" :: "a"(v), "d"((NvU16)addr)); }
void  NV_API_CALL os_io_write_word(NvU32 addr, NvU16 v)  { NV_XNU_TRC; __asm__ __volatile__("outw %0, %1" :: "a"(v), "d"((NvU16)addr)); }
void  NV_API_CALL os_io_write_dword(NvU32 addr, NvU32 v) { NV_XNU_TRC; __asm__ __volatile__("outl %0, %1" :: "a"(v), "d"((NvU16)addr)); }
NvU8  NV_API_CALL os_io_read_byte(NvU32 addr)  { NV_XNU_TRC; NvU8 v;  __asm__ __volatile__("inb %1, %0" : "=a"(v) : "d"((NvU16)addr)); return v; }
NvU16 NV_API_CALL os_io_read_word(NvU32 addr)  { NV_XNU_TRC; NvU16 v; __asm__ __volatile__("inw %1, %0" : "=a"(v) : "d"((NvU16)addr)); return v; }
NvU32 NV_API_CALL os_io_read_dword(NvU32 addr) { NV_XNU_TRC; NvU32 v; __asm__ __volatile__("inl %1, %0" : "=a"(v) : "d"((NvU16)addr)); return v; }

NV_STATUS NV_API_CALL os_registry_init(void) { NV_XNU_TRC; return NV_OK; }
NvBool NV_API_CALL os_is_vgx_hyper(void) { NV_XNU_TRC; return NV_FALSE; }
NvBool NV_API_CALL os_is_grid_supported(void) { NV_XNU_TRC; return NV_FALSE; }
NvBool NV_API_CALL os_is_efi_enabled(void) { NV_XNU_TRC; return NV_TRUE; }
NV_STATUS NV_API_CALL os_get_is_openrm(NvBool *b) { NV_XNU_TRC; *b = NV_TRUE; return NV_OK; }
void   NV_API_CALL os_disable_console_access(void) { NV_XNU_TRC;}
void   NV_API_CALL os_enable_console_access(void) { NV_XNU_TRC;}
NV_STATUS NV_API_CALL os_flush_cpu_cache_all(void) { NV_XNU_TRC; return NV_OK; }
NV_STATUS NV_API_CALL os_flush_user_cache(void) { NV_XNU_TRC; return NV_OK; }
void   NV_API_CALL os_flush_cpu_write_combine_buffer(void) { NV_XNU_TRC; __asm__ __volatile__("sfence" ::: "memory"); }

}
