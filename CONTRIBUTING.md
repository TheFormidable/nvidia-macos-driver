# Contributing

Bug reports and pull requests are welcome in TheFormidable's fork. Target `fable` for development changes and
consult [current status](docs/STATUS.md) before proposing a new feature.

## Reporting a bug

1. Press Send logs in the 1401 app (Windows or Mac). It gives you a report ID like `NM-ABCD1234`.
2. Open an issue with the bug form and put the report ID in it.
3. Describe what you see in a few sentences of your own. The logs are in the report, so don't paste them in.

One problem per issue. If you found the cause, say how you know: what you ran and what it printed.

## Assisted development and upstream

AI-assisted contributions are permitted in this fork. The submitter remains responsible for correctness,
provenance, licensing, review and testing; disclose significant AI assistance and do not invent hardware evidence.
This policy applies only to this fork. NullMoth's upstream contribution rules remain independent; do not submit
AI-generated contributions there contrary to its policy.

## Pull requests

- Understand the change and be able to explain its behavior and answer review questions. Review generated code
  and descriptions before submitting.
- One change per PR. A fix, a feature or a cleanup, not all three. Small PRs get merged; 2,000-line PRs sit.
- Say what you tested. Your card, macOS version, and what you did to check it. "Builds" is not tested.
- Keep the description short: what was wrong, what you changed, how you tested it. Link the issue.
- Commit messages: `component: short summary` under 72 characters (`NVRMFB: fix 165 Hz mode missing`), then a
  plain explanation if one is needed.
- Comments in code explain why the code does what it does. They are not a log of when you found something.
- Sign off every commit (`git commit -s`). It certifies you wrote the change or may submit it under this
  repository's licence (Developer Certificate of Origin, <https://developercertificate.org>).

Kernel changes (`kexts/`) require real hardware validation before being declared release-ready, because a bad kext
can stop the machine from starting. Experimental changes may be integrated into `fable` with explicit evidence
limits, a recovery path and host tests. Say which card and exact revision you booted, or state that no boot test ran.

## Where help is needed

See the issues labelled `help wanted`. The areas with the most open problems:

- Turing (GTX 16, RTX 20): flicker and black screens on cards with a small BAR.
- Display: ultrawide and high refresh rate modes, hot-plugging a monitor.
- Video: hardware decode in sandboxed processes (Chrome, Photos).
- Laptops: machines where the screen is wired to the Intel GPU.

If you own a card we haven't tested, a working report (the "It works" form) is as useful as code.

## Building

See [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md) for the layout of the source and how each part is built.
Setting up, the tests and the rules for changes are in [DEVELOPING.md](DEVELOPING.md).
