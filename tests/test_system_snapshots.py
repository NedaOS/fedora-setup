"""Snapshot audit regression tests: isolated files and fake read-only queries only."""

import copy
import importlib.util
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

REPO = Path(__file__).resolve().parents[1]
ROLE = REPO / 'roles/system_snapshots'
spec = importlib.util.spec_from_file_location('snapshot_audit', ROLE / 'files/audit.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        defaults = yaml.safe_load((ROLE / 'defaults/main.yml').read_text())
        self.cfg = {key: copy.deepcopy(defaults['system_snapshots_' + name]) for key, name in [
            ('expected', 'expected'), ('policy', 'snapper_policy'), ('timers', 'timers'),
            ('dracut_files', 'dracut_files'), ('grub_settings', 'grub_btrfs_settings')]}
        self.cfg.update(kernel='7.3.3-test', timeout=10, reference_files={
            p.name: p.read_text() for p in (ROLE / 'files/dracut/69overlayfs').glob('*.sh')})
        e = self.cfg['expected']
        self.write(e['snapper_config_file'], '\n'.join(f'{k}="{v}"' for k, v in self.cfg['policy'].items()))
        self.write(e['grub_btrfs_config'], '\n'.join(f'{k}="{v}"' for k, v in self.cfg['grub_settings'].items()))
        self.write(e['dracut_config'], 'add_dracutmodules+=" overlayfs "\n')
        for name, content in self.cfg['reference_files'].items():
            p = self.write(e['dracut_module_dir'] + '/' + name, content)
            p.chmod(0o755)
        self.write(e['grub_config'], 'configfile "${prefix}/grub-btrfs.cfg"\n')
        self.write(e['grub_btrfs_menu'], self.entry())
        self.write('/boot/vmlinuz-7.3.3-test', '')
        self.write('/boot/initramfs-7.3.3-test.img', '')
        self.write('/.snapshots/1/snapshot/usr/lib/modules/7.3.3-test/modules.dep', 'kernel/test.ko:\n')
        self.responses = {}
        self.calls = []
        self.embedded = dict(self.cfg['reference_files'])
        self.versions = ['7.3.3-test']
        self.modules = 'base\noverlayfs\nbtrfs\n'
        self.omit_hook = None
        self.hardlink = False

    def write(self, path, value):
        p = self.root / path.lstrip('/')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value)
        return p

    def entry(self, kernel='7.3.3-test', subvol='root/.snapshots/1/snapshot', image=None):
        image = image or kernel
        return ("menuentry 'snapshot' {\n search --fs-uuid --set=root test-uuid\n"
                f' linux "/vmlinuz-{kernel}" root=UUID=test-uuid '
                f'rd.live.overlay.overlayfs=1 rootflags=compress=zstd:1,subvol="{subvol}"\n'
                f' initrd "/initramfs-{image}.img"\n}}\n')

    def runner(self, argv, timeout):
        self.calls.append(tuple(argv))
        if tuple(argv) in self.responses:
            response = self.responses[tuple(argv)]
            if isinstance(response, Exception):
                raise response
            return response
        if argv[0] == 'snapper':
            return 0, 'config,subvolume\nroot,/\n'
        if argv[0] == 'systemctl':
            return 0, 'active\n' if argv[1] == 'is-active' else 'enabled\n'
        if argv[0] == 'grub2-script-check':
            return 0, ''
        if argv[0] == 'findmnt':
            return 0, json.dumps({'filesystems': [{
                'target': '/' if argv[-1] == '/' else '/boot',
                'fstype': 'btrfs' if argv[-1] == '/' else 'ext4',
                'fsroot': '/root' if argv[-1] == '/' else '/', 'uuid': 'test-uuid'}]})
        if argv[0] == 'lsinitrd':
            if argv[1] == '-m':
                return 0, self.modules
            if argv[1] == '-f':
                if self.hardlink and '/mount/' in argv[2]:
                    return 0, ''
                name = 'prepare-overlayfs.sh' if 'prepare-' in argv[2] else 'mount-overlayfs.sh'
                return 0, self.embedded[name]
            paths = [f'usr/lib/modules/{v}/kernel/fs/overlayfs/overlay.ko.xz' for v in self.versions]
            paths += ['var/lib/dracut/hooks/' + h for h in probe.HOOKS if h != self.omit_hook]
            return 0, '\n'.join(
                ('-rwxr-xr-x 2 root root 0 Nov 13 2026 ' if self.hardlink and '/mount/' in p
                 else '-rwxr-xr-x 1 root root 99 Nov 13 2026 ') + p for p in paths)
        raise AssertionError('Unexpected command: ' + repr(argv))

    def audit(self):
        return probe.Audit(self.cfg, root=self.root, runner=self.runner, owner_uid=os.getuid()).run()

    def assertFinding(self, fragment):
        report = self.audit()
        self.assertTrue(any(fragment in e for e in report['errors']), report)
        return report

    def test_success_and_repeatability(self):
        first = self.audit()
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        second = self.audit()
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(first['errors'], [])
        self.assertEqual(first, second)
        self.assertEqual(before, after)
        self.assertTrue(all(c[0] in {'snapper', 'systemctl', 'grub2-script-check', 'findmnt', 'lsinitrd'} for c in self.calls))
        self.assertTrue(all(c[1] in {'is-active', 'is-enabled'} for c in self.calls if c[0] == 'systemctl'))

    def test_policy_drift_and_missing_config(self):
        self.write(self.cfg['expected']['snapper_config_file'], 'TIMELINE_CREATE="no"\n')
        self.assertFinding('TIMELINE_CREATE')
        self.responses[('snapper', '--no-dbus', '--csvout', 'list-configs')] = (0, 'root,/home\n')
        self.assertFinding('mapping missing')

    def test_each_unit_must_be_active_and_enabled(self):
        for unit in ['grub-btrfsd.service', *self.cfg['timers']]:
            for verb in ['is-active', 'is-enabled']:
                with self.subTest(unit=unit, verb=verb):
                    self.responses = {('systemctl', verb, unit): (1, 'disabled\n')}
                    self.assertFinding(unit)
        self.responses = {('systemctl', 'is-enabled', 'snapper-cleanup.timer'): (0, 'enabled-runtime\n')}
        self.assertFinding('expected enabled')

    def test_missing_utility_and_timeout_are_actionable(self):
        for exc in [FileNotFoundError(), subprocess.TimeoutExpired('snapper', 10)]:
            self.responses[('snapper', '--no-dbus', '--csvout', 'list-configs')] = exc
            self.assertFinding('unavailable or timed out')

    def test_grub_syntax_and_include_failure(self):
        menu = str(self.root / self.cfg['expected']['grub_btrfs_menu'].lstrip('/'))
        self.responses[('grub2-script-check', menu)] = (1, '')
        self.assertFinding('grub2-script-check')
        self.write(self.cfg['expected']['grub_config'], '# configfile "${prefix}/grub-btrfs.cfg"\n')
        self.assertFinding('does not load')

    def test_config_drift_duplicates_and_no_shell_execution(self):
        p = self.cfg['expected']['grub_btrfs_config']
        self.write(p, 'GRUB_BTRFS_SNAPSHOT_KERNEL_PARAMETERS="$(touch /do-not-create)"\n')
        self.assertFinding('not a supported literal')
        self.assertFalse(Path('/do-not-create').exists())
        self.write(p, 'GRUB_BTRFS_DISABLE="false"\nGRUB_BTRFS_DISABLE="true"\n')
        self.assertFinding('duplicate GRUB_BTRFS_DISABLE')
        self.write(p, 'source /somewhere\n')
        self.assertFinding('control flow/includes unsupported')

    def test_dracut_missing_drift_and_permissions(self):
        p = self.root / self.cfg['expected']['dracut_module_dir'].lstrip('/') / 'module-setup.sh'
        p.write_text('different')
        self.assertFinding('differs from repository Dracut')
        p.chmod(0o777)
        self.assertFinding('without group/world write')
        p.unlink()
        self.assertFinding('missing/unreadable custom Dracut')

    def test_dracut_override(self):
        self.write('/etc/dracut.conf.d/99-other.conf', 'omit_dracutmodules+=" overlayfs "\n')
        self.assertFinding('explicitly omits')

    def test_rescue_without_custom_module(self):
        self.write(self.cfg['expected']['grub_btrfs_menu'], self.entry(kernel='0-rescue-machine'))
        self.write('/boot/vmlinuz-0-rescue-machine', '')
        self.write('/boot/initramfs-0-rescue-machine.img', '')
        self.modules = 'base\nbtrfs\n'
        self.assertFinding('custom overlayfs Dracut module absent')

    def test_embedded_hook_missing_or_stale(self):
        self.omit_hook = 'pre-pivot/10-mount-overlayfs.sh'
        self.assertFinding('missing or ambiguous')
        self.omit_hook = None
        self.embedded['mount-overlayfs.sh'] = 'old hook\n'
        self.assertFinding('installed scripts alone')

    def test_cpio_hardlink_is_unverified_not_false_drift(self):
        self.hardlink = True
        report = self.audit()
        self.assertEqual(report['errors'], [])
        self.assertTrue(any('hard-link member' in w for w in report['warnings']))

    def test_missing_initramfs(self):
        (self.root / 'boot/initramfs-7.3.3-test.img').unlink()
        self.assertFinding('referenced initramfs missing')

    def test_invalid_mount_response(self):
        self.responses[('findmnt', '--json', '--output', 'TARGET,FSTYPE,FSROOT,UUID', '--target', '/')] = (0, '{}')
        self.assertFinding('expected a Btrfs root')

    def test_empty_menu(self):
        self.write(self.cfg['expected']['grub_btrfs_menu'], '')
        self.assertFinding('Snapshot menu empty')

    def test_missing_snapshot_modules(self):
        (self.root / '.snapshots/1/snapshot/usr/lib/modules/7.3.3-test/modules.dep').unlink()
        self.assertFinding('snapshot lacks /usr/lib/modules/7.3.3-test/modules.dep')

    def test_mismatched_initramfs(self):
        self.versions = ['7.2-old']
        self.assertFinding('kernel filename and initramfs kernel releases disagree')

    def test_stale_target_and_boot_artifact(self):
        self.write(self.cfg['expected']['grub_btrfs_menu'], self.entry(subvol='root/.snapshots/999/snapshot'))
        self.assertFinding('snapshot target missing')
        (self.root / 'boot/vmlinuz-7.3.3-test').unlink()
        self.assertFinding('kernel file missing')

    def test_current_root_and_unsupported_menu_fail_closed(self):
        self.write(self.cfg['expected']['grub_btrfs_menu'], self.entry(subvol='root'))
        self.assertFinding('targets current root')
        self.write(self.cfg['expected']['grub_btrfs_menu'], 'linux /vmlinuz-test\n')
        self.assertFinding('unparsed kernel commands')

    def test_wrong_boot_device_search(self):
        self.write(self.cfg['expected']['grub_btrfs_menu'], self.entry().replace('--set=root test-uuid', '--set=root wrong'))
        self.assertFinding('boot-device search differs')

    def test_conflicting_root_selector(self):
        text = self.entry().replace('rootflags=compress', 'root=UUID=other rootflags=compress')
        self.write(self.cfg['expected']['grub_btrfs_menu'], text)
        self.assertFinding('root device differs')

    def test_image_queries_are_cached(self):
        self.write(self.cfg['expected']['grub_btrfs_menu'], self.entry() * 2)
        self.assertEqual(self.audit()['errors'], [])
        self.assertEqual(sum(c[:2] == ('lsinitrd', '-m') for c in self.calls), 1)

    def test_path_traversal_rejected(self):
        self.write(self.cfg['expected']['grub_btrfs_menu'], self.entry().replace('/vmlinuz-7.3.3-test', '/../vmlinuz-7.3.3-test'))
        self.assertFinding('unsupported boot artifact path')



