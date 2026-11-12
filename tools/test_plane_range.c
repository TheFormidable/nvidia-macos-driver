/* The rules of kexts/NVRM/accel/nvaccel_plane_range.h: where one plane of a two-plane IOSurface lives in the surface's
 * memory, what a ranged grant accepts, which validity bit a plane uses, and what a page-off copies.
 *   cc -std=c11 -Wall -Wextra -Werror -I kexts/NVRM -I kexts/NVRM/accel tools/test_plane_range.c -o /tmp/test_plane_range && /tmp/test_plane_range
 *   c++ -x c++ -std=c++17 -Wall -Wextra -Werror -I kexts/NVRM -I kexts/NVRM/accel tools/test_plane_range.c -o /tmp/test_plane_range_cpp && /tmp/test_plane_range_cpp
 * Negative controls: the check written with an addition (off + len > mdLen), a validity rule that takes the lowest bit
 * of two, and a dirty rule that marks 1 << plane without a range are each run over the same inputs and must fail an arm
 * the header passes. */
#include "nvrm_surfshare_abi.h"
#include "nvaccel_plane_range.h"
#include <stdio.h>

static int fails;
#define ARM(name, cond) do { const int ok_ = !!(cond); printf("%s  %s\n", ok_ ? "ok  " : "FAIL", name); if (!ok_) fails++; } while (0)

/* the controls: what a less careful version would have written */
static unsigned check_added(unsigned long long off, unsigned long long len, unsigned long long mdLen, unsigned long long vramBytes)
{
    if (!len || len > vramBytes) return NVRM_SS_TOO_SMALL;
    if (off + len > mdLen) return NVRM_SS_BAD_RANGE;
    return 0;
}
static unsigned short valid_lowest(unsigned short s, unsigned short v)
{
    const unsigned short m = (unsigned short)(s | v);
    return (unsigned short)(m ? (m & (unsigned short)-m) : 1);
}
static unsigned short dirty_shift(unsigned long long rangeLen, unsigned short validBit, unsigned plane)
{
    return rangeLen ? validBit : (unsigned short)(1u << plane);
}

