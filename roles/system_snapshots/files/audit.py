"""Read-only Fedora snapshot audit. No shell evaluation, mounts or repair actions.

JSON configuration arrives on stdin; only structured findings go to stdout.
The injectable filesystem root and runner are for offline regression tests.
"""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import stat
import subprocess
import sys


HOOKS = {
    'pre-mount/01-prepare-overlayfs.sh': 'prepare-overlayfs.sh',
    'mount/01-mount-overlayfs.sh': 'mount-overlayfs.sh',
    'pre-pivot/10-mount-overlayfs.sh': 'mount-overlayfs.sh',
}


def query(argv, timeout):
    """Queries only; never invoke a shell or return arbitrary command output."""
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, 'LC_ALL': 'C'})
    return result.returncode, result.stdout


class Audit:
    def __init__(self, config, root=Path('/'), runner=query, owner_uid=0):
        self.config = config
        self.expected = config['expected']
        self.root = Path(root)
        self.runner = runner
        self.owner_uid = owner_uid
        self.errors = []
        self.warnings = []
        self.evidence = {}
        self.image_cache = {}

    def error(self, message):
        if message not in self.errors:
            self.errors.append(message)

    def path(self, value):
        path = PurePosixPath(value)
        if not path.is_absolute() or '..' in path.parts or '$' in value or '`' in value:
            raise ValueError('unsupported or unsafe path')
        candidate = self.root / str(path).lstrip('/')
        # Fixtures cannot escape their temporary root through symlinks.
        if not candidate.resolve().is_relative_to(self.root.resolve()):
            raise ValueError('path escapes audit root')
        return candidate

    def read(self, value):
        try:
            return self.path(value).read_text()
        except (OSError, UnicodeError, ValueError):
            self.error(f'{value}: missing, unreadable or unsupported path; inspect permissions and restore the expected file.')
            return ''

    def command(self, argv):
        try:
            rc, out = self.runner(argv, self.config['timeout'])
        except (OSError, subprocess.TimeoutExpired):
            self.error(f'{argv[0]}: unavailable or timed out; verify the installed utility, privileges and timeout.')
            return None
        if rc != 0:
            self.error(f'{shlex.join(argv)}: query failed (rc={rc}); inspect this command read-only before repairing configuration.')
            return None
        return out

    def assignments(self, text, names, source):
        """Parse literal assignments only; never source privileged shell files."""
        values = {}
        for line in text.splitlines():
            if line.strip() and not line.lstrip().startswith('#') and not re.match(
                    r'^\s*(?:export\s+)?[A-Za-z_][A-Za-z_0-9]*\s*=', line):
                self.error(f'{source}: shell control flow/includes unsupported; review effective settings manually.')
            match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=(.*)$', line)
            if not match or match[1] not in names:
                continue
            key, raw = match.groups()
            if key in values:
                self.error(f'{source}: duplicate {key}; use one unambiguous literal assignment.')
            try:
                tokens = shlex.split(raw, comments=True)
                if len(tokens) > 1 or any(c in ''.join(tokens) for c in '$`\n;'):
                    raise ValueError()
                values[key] = tokens[0] if tokens else ''
            except ValueError:
                self.error(f'{source}: {key} is not a supported literal; inspect effective configuration manually.')
        return values

    def policy(self):
        e = self.expected
        listing = self.command(['snapper', '--no-dbus', '--csvout', 'list-configs'])
        if listing is not None:
            # Snapper CSV columns are config,subvolume; tolerate optional header/quotes.
            import csv
            rows = list(csv.reader(listing.splitlines()))
            if [e['snapper_config'], e['snapper_subvolume']] not in rows:
                self.error('Snapper: expected config/subvolume mapping missing; inspect snapper list-configs and the root config.')
        file = e['snapper_config_file']
        values = self.assignments(self.read(file), self.config['policy'], file)
        for key, expected in self.config['policy'].items():
            if values.get(key) != str(expected):
                self.error(f'{file}: {key} must be {expected!r}; review snapshot creation/retention policy (no changes made).')
        for unit in [e['grub_btrfs_service'], *self.config['timers']]:
            for verb, expected in [('is-active', 'active'), ('is-enabled', 'enabled')]:
                result = self.command(['systemctl', verb, unit])
                if result is not None and result.strip() != expected:
                    self.error(f'{unit}: expected {expected}; inspect systemctl {verb} {unit}.')
        self.evidence['checked_units'] = [e['grub_btrfs_service'], *self.config['timers']]

    def configuration(self):
        e = self.expected
        for value in [e['grub_config'], e['grub_btrfs_menu']]:
            self.command(['grub2-script-check', str(self.path(value))])
        main = self.read(e['grub_config'])
        if not re.search(r'^\s*configfile\s+["\']?(?:\$\{prefix\}|' +
                         re.escape(str(PurePosixPath(e['grub_btrfs_menu']).parent)) +
                         r')/grub-btrfs\.cfg["\']?\s*$', main, re.M):
            self.error('GRUB: main configuration does not load the expected grub-btrfs menu; inspect the generated include.')
        source = e['grub_btrfs_config']
        names = {*self.config['grub_settings'], 'GRUB_BTRFS_DISABLE', 'GRUB_BTRFS_ROOTFLAGS',
                 'GRUB_BTRFS_BOOT_DIRNAME', 'GRUB_BTRFS_GBTRFS_DIRNAME', 'GRUB_BTRFS_GBTRFS_SEARCH_DIRNAME'}
        values = self.assignments(self.read(source), names, source)
        for key, expected in self.config['grub_settings'].items():
            if values.get(key) != expected:
                self.error(f'{source}: {key} differs from expected policy; review the literal setting and generated menu.')
        if values.get('GRUB_BTRFS_DISABLE', 'false') != 'false':
            self.error('grub-btrfs is disabled or has an unsupported disable value; review configuration.')
        if re.search(r'(^|,)\s*subvol(?:id)?=', values.get('GRUB_BTRFS_ROOTFLAGS', '')):
            self.error('GRUB_BTRFS_ROOTFLAGS must not override the snapshot subvolume selector.')
        for key, expected in [('GRUB_BTRFS_BOOT_DIRNAME', e['boot_dir']),
                              ('GRUB_BTRFS_GBTRFS_DIRNAME', str(PurePosixPath(e['grub_btrfs_menu']).parent))]:
            if key in values and values[key] != expected:
                self.error(f'{key}: custom location needs explicit audit support; align expected paths before relying on this audit.')
        if 'GRUB_BTRFS_GBTRFS_SEARCH_DIRNAME' in values:
            self.error('Custom grub-btrfs search directory requires manual include-path validation.')
        source = e['dracut_config']
        text = self.read(source)
        if not re.search(r'^\s*add_dracutmodules\s*\+=\s*["\'][^"\']*\boverlayfs\b[^"\']*["\']\s*(?:#.*)?$', text, re.M):
            self.error(f'{source}: expected literal add_dracutmodules+=" overlayfs " directive missing.')
        # Detect resets/omits in local overrides without evaluating shell code.
        confs = [self.path('/etc/dracut.conf'), *self.path('/etc/dracut.conf.d').glob('*.conf')]
        for conf in confs:
            if not conf.exists():
                continue
            for line in conf.read_text().splitlines():
                if re.match(r'^\s*omit_dracutmodules\s*\+?=', line) and re.search(r'\boverlayfs\b', line.split('#')[0]):
                    self.error(f'{conf.name}: explicitly omits overlayfs; review conflicting Dracut settings.')
                if re.match(r'^\s*add_dracutmodules\s*=(?!=)', line):
                    self.error(f'{conf.name}: resets add_dracutmodules; effective ordering requires manual validation.')
        for name in self.config['dracut_files']:
            file = self.path(e['dracut_module_dir']) / name
            try:
                info = file.stat()
                expected = self.config['reference_files'][name].encode()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != self.owner_uid or info.st_mode & 0o022 or not info.st_mode & 0o111:
                    self.error(f'{file}: expected root-owned executable regular file without group/world write permission.')
                if hashlib.sha256(file.read_bytes()).digest() != hashlib.sha256(expected).digest():
                    self.error(f'{file}: differs from repository Dracut reference; review drift before any initramfs rebuild.')
            except OSError:
                self.error(f'{file}: missing/unreadable custom Dracut file; restore from the reviewed repository in a later change.')

    def image(self, image):
        if image in self.image_cache:
            return self.image_cache[image]
        modules = self.command(['lsinitrd', '-m', str(self.path(image))])
        if modules is not None and 'overlayfs' not in {line.strip() for line in modules.splitlines()}:
            self.error(f'{image}: custom overlayfs Dracut module absent; exclude this snapshot entry or rebuild only in a separately approved change.')
        listing = self.command(['lsinitrd', str(self.path(image))])
        versions = set()
        if listing is not None:
            # Ignore summaries/arguments; only actual archive member rows count.
            rows = [line.split() for line in listing.splitlines()
                    if re.match(r'^[bcdlps-][rwxstST-]{9}\s', line)]
            metadata = {row[-1].lstrip('./'): row for row in rows}
            members = list(metadata)
            versions = set(re.findall(r'(?:^|/)lib/modules/([^/\s]+)(?:/|$)', '\n'.join(members), re.M))
            if not any(re.search(r'/kernel/fs/overlayfs/overlay\.ko(?:\.(?:xz|zst|gz))?$', p) for p in members):
                self.error(f'{image}: overlay kernel driver absent from archive; inspect initramfs contents.')
            for suffix, reference in HOOKS.items():
                candidates = [p for p in members if p.endswith('/dracut/hooks/' + suffix)]
                if len(candidates) != 1:
                    self.error(f'{image}: expected embedded hook {suffix} missing or ambiguous.')
                    continue
                embedded = self.command(['lsinitrd', '-f', candidates[0], str(self.path(image))])
                row = metadata[candidates[0]]
                if embedded == '' and row[1].isdigit() and int(row[1]) > 1 and row[4] == '0':
                    # GNU cpio --to-stdout does not resolve zero-size hard-link
                    # members. Do not misreport these as stale hook content.
                    self.warnings.append(f'{image}: {suffix} is a zero-size cpio hard-link member; '
                                         'its target/content identity requires separate archive validation.')
                elif embedded is not None and embedded != self.config['reference_files'][reference]:
                    self.error(f'{image}: embedded {suffix} differs from repository; installed scripts alone do not verify this image.')
        if len(versions) != 1:
            self.error(f'{image}: could not identify one kernel release from initramfs members; compatibility requires inspection.')
        self.image_cache[image] = versions
        return versions

    def entries(self):
        e = self.expected
        mounts = self.command(['findmnt', '--json', '--output', 'TARGET,FSTYPE,FSROOT,UUID', '--target', '/'])
        if mounts is None:
            return
        try:
            mount = json.loads(mounts)['filesystems'][0]
            if mount['fstype'] != 'btrfs':
                raise ValueError()
            root_subvol = mount['fsroot'].strip('/')
            root_uuid = mount['uuid']
            if not root_subvol or not root_uuid:
                raise ValueError()
        except (ValueError, KeyError, IndexError, TypeError):
            self.error('findmnt: expected a Btrfs root with a named subvolume and UUID; audit cannot resolve snapshot paths safely.')
            return
        boot_mount = self.command(['findmnt', '--json', '--output', 'TARGET,FSTYPE,FSROOT,UUID',
                                   '--target', e['boot_dir']])
        try:
            boot = json.loads(boot_mount)['filesystems'][0]
            if boot['target'] != e['boot_dir'] or boot['fsroot'] != '/':
                raise ValueError()
        except (ValueError, TypeError, KeyError, IndexError):
            self.error('Expected separate /boot filesystem; boot artifact path resolution needs manual validation.')
            return
        menu = self.read(e['grub_btrfs_menu'])
        blocks = re.findall(r'^\s*menuentry[^\n]*\{\s*\n(.*?)^\s*\}', menu, re.M | re.S)
        parsed = 0
        count = 0
        for block in blocks:
            linux = re.findall(r'^\s*linux(?:efi)?\s+(.+)$', block, re.M)
            initrd = re.findall(r'^\s*initrd(?:efi)?\s+(.+)$', block, re.M)
            if not linux:
                continue
            parsed += len(linux)
            count += 1
            try:
                if len(linux) != 1 or len(initrd) != 1:
                    raise ValueError('expected one kernel and one initrd command')
                args = shlex.split(linux[0], comments=True)
                images = shlex.split(initrd[0], comments=True)
                if any('$' in x or '`' in x for x in args + images):
                    raise ValueError('dynamic GRUB expression needs manual validation')
                selectors = [a.split('=', 1)[1] for a in args[1:] if a.startswith('rootflags=')]
                if len(selectors) != 1:
                    raise ValueError('expected one rootflags argument')
                subvols = [v.split('=', 1)[1] for v in selectors[0].split(',') if v.startswith('subvol=')]
                if len(subvols) != 1 or 'subvolid=' in selectors[0]:
                    raise ValueError('ambiguous or unsupported subvolume selector')
                subvol = subvols[0].lstrip('/')
                if not subvol.startswith(root_subvol + '/'):
                    raise ValueError('entry targets current root or a subvolume outside the mounted root')
                snapshot = '/' + subvol[len(root_subvol) + 1:]
                if not re.fullmatch(r'/\.snapshots/[0-9]+/snapshot', snapshot):
                    raise ValueError('snapshot path is not the supported Snapper layout')
                label = f'{subvol} / {args[0]}'
                searches = re.findall(r'^\s*search\s+(.+)$', block, re.M)
                boot_devices = []
                for search in searches:
                    tokens = shlex.split(search, comments=True)
                    if '--fs-uuid' in tokens and '--set=root' in tokens:
                        boot_devices.append(tokens[-1])
                if not boot_devices or set(boot_devices) != {boot['uuid']}:
                    self.error(f'{label}: GRUB boot-device search differs from mounted /boot UUID; inspect stale or unsupported search commands.')
                if [a for a in args[1:] if a.startswith('root=')] != ['root=UUID=' + root_uuid]:
                    self.error(f'{label}: root device differs from mounted Btrfs UUID; inspect stale generated entries.')
                if [a for a in args[1:] if a.startswith('rd.live.overlay.overlayfs=')] != ['rd.live.overlay.overlayfs=1']:
                    self.error(f'{label}: missing/conflicting overlay activation parameter.')
                if not self.path(snapshot).is_dir():
                    self.error(f'{label}: snapshot target missing; inspect stale menu (no regeneration performed).')
                kernel = self.boot_path(args[0])
                if not self.path(kernel).is_file():
                    self.error(f'{label}: referenced kernel file missing.')
                image_versions = set()
                for item in images:
                    image = self.boot_path(item)
                    if not self.path(image).is_file():
                        self.error(f'{label}: referenced initramfs missing: {image}.')
                        continue
                    image_versions.update(self.image(image))
                # Fedora rescue file names encode machine-id, not the kernel release.
                name = PurePosixPath(kernel).name
                if name.startswith('vmlinuz-') and not name.startswith('vmlinuz-0-rescue-'):
                    version = name.removeprefix('vmlinuz-')
                    if image_versions != {version}:
                        self.error(f'{label}: kernel filename and initramfs kernel releases disagree.')
                elif name.startswith('vmlinuz-0-rescue-') and len(image_versions) == 1:
                    version = next(iter(image_versions))
                    self.warnings.append(f'{label}: rescue kernel release inferred from initramfs; kernel binary identity not proven.')
                else:
                    raise ValueError('kernel release cannot be resolved')
                module_dir = self.path(snapshot + '/usr/lib/modules/' + version)
                if not module_dir.is_dir() or not (module_dir / 'modules.dep').is_file():
                    self.error(f'{label}: snapshot lacks /usr/lib/modules/{version}/modules.dep; kernel/snapshot combination is incompatible or incomplete.')
            except (ValueError, IndexError) as exc:
                self.error(f'Snapshot menu entry {count}: {exc}; inspect generated entry manually.')
        total = len(re.findall(r'^\s*linux(?:efi)?\s+', menu, re.M))
        if not count or parsed != total:
            self.error('Snapshot menu empty or contains unparsed kernel commands; unsupported GRUB structure needs manual validation.')
        self.evidence['snapshot_boot_entries'] = count
        # The ordinary running-kernel image is required even if absent from the menu.
        running_image = e['boot_dir'] + '/initramfs-' + self.config['kernel'] + '.img'
        if self.image(running_image) != {self.config['kernel']}:
            self.error(f'{running_image}: embedded kernel release differs from the running kernel.')
        self.evidence['initramfs_images_inspected'] = sorted(self.image_cache)

    def boot_path(self, value):
        # Generated Fedora entries use paths relative to the separate /boot FS.
        if not value.startswith('/') or value.startswith('//') or '..' in PurePosixPath(value).parts:
            raise ValueError('unsupported boot artifact path')
        boot = self.expected['boot_dir'].rstrip('/')
        return value if value.startswith(boot + '/') else boot + value

    def run(self):
        for check in [self.policy, self.configuration, self.entries]:
            try:
                check()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.error(f'{check.__name__}: audit incomplete ({type(exc).__name__}); inspect configuration paths/schema and permissions.')
        self.warnings = sorted(set(self.warnings))
        return {'errors': self.errors, 'warnings': self.warnings, 'evidence': self.evidence,
                'limitations': [
                    'Static checks do not prove snapshot boot, OverlayFS hook ordering, SELinux behavior or persistent rollback.',
                    'Kernel filename and modules.dep presence are necessary checks, not proof of ABI or complete driver coverage.',
                    'Configuration files are parsed as literals, never executed; shell control flow and nonstandard layouts require manual review.',
                    'Root snapshots exclude home, separate boot/EFI and nested subvolume contents; no independent backup was tested.',
                ]}


if __name__ == '__main__':
    print(json.dumps(Audit(json.load(sys.stdin)).run()))
