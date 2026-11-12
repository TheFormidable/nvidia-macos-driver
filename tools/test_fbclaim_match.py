#!/usr/bin/env python3
"""NVRMFBClaim retires IONDRVFramebuffer on the NVIDIA GPU only (#119: with IONameMatch "display" alone it also held the
IOFramebuffer category on a muxless laptop's Intel GPU, whose panel then had no framebuffer at all).

Reads the framebuffer's Info.plist and NVRM.kext's, then applies IOPCIFamily's masked compares (IOPCIPrimaryMatch: the
(device << 16 | vendor) word; IOPCIClassMatch: the class/revision register) to the devices that matter."""
import pathlib
import plistlib
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FB = [ROOT / "kexts/NVRMFB/fb/Info.plist"]


def claim_holders(root):
    """The Info.plist files under root/kexts that declare the NVRMFBClaim personality."""
    return sorted(str(p.relative_to(root)) for p in (root / "kexts").rglob("Info.plist")
                  if "NVRMFBClaim" in p.read_text(errors="replace"))


def matches(spec, word):
    value, _, mask = spec.partition("&")
    value, mask = int(value, 16), int(mask or "0xffffffff", 16)
    return (word & mask) == (value & mask)


def claims(p, vendor, device, cls, subvendor=0, subdevice=0):
    # IOPCIBridge::pciMatchNub: every PCI key present must match. IOPCIMatch takes the primary (device << 16 | vendor)
    # or the subsystem (subdevice << 16 | subvendor) word; IOPCIPrimaryMatch only the primary one. No PCI key: IONameMatch
    # "display" alone decides, and IOPCIFamily names every base-class-03 device "display".
    primary, sub = device << 16 | vendor, subdevice << 16 | subvendor
    ok = cls >> 16 == 0x03
    if "IOPCIMatch" in p:
        ok = ok and (matches(p["IOPCIMatch"], primary) or matches(p["IOPCIMatch"], sub))
    if "IOPCIPrimaryMatch" in p:
        ok = ok and matches(p["IOPCIPrimaryMatch"], primary)
    if "IOPCIClassMatch" in p:
        ok = ok and matches(p["IOPCIClassMatch"], cls << 8)
    return ok


class FBClaim(unittest.TestCase):
    def setUp(self):
        self.claim = plistlib.loads(FB[0].read_bytes())["IOKitPersonalities"]["NVRMFBClaim"]

    def test_one_fb_plist(self):
        # kexts/NVRM/fb held a second copy of this plist that no build used, and this test kept the two identical.
        # The copy is gone; a new one would drift unseen, so the claim may be declared in exactly one place.
        self.assertEqual(claim_holders(ROOT), ["kexts/NVRMFB/fb/Info.plist"])

    def test_a_second_copy_is_found(self):
        # control for the arm above: the search reports every plist that declares the claim, wherever it lies
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            for rel, text in (("kexts/NVRMFB/fb/Info.plist", "<key>NVRMFBClaim</key>"),
                              ("kexts/NVRM/fb/Info.plist", "<key>NVRMFBClaim</key>"),
                              ("kexts/NVRM/Info.plist", "<key>NVRM</key>")):
                (d / rel).parent.mkdir(parents=True, exist_ok=True)
                (d / rel).write_text(text)
            self.assertEqual(claim_holders(d), ["kexts/NVRM/fb/Info.plist", "kexts/NVRMFB/fb/Info.plist"])

    def test_same_vendor_and_class_match_as_nvrm(self):
        nvrm = plistlib.loads((ROOT / "kexts/NVRM/Info.plist").read_bytes())["IOKitPersonalities"]["NVRM"]
        for key in ("IOPCIPrimaryMatch", "IOPCIClassMatch"):
            self.assertEqual(self.claim[key], nvrm[key], key)
        self.assertNotIn("IOPCIMatch", self.claim)

    def test_still_outranks_the_firmware_framebuffer(self):
        self.assertEqual(self.claim["IOProviderClass"], "IOPCIDevice")
        self.assertEqual(self.claim["IONameMatch"], "display")
        self.assertEqual(self.claim["IOMatchCategory"], "IOFramebuffer")
        self.assertGreater(self.claim["IOProbeScore"], 20000)   # IONDRVFramebuffer (.Display_boot)

    def test_devices(self):
        cases = [
            ("RTX 5060", 0x10DE, 0x2D05, 0x030000, 0, True),
            ("Quadro T500, 3D controller", 0x10DE, 0x1FBB, 0x030200, 0, True),
            ("Iris Xe (Tiger Lake)", 0x8086, 0x9A49, 0x030000, 0x17AA, False),
            ("Intel GPU, NVIDIA subsystem", 0x8086, 0x9A49, 0x030000, 0x10DE, False),
            ("NVIDIA HD Audio", 0x10DE, 0x22BE, 0x040300, 0, False),
        ]
        for name, vendor, device, cls, sub, want in cases:
            with self.subTest(name):
                self.assertEqual(claims(self.claim, vendor, device, cls, sub), want)

    def test_boot_fb_is_nvidia_primary_only(self):
        # driver-off firmware framebuffer: the same rule, an Intel GPU with an NVIDIA subsystem id is not ours
        boot = plistlib.loads(FB[0].read_bytes())["IOKitPersonalities"]["NVRMBootFB"]
        self.assertNotIn("IOPCIMatch", boot)
        self.assertTrue(claims(boot, 0x10DE, 0x2D05, 0x030000))
        self.assertFalse(claims(boot, 0x8086, 0x9A49, 0x030000, 0x10DE))

    def test_controls(self):
        # the personality before this fix (IONameMatch only) took the Intel GPU (#119) ...
        old = {k: v for k, v in self.claim.items() if not k.startswith("IOPCI")}
        self.assertTrue(claims(old, 0x8086, 0x9A49, 0x030000, 0x17AA))
        # ... and IOPCIMatch instead of IOPCIPrimaryMatch would take an Intel GPU with an NVIDIA subsystem id
        loose = dict(old, IOPCIMatch="0x000010de&0x0000ffff", IOPCIClassMatch=self.claim["IOPCIClassMatch"])
        self.assertTrue(claims(loose, 0x8086, 0x9A49, 0x030000, 0x10DE))
        self.assertFalse(claims(self.claim, 0x8086, 0x9A49, 0x030000, 0x10DE))


if __name__ == "__main__":
    unittest.main()