int main(void)
{
    unsigned long long o = 1, l = 1, d = 1, b = 1;

    /* NV12 1920x1080: plane 0 at 0 (1920 * 1080 B), plane 1 right after it (1920 * 540 B) */
    nvaccel_range_decode((2073600ull << 32) | 1036800ull, &o, &l);
    ARM("the size word carries the offset (high 32 bits) and the length (low 32 bits)", o == 2073600ull && l == 1036800ull);
    nvaccel_range_decode(0xffffffffffffffffull, &o, &l);
    ARM("both halves are 32 bits at most", o == 0xffffffffull && l == 0xffffffffull);
    nvaccel_range_decode(4096ull, &o, &l);
    ARM("a size word with no offset is a plane at offset 0", o == 0 && l == 4096);

    ARM("a plane inside the surface and the VRAM is accepted", nvaccel_range_check(64, 4096, 8192, 65536) == 0);
    ARM("a plane that ends exactly at the end of the surface and fills its VRAM is accepted",
        nvaccel_range_check(4096, 4096, 8192, 4096) == 0);
    ARM("an empty plane is refused as too small", nvaccel_range_check(0, 0, 8192, 4096) == NVRM_SS_TOO_SMALL);
    ARM("a plane larger than its VRAM is refused as too small", nvaccel_range_check(0, 4097, 8192, 4096) == NVRM_SS_TOO_SMALL);
    ARM("a plane that runs past the end of the surface is a bad range", nvaccel_range_check(4097, 4096, 8192, 65536) == NVRM_SS_BAD_RANGE);
    ARM("an offset past the end of the surface is a bad range", nvaccel_range_check(8193, 1, 8192, 65536) == NVRM_SS_BAD_RANGE);
    ARM("an offset exactly at the end with any length is a bad range", nvaccel_range_check(8192, 1, 8192, 65536) == NVRM_SS_BAD_RANGE);
    ARM("a surface with no memory refuses every plane", nvaccel_range_check(0, 1, 0, 65536) == NVRM_SS_BAD_RANGE);
    ARM("offset + length near 2^64 does not wrap past the test",
        nvaccel_range_check(0xffffffffffffff00ull, 0x200, 8192, 65536) == NVRM_SS_BAD_RANGE);
    ARM("control: the check written with an addition lets that wrapped range through",
        check_added(0xffffffffffffff00ull, 0x200, 8192, 65536) == 0);
    ARM("the two refusals are the ABI's numbers (0x80000006, 0x80000009), and NO_DMA is another",
        NVRM_SS_TOO_SMALL == 0x80000006u && NVRM_SS_BAD_RANGE == 0x80000009u && NVRM_SS_NO_DMA != NVRM_SS_BAD_RANGE);

    ARM("a first grant takes any range", nvaccel_range_same(0, 0, 4096, 2048));
    ARM("a second grant with the same range is accepted", nvaccel_range_same(4096, 2048, 4096, 2048));
    ARM("a second grant that moves the range is refused", !nvaccel_range_same(4096, 2048, 4160, 2048) && !nvaccel_range_same(4096, 2048, 4096, 2049));

    ARM("validity: system memory current on bit 0 gives bit 0", nvaccel_valid_bit(1, 0) == 1);
    ARM("validity: VRAM current only, on bit 1, gives bit 1; both words on bit 1 give bit 1",
        nvaccel_valid_bit(0, 2) == 2 && nvaccel_valid_bit(2, 2) == 2);
    ARM("validity: a fresh resource (no bit) gets bit 0", nvaccel_valid_bit(0, 0) == 1);
    ARM("validity: two bits are not understood", nvaccel_valid_bit(1, 2) == 0 && nvaccel_valid_bit(3, 0) == 0 && nvaccel_valid_bit(0, 0x8001) == 0);
    ARM("validity: the top bit alone is understood", nvaccel_valid_bit(0x8000, 0) == 0x8000);
    ARM("control: taking the lowest of two bits would choose one", valid_lowest(1, 2) != 0);
    ARM("grant bit: a first grant records what the words say", nvaccel_grant_bit(0, 0, 2) == 2 && nvaccel_grant_bit(0, 0, 0) == 1);
    ARM("grant bit: a later grant that sees the recorded bit keeps it", nvaccel_grant_bit(2, 2, 0) == 2 && nvaccel_grant_bit(1, 0, 0) == 1);
    ARM("grant bit: a later grant that sees another bit, or two, is refused", nvaccel_grant_bit(1, 2, 0) == 0 && nvaccel_grant_bit(2, 3, 0) == 0);

    ARM("dirty mark: plane 0 without a range sets bit 0, as before", nvaccel_dirty_bit(0, 0, 0) == 1);
    ARM("dirty mark: a plane above 0 without a range is refused", nvaccel_dirty_bit(0, 0, 1) == 0 && nvaccel_dirty_bit(0, 4, 15) == 0);
    ARM("dirty mark: a ranged resource sets its recorded bit whatever the plane", nvaccel_dirty_bit(1036800, 2, 1) == 2 && nvaccel_dirty_bit(2073600, 1, 0) == 1);
    ARM("control: marking 1 << plane without a range accepts plane 1", dirty_shift(0, 0, 1) != 0);

    ARM("grant: on, unlocked wait, no mapper", nvaccel_planes_allowed(1, 1, 0, 0) == 1);
    ARM("grant: switched off by the boot-arg", nvaccel_planes_allowed(0, 1, 0, 1) == 0);
    ARM("grant: the page-off waits with the accelerator lock held", nvaccel_planes_allowed(1, 0, 0, 1) == 0);
    ARM("grant: behind a mapper only with the staging buffer mapped", nvaccel_planes_allowed(1, 1, 1, 0) == 0 && nvaccel_planes_allowed(1, 1, 1, 1) == 1
        && nvaccel_planes_allowed(1, 1, 2, 0) == 0 && nvaccel_planes_allowed(1, 1, 2, 1) == 1);
    ARM("grant: a mapper the driver may not use, or a mode it does not know, never", nvaccel_planes_allowed(1, 1, 3, 1) == 0 && nvaccel_planes_allowed(1, 1, 7, 1) == 0);

    /* not ranged: exactly what the page-off has always copied, whatever the range words hold */
    {
        int same = 1;
        static const unsigned long long md[] = { 0, 1, 4095, 4096, 8192, 1ull << 33 };
        static const unsigned long long vr[] = { 0, 1, 4096, 65536, 1ull << 32 };
        for (unsigned i = 0; i < sizeof md / sizeof md[0]; i++)
            for (unsigned j = 0; j < sizeof vr / sizeof vr[0]; j++) {
                const unsigned long long want = md[i] < vr[j] ? md[i] : vr[j];
                d = 7; b = 7;
                const int r = nvaccel_pageoff_span(0, 77, 99, md[i], vr[j], &d, &b);
                same &= d == 0 && b == want && r == (want != 0);
            }
        ARM("page-off, not ranged: offset 0 and the smaller of surface and VRAM, for every size", same);
    }
    ARM("page-off, ranged: the plane's range only", nvaccel_pageoff_span(1, 4160, 2048, 8192, 65536, &d, &b) && d == 4160 && b == 2048);
    ARM("page-off, ranged: VRAM that shrank since the grant copies nothing", !nvaccel_pageoff_span(1, 4160, 2048, 8192, 1024, &d, &b) && b == 0 && d == 0);
    ARM("page-off, ranged: a surface that is shorter than the range copies nothing", !nvaccel_pageoff_span(1, 4160, 2048, 6000, 65536, &d, &b) && b == 0);
    ARM("page-off, ranged: an empty range copies nothing", !nvaccel_pageoff_span(1, 0, 0, 8192, 65536, &d, &b) && b == 0);

    puts(fails ? "FAILED" : "PASS");
    return fails != 0;
}
