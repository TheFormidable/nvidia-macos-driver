#!/usr/bin/env python3
# Compiles and runs tools/test_mem_index.c against plugin/nvmtl_mem.h, then proves its arms are load-bearing: the test is
# rebuilt against copies of the header with the index broken one way at a time, and each such build must FAIL.
import pathlib, shutil, subprocess, tempfile

root = pathlib.Path(__file__).resolve().parent.parent
test_c = root / 'tools' / 'test_mem_index.c'
plugin = root / 'plugin'

def build_run(include_dir):
    with tempfile.TemporaryDirectory() as bd:
        exe = pathlib.Path(bd) / 'tmi'
        subprocess.run(['cc', '-O2', '-I', str(include_dir), str(test_c), '-o', str(exe)], check=True)
        return subprocess.run([str(exe)], stdout=subprocess.PIPE, text=True).returncode

assert build_run(plugin) == 0, 'the real header must pass'

WALK = 'i = (c = s->e[i - 1].chain) < i ? c : 0) if (s->e[i - 1].mem == mem) return &s->e[i - 1];'
MUTANTS = [
    ('an index that returns the oldest match', WALK,
     'i = (c = s->e[i - 1].chain) < i ? c : 0) if (s->e[i - 1].mem == mem) { const nvmtl_mm_entry *o = &s->e[i - 1]; for (uint32_t j = s->e[i - 1].chain; j; j = s->e[j - 1].chain) if (s->e[j - 1].mem == mem) o = &s->e[j - 1]; return (nvmtl_mm_entry *)o; }'),
    ('an append that does not link its entry', 'e->chain = s->heads[b]; s->heads[b] = s->n;', 'e->chain = 0; (void)b;'),
    ('a walk without the older-link stop', WALK, 'i = s->e[i - 1].chain) if (s->e[i - 1].mem == mem) return &s->e[i - 1];'),
]
for name, old, new in MUTANTS:
    with tempfile.TemporaryDirectory() as d:
        shutil.copy(plugin / 'nvmtl_mem.h', d)
        p = pathlib.Path(d) / 'nvmtl_mem.h'; text = p.read_text()
        assert text.count(old) == 1, f'mutant "{name}": the guarded code moved'
        p.write_text(text.replace(old, new))
        assert build_run(d) != 0, f'mutant "{name}" must FAIL the test'
print('PASS: the memory index answers as the backward scan did; each broken index fails the test')