@unittest.skipUnless(shutil.which('ansible-playbook'), 'ansible-playbook unavailable')
class AnsibleIntegrationTests(unittest.TestCase):
    def run_role(self, errors, check=False, broken=False, audit_only=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'fake-python'
            fake.write_text('#!/usr/bin/python3\nimport json,sys\n'
                            'config=json.load(sys.stdin)\n'
                            'assert config["reference_files"]["mount-overlayfs.sh"].endswith("\\n")\n'
                            + ('sys.exit(7)\n' if broken else
                               'print(' + repr(json.dumps({'errors': errors, 'warnings': [], 'evidence': {}})) + ')\n'))
            fake.chmod(0o755)
            play = root / 'audit.yml'
            play.write_text(yaml.safe_dump([{
                'name': 'Offline role orchestration', 'hosts': 'localhost', 'connection': 'local',
                'gather_facts': False, 'vars': {
                    'ansible_facts': {'distribution': 'Fedora', 'distribution_major_version': '44', 'kernel': 'test'},
                    'system_snapshots_python': str(fake), 'system_snapshots_audit_only': audit_only},
                'roles': [str(ROLE)]}]))
            result = subprocess.run(
                ['ansible-playbook', '-i', 'localhost,', str(play), *(['--check'] if check else [])],
                cwd=root, capture_output=True, text=True, timeout=60,
                env={**os.environ, 'ANSIBLE_CONFIG': str(REPO / 'ansible.cfg'),
                     'ANSIBLE_LOCAL_TEMP': str(root / 'controller-tmp'),
                     'ANSIBLE_REMOTE_TEMP': str(root / 'target-tmp'),
                     'ANSIBLE_NOCOLOR': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
            return result.returncode, result.stdout + result.stderr

    def test_success_twice_and_check_mode_all_report_zero_changes(self):
        for check in [False, False, True]:
            rc, out = self.run_role([], check=check)
            self.assertEqual(rc, 0, out)
            self.assertRegex(out, r'changed=0\s+unreachable=0\s+failed=0')

    def test_findings_fail_including_check_mode(self):
        for check in [False, True]:
            rc, out = self.run_role(['fixture boot mismatch'], check=check)
            self.assertNotEqual(rc, 0)
            self.assertIn('fixture boot mismatch', out)
            self.assertIn('changed=0', out)

    def test_probe_error_is_actionable(self):
        rc, out = self.run_role([], broken=True)
        self.assertNotEqual(rc, 0)
        self.assertIn('Snapshot audit could not complete', out)

    def test_audit_only_guard(self):
        rc, out = self.run_role([], audit_only=False)
        self.assertNotEqual(rc, 0)
        self.assertIn('only supports read-only auditing', out)

if __name__ == '__main__':
    unittest.main()
