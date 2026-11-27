# Contributing

Bug reports and pull requests are welcome in TheFormidable's fork of the NullMoth driver (now 3.0.20, Release 2).
Issues go to the [fork's issue tracker](https://github.com/TheFormidable/nvidia-macos-driver/issues), pull requests
to the `fable` branch. Read [current status](docs/STATUS.md) before proposing a new feature. A problem with the
original project's own releases belongs to [the original project](https://github.com/nullmoth/nvidia-macos-driver):
its builds differ from this fork's.

## Reporting a bug

Look in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) first. If your problem is not there, open an issue with the
bug form and attach:

1. The package: the version (`cat /Library/NullMoth/driver-version`, or `cat VERSION` in its `pkgroot`) and, for a
   package you built, the commit from `NULLMOTH-LOCAL-BUILD.txt`.
2. The macOS build (`sw_vers`), the card, and your boot-args (`nvram boot-args`, or OpenCore's `config.plist`).
3. The whole output of `sudo ./install.sh`, copied from Terminal.
4. `sysctl debug.nvrm_switches debug.nvrm_switches_set debug.nvrm_switches_nondefault`, and
   `sysctl debug | grep -e nvrm -e nvaccel` taken while or right after the problem shows.
5. The kernel log of that boot, `log show --last boot --predicate 'process == "kernel"' > ~/kernel.txt`, and the
   plugin's lines, `log show --last boot --predicate 'eventMessage CONTAINS "NullMoth:"' > ~/nullmoth.txt`, plus
   `/Library/NullMoth/lastgood.log` and `/Library/NullMoth/bootguard.log` when they exist.
6. A few sentences of your own on what you see, and which way back you tried
   ([README](README.md#what-is-on-by-default-and-the-ways-back)) and whether the problem went away with it.

Read the files before you post them: logs and paths carry your account name and your Mac's name. One problem per
issue. If you found the cause, say how you know: what you ran and what it printed. The full list is in
[docs/TESTING-ON-HARDWARE.md](docs/TESTING-ON-HARDWARE.md), "What to attach to an issue".

A security problem is not a public issue: see [SECURITY.md](SECURITY.md).

## Assisted development and upstream

AI-assisted contributions are permitted in this fork. The submitter remains responsible for correctness,
provenance, licensing, review and testing; disclose significant AI assistance and do not invent hardware evidence.
This policy applies only to this fork. NullMoth's upstream contribution rules remain independent; do not submit
AI-generated contributions there contrary to its policy.

## Pull requests

- Target `fable`. The fork's `main` mirrors the original project and takes no changes.
- Understand the change and be able to explain its behavior and answer review questions. Review generated code
  and descriptions before submitting.
- One change per PR. A fix, a feature or a cleanup, not all three. Small PRs get merged; 2,000-line PRs sit.
- Run the host tests yourself (`python3 tools/run_all_tests.py`, from a full clone) and give the runner's last line.
  GitHub Actions is currently disabled on the fork, so nothing runs them for you.
- Say what you tested. Your card, macOS build, the commit, and what you did to check it. "Builds" is not tested.
- A change that replaces an older path is on by default and keeps one way back, written `name=0`
  ([DEVELOPING.md](DEVELOPING.md), "Rules for changes").
- Keep the description short: what was wrong, what you changed, how you tested it. Link the issue.
- Commit messages: `component: short summary` under 72 characters (`NVRMFB: fix 165 Hz mode missing`), then a
  plain explanation if one is needed.
- Comments in code explain why the code does what it does. They are not a log of when you found something.

Kernel changes (`kexts/`) require real hardware validation before being declared release-ready, because a bad kext
can stop the machine from starting. Experimental changes may be integrated into `fable` with explicit evidence
limits, a recovery path and host tests. Say which card and exact revision you booted, or state that no boot test ran.

## Where help is needed

The fork has run on one machine only: an RTX 5050 on macOS 26.7.3, with one 1920x1080 display at 100 Hz. What it
most needs is the same checks on anything else ([docs/STATUS.md](docs/STATUS.md), "Validation priorities"):

- macOS 15 (15.5 or later): not tried by the fork.
- Any other card: Turing (GTX 16, RTX 20), Ampere, Ada and other Blackwell cards, and laptops, among them those whose
  screen is wired to the Intel GPU. No card but the RTX 5050 has run a 3.0.x build.
- Other displays: ultrawide and high refresh rate modes, several displays, hot-plugging a monitor.
- The kits for macOS 10.7 to 14: never run on a card or on those releases.
- A first install on a Mac that never had the driver: it should need one restart and no **Allow** (not yet tried).
- The optional AMFI route ([docs/SIGNED-GRAPHICS-EXPERIMENT.md](docs/SIGNED-GRAPHICS-EXPERIMENT.md)) on another
  machine, and its open checks: repeated boots, sleep and wake, sustained GPU load, App Store and iMessage.

If you own a card or machine the fork has not run on, a working report (the "It works" form) is as useful as code.
[docs/TESTING-ON-HARDWARE.md](docs/TESTING-ON-HARDWARE.md) is the checklist.

## Building

See [docs/BUILDING.md](docs/BUILDING.md) for the build and [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md) for the
layout of the source. Setting up, the tests and the rules for changes are in [DEVELOPING.md](DEVELOPING.md).
