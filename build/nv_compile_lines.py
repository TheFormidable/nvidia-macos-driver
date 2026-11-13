#!/usr/bin/env python3
"""nv_compile_lines.py rm|kms: NVIDIA's "make -n" compile commands, read from standard input (the lines that hold " -c "),
written to standard output as the commands that compile the RM core (rm, build_rm620.sh) or NVKMS (kms, build_kms620.sh)
for the kexts. Both scripts call this one file, so the two lists cannot drift apart.

The scripts' own choices, the same since 2.9: cc becomes clang (and c++ clang++, for NVKMS's DisplayPort library),
-arch x86_64 -mmacosx-version-min=15.0 -mkernel -DNV_DARWIN -fcf-protection=branch go in front, and -mcmodel=kernel and
-fno-pic (the Linux kernel's code model) come out.

What this file adds: the same commands whichever Mac runs the build. NVIDIA's Makefiles ask the build computer's own
compiler which hardening options it takes (utils.mk's TEST_CC_ARG compiles /dev/null with $(CC) alone, with no -arch)
and add the ones it takes. An Apple-silicon Mac's clang answers for arm64 and refuses every x86-only option; an Intel
Mac's clang answers for x86_64 and takes some. The kexts are x86_64 on both, so the answers are about the wrong question.
On an Intel Mac NVKMS got -mfunction-return=thunk-extern: every return jumps to ___x86_return_thunk, a function that
only the Linux kernel defines, and NVRM.kext linked and then could not be loaded. Every released binary was built on
Apple silicon, so the rule is: whatever such a question added, the command an Apple-silicon Mac makes does not have,
and it comes out here; what both kinds of Mac take stays. HOST_PROBED below lists every question the two Makefiles
(and utils.mk, which both include) ask, with the answer of each kind of Mac. On a computer whose compiler took
something, a note on standard error names what was left out; an Apple-silicon Mac gets the same commands as before
and no note.

Exit 2, with "STOP: ..." on standard error and nothing on standard output, for a line from an unexpected compiler.
Written for the python3 that macOS has (3.9)."""
import re
import sys

PREFIX = "-arch x86_64 -mmacosx-version-min=15.0 -mkernel -DNV_DARWIN -fcf-protection=branch"
TOOLS = {"rm": {"cc": "clang"}, "kms": {"cc": "clang", "c++": "clang++"}}

# (components, whole option as a regular expression, what an Intel Mac's and an Apple-silicon Mac's clang answer, and
# why it goes). Answers are those of the probe as the Makefile runs it: "cc -c -x c /dev/null -Werror <option>".
HOST_PROBED = [
    ("rm kms", r"-fcf-protection=\S+",
     "Intel: full (it assembles endbr64, so not just return); Apple silicon: refused (no -fcf-protection for arm64). "
     "After this file's -fcf-protection=branch it would win. No difference was seen in the objects (macOS has no "
     "shadow stack for kexts), but it is not the release's command"),
    ("kms", r"-mfunction-return=\S+",
     "Intel: taken; Apple silicon: refused (x86 only). Every return becomes a jump to ___x86_return_thunk, which no "
     "macOS defines: the kext does not load"),
    ("rm kms", r"-mindirect-branch=\S+",
     "GCC's option, refused by clang for both; were a compiler to take it, indirect calls would go through "
     "___x86_indirect_thunk_*, which no macOS defines"),
    ("rm kms", r"-mindirect-branch-register",
     "GCC's option, refused by clang for both"),
    ("kms", r"-Wformat-overflow=\S+", "GCC's warning level, refused by clang for both"),
    ("kms", r"-Wformat-truncation=\S+", "GCC's warning level, refused by clang for both"),
    ("rm kms", r"-Wstack-usage=\S+", "GCC's warning, asked only when STACK_USAGE_WARNING is set; refused by clang"),
    ("kms", r"-ffunction-sections",
     "NVKMS adds it only when the build computer's ld takes GNU ld's --force-group-allocation: Apple's ld never does "
     "(a Linux computer's may; the RM has it always and keeps it)"),
    ("kms", r"-fdata-sections", "the same question as -ffunction-sections"),
]
# Asked and kept, because every clang takes it for either processor: -fno-jump-tables (both Makefiles).
# Asked and never reached for these commands: -mno-outline-atomics (only under TARGET_ARCH=aarch64).


def transform(component, lines):
    """(commands, notes) for the "make -n" lines of one component; raises ValueError for an unexpected compiler."""
    tools = TOOLS[component]
    rules = [(re.compile(r" (?:" + rx + r")(?= |$)"), rx, why) for comps, rx, why in HOST_PROBED if component in comps.split()]
    out, dropped = [], {}
    for line in lines:
        line = line.rstrip("\n")
        if " -c " not in line:
            continue
        cc, _, rest = line.partition(" ")
        if cc not in tools:
            raise ValueError(f"a {component} compile command starts with {cc!r}, not {' or '.join(sorted(tools))}: {line[:60]}")
        rest = " " + rest
        for rx, name, _why in rules:
            found = rx.findall(rest)
            if found:
                # counted once per command: the note says in how many commands an option was
                for f in set(f.strip() for f in found):
                    dropped[f] = dropped.get(f, 0) + 1
                rest = rx.sub("", rest)
        rest = rest[1:].replace(" -mcmodel=kernel", "").replace(" -fno-pic", "")
        out.append(tools[cc] + " " + PREFIX + " " + rest)
    why = {}
    for _comps, rx, text in HOST_PROBED:
        for opt in dropped:
            if re.fullmatch(rx, opt):
                why[opt] = text
    notes = [f"note: left out {opt} from {n} of {len(out)} {component} commands: NVIDIA's Makefile added it because this "
             f"computer's compiler (or linker) took it, and a build on an Apple-silicon Mac does not have it. "
             f"{why.get(opt, '')}" for opt, n in sorted(dropped.items())]
    return out, notes


def main(argv):
    if len(argv) != 2 or argv[1] not in TOOLS:
        sys.stderr.write("usage: nv_compile_lines.py rm|kms < make-n output > compile_cmds.sh\n")
        return 2
    try:
        out, notes = transform(argv[1], sys.stdin)
    except ValueError as e:
        sys.stderr.write(f"STOP: {e}\n")
        return 2
    for n in notes:
        sys.stderr.write(n + "\n")
    sys.stdout.write("".join(l + "\n" for l in out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
