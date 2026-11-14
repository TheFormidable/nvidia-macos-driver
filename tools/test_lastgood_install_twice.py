#!/usr/bin/env python3
"""A second run of the installer before a start that lit the display keeps the first run's way back.

Every run of package/install.sh backs up the driver in place and writes the fallback job (step 6b) to restore that
backup. A second run before any start lit the display (the installer itself suggests running it again for DLSS or the
boost switch, and a newer build goes over an untried one) backed up the driver on trial, so the job's "way back" put
that same driver back. The installer now records the NVRM it installed and the fallback it chose
(/Library/NullMoth/trial-backup); a later run that finds nullmoth-nv-ok absent and that same NVRM in place keeps the
recorded fallback, with that backup's own macOS build.

The real install.sh runs through tools/primary_install_fixture.py with an NVRAM stand-in that keeps what is written
(the fixture's usual one reports every variable present, so nullmoth-nv-ok could never look absent). The generated job
is then run on two dark starts against the same stand-ins, and must put back the driver from before the trial."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

R = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer_fixture', R / 'tools/primary_install_fixture.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

V = '7C436110-AB2A-4BBB-A880-FE41995C9F82'


class Fixture(m.Install):
    """The fixture with real folders made by mktemp, and the stand-ins only a Mac does not need."""
    def setUp(self):
        super().setUp()
        self.env['FIXTURE_NVRAM'] = 'state'
        # the backup folder's name must be the installer's own (backup-*), so mktemp makes real folders: a template
        # under the fixture's root is used as it is, and any other (/var/tmp) is moved under the root
        self.tool('mktemp', '#!/bin/bash\nt="${@: -1}"\ncase "$t" in "$FIXTURE_ROOT"/*) ;; *) '
                  'mkdir -p "$FIXTURE_ROOT/vartmp"; t="$FIXTURE_ROOT/vartmp/${t##*/}";; esac\n'
                  'exec /usr/bin/mktemp -d "$t"\n')
        self.tool('sleep', '#!/bin/bash\nexit 0\n')
        # what only a Mac has: ditto copies a file or a folder's contents, ls -lde lists one folder with its ACL
        if not shutil.which('ditto'):
            self.tool('ditto', '#!/bin/bash\nif [ -d "$1" ]; then mkdir -p "$2" && cp -a "$1/." "$2/"; '
                      'else mkdir -p "$(dirname "$2")" && cp -a "$1" "$2"; fi\n')
        if subprocess.run(['ls', '-lde', '/'], capture_output=True).returncode != 0:
            self.tool('ls', '#!/bin/bash\nif [ "$1" = -lde ]; then shift; exec /bin/ls -ld "$@"; fi\n'
                      'exec /bin/ls "$@"\n')

    def tool(self, name, body):
        p = self.bin / name
        p.write_text(body)
        p.chmod(0o755)

    def nvram(self):
        f = self.root / 'nvram.json'
        return json.loads(f.read_text()) if f.exists() else {}

    def set_nvram(self, **kv):
        d = self.nvram() or {'boot-args': 'nvfb=1 nvaccel=1'}
        d.update(kv)
        (self.root / 'nvram.json').write_text(json.dumps(d))

    def install(self, **env):
        r = self.run_install(**env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r.bk = re.search(r'^== 4\. back up what is there now -> (\S+)$', r.stdout, re.M).group(1)
        r.line6b = re.search(r'^   ok  (.*)$', r.stdout.split('\n== 6b.', 1)[1], re.M).group(1)
        return r

    def nvrm(self, folder):
        return (Path(folder) / 'NVRM.kext/Contents/MacOS/NVRM').read_text()

    def scratch(self):
        """The installer's private folders left in /var/tmp (moved under the fixture's root by the mktemp stand-in)."""
        d = self.root / 'vartmp'
        return sorted(x.name for x in d.iterdir()) if d.exists() else []

    def job(self):
        p = self.root / 'Library/NullMoth/lastgood.sh'
        if not p.exists():
            return None
        text = p.read_text()
        bk = re.search(r"^BK='([^']*)'", text, re.M).group(1)
        osb = re.search(r"^OSB='([^']*)'", text, re.M).group(1)
        return text, bk, osb

    def dark_twice(self):
        """The next start after two that never lit the display: the driver is off and the job runs."""
        text = self.job()[0]
        root = str(self.root)
        # the job must name no system folder outside the fixture's root before it runs here
        for path in re.findall(r'(\S*)(/(?:Library|System|Users)/)', text):
            self.assertTrue(path[0].endswith(root), path)
        self.set_nvram(**{'nullmoth-nv-starts': '%02'})
        job = self.root / 'job.sh'
        mine = f'PATH={self.bin}:/usr/bin:/bin:/usr/sbin:/sbin'
        job.write_text(text.replace('PATH=/usr/bin:/bin:/usr/sbin:/sbin', mine, 1))
        # JOB_BASH: the shell the job runs under (macOS runs it with bash 3.2)
        j = subprocess.run([os.environ.get('JOB_BASH', '/bin/bash'), str(job)], env=self.env, capture_output=True,
                           text=True, timeout=120)
        log = self.root / 'Library/NullMoth/lastgood.log'
        return j, log.read_text() if log.exists() else ''

    def installed(self):
        return self.nvrm(self.root / 'Library/Extensions')


class InstallTwice(Fixture):
    def test_install_twice_keeps_the_first_backup(self):
        first = self.install()
        self.assertEqual(self.nvrm(first.bk), 'old-NVRM')
        second = self.install()
        self.assertNotEqual(second.bk, first.bk)
        self.assertEqual(self.nvrm(second.bk), 'new-NVRM')   # this run's backup holds the driver on trial
        text, bk, osb = self.job()
        self.assertEqual((bk, osb), (first.bk, '25G241'))
        self.assertIn('has not lit its display yet: its fallback stays (' + first.bk + ')', second.line6b)
        self.assertIn(second.bk + ' holds the driver on trial', second.line6b)
        j, log = self.dark_twice()
        self.assertIn('restoring the previous driver from ' + first.bk, log)
        self.assertEqual(self.installed(), 'old-NVRM', log + j.stderr)

    def test_three_runs_keep_the_first_backup(self):
        first = self.install()
        self.install()
        self.install()
        self.assertEqual(self.job()[1], first.bk)

    def test_control_stateless_nvram_hides_it(self):
        # the fixture's usual NVRAM stand-in answers every question: nv-ok looks present and the trial looks over
        del self.env['FIXTURE_NVRAM']
        first = self.install()
        second = self.install()
        self.assertEqual(self.job()[1], second.bk)
        self.assertNotEqual(second.bk, first.bk)

    def test_lit_display_in_between_takes_the_new_backup(self):
        self.install()
        self.set_nvram(**{'nullmoth-nv-ok': '1'})   # a start lit the display: the driver in place worked
        second = self.install()
        self.assertEqual(self.job()[1], second.bk)
        self.assertIn('previous driver kept as the fallback (' + second.bk + ')', second.line6b)

    def test_restored_in_between_takes_the_new_backup(self):
        self.install()
        j, log = self.dark_twice()
        self.assertEqual(self.installed(), 'old-NVRM', log + j.stderr)
        second = self.install()
        self.assertEqual(self.job()[1], second.bk)
        self.assertEqual(self.nvrm(second.bk), 'old-NVRM')

    def test_fresh_install_then_second_run_writes_no_job(self):
        for k in m.KEXTS:
            shutil.rmtree(self.root / f'Library/Extensions/{k}.kext')
        first = self.install()
        self.assertNotIn('No such file', first.stderr)   # no NVRM in place to read is not an error to show
        self.assertIn('no previous driver to fall back to (first install)', first.line6b)
        self.assertIsNone(self.job())
        second = self.install()
        self.assertIsNone(self.job())
        self.assertIn('has not lit its display yet and left no fallback: no fallback', second.line6b)

    def test_macos_update_in_between_keeps_the_first_backup_with_its_build(self):
        first = self.install()
        self.tool('sw_vers', '#!/bin/bash\nif [ "$1" = -buildVersion ]; then echo 25H999; else echo 15.8.1; fi\n')
        self.install()
        self.assertEqual(self.job()[1:], (first.bk, '25G241'))

    def test_kept_backup_gone_writes_no_job(self):
        first = self.install()
        shutil.rmtree(first.bk)
        second = self.install()
        self.assertIsNone(self.job())
        self.assertIn('its fallback ' + first.bk + ' is gone: no fallback', second.line6b)

    def test_record_outside_the_backups_is_not_used(self):
        self.install()
        rec = self.root / 'Library/NullMoth/trial-backup'
        other = self.root / 'Library/NullMoth/backup-x/../elsewhere'
        (self.root / 'Library/NullMoth/elsewhere/NVRM.kext').mkdir(parents=True)
        rec.write_text(rec.read_text().splitlines()[0] + '\n' + str(other) + '\n')
        self.install()
        self.assertIsNone(self.job())
        self.assertTrue((self.root / 'Library/NullMoth/elsewhere/NVRM.kext').is_dir())

    def test_last_lines_name_what_was_installed_before(self):
        # uninstall.sh restores nothing: the installer's last words say where the driver from before is kept
        first = self.install()
        self.assertNotIn('Undo:', first.stdout)
        self.assertTrue(first.stdout.rstrip().endswith(
            'What was installed before is kept in ' + first.bk + '. To go back to it, install its own package again;'
            ' sudo ./uninstall.sh\nremoves the driver.'), first.stdout[-400:])
        second = self.install()
        self.assertTrue(second.stdout.rstrip().endswith(
            'What was installed before the trial is kept in ' + first.bk + " (this run's backup " + second.bk +
            ' holds the driver on trial). To go back\nto it, install its own package again; sudo ./uninstall.sh removes'
            ' the driver.'), second.stdout[-400:])

    def test_last_line_of_a_first_install(self):
        for k in m.KEXTS:
            shutil.rmtree(self.root / f'Library/Extensions/{k}.kext')
        first = self.install()
        self.assertTrue(first.stdout.rstrip().endswith(
            '\nNothing was installed before; sudo ./uninstall.sh removes the driver.'), first.stdout[-400:])
        second = self.install()
        self.assertTrue(second.stdout.rstrip().endswith(
            '\nNo driver from before the trial is kept (see step 6b); sudo ./uninstall.sh removes the driver.'),
            second.stdout[-400:])

    def test_uninstall_removes_the_record(self):
        self.assertIn(' /Library/NullMoth/trial-backup ', (R / 'package/uninstall.sh').read_text())


class Rehearsal(Fixture):
    """A rehearsal (CHECK=1) that stops in step 3 used to leave /var/tmp/nullmoth.XXXX behind, with a copy of every
    third-party kext in /Library/Extensions. It is removed now, and only the one step 3 made: a T inherited from the
    caller's environment is never removed (the installer runs as root)."""
    def test_rehearsal_that_stops_in_step_3_leaves_nothing(self):
        r = self.run_install(CHECK='1', FAKE_PREFLIGHT_FAIL='1')
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('test kernel collection build failed', r.stdout + r.stderr)
        self.assertEqual(self.scratch(), [])

    def test_rehearsal_that_passes_leaves_nothing(self):
        r = self.run_install(CHECK='1')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.scratch(), [])

    def test_inherited_t_is_never_removed(self):
        mine = self.root / 'not-the-installers'
        (mine / 'keep').mkdir(parents=True)
        r = self.run_install(CHECK='1', T=str(mine), FAKE_OS_VERSION='14.7')   # stops in step 1, before step 3
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('built for macOS 15 and 26', r.stdout + r.stderr)
        self.assertTrue((mine / 'keep').is_dir())

    def test_real_install_that_stops_keeps_its_folder(self):
        r = self.run_install(FAKE_LIVE_FAIL='1')
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len([x for x in self.scratch() if x.startswith('nullmoth.')]), 1, self.scratch())


if __name__ == '__main__':
    unittest.main(verbosity=2)
