# Fedora 44 snapshot audit

This is a read-only verification role for the existing Fedora Btrfs/Snapper,
GRUB/BLS, grub-btrfs and custom Dracut `69overlayfs` architecture. It does not
install tools, manage retention, repair files, regenerate menus/initramfs, mount
filesystems, create/delete snapshots, change units, or perform rollback.

## Run

From the repository:

```bash
ansible-playbook -i localhost, playbooks/system-snapshots.yml --syntax-check
ansible-playbook -i localhost, playbooks/system-snapshots.yml --check --ask-become-pass
```

The playbook also accepts an absolute path from another working directory. It
resolves its role relative to `playbook_dir`, independently of `ansible.cfg`.
Run as the desktop user with become privileges, or as root. Normal mode and
check mode both execute the same read-only queries and report `changed=0`.
Check mode deliberately does not skip the probe.

Exit status zero means the supported static checks passed. Nonzero can mean
real host drift/incompatible entries, rather than a broken playbook. Read the
structured `system_snapshots_report` before the final assertion. Probe startup
failures have a separate assertion. Warnings and limitations remain relevant
when there are no errors.

## Implementation and expectations

- `roles/system_snapshots/defaults/main.yml` contains the Fedora paths, expected
  Snapper policy, required timer names, Dracut file names and grub-btrfs settings.
- `tasks/main.yml` enforces audit-only Fedora 44 execution, reads repository
  references, executes the probe, reports findings and fails on critical errors.
- `files/audit.py` uses Python's standard library. Ansible passes its source
  directly to Python and its configuration/reference scripts through JSON stdin;
  no helper is installed on the workstation.
- `files/dracut/69overlayfs/` remains the unchanged reference module.
- `playbooks/system-snapshots.yml` is the standalone privileged entry point.

Override role defaults through ordinary Ansible variables. Dictionary overrides
replace the dictionary under Ansible's default merge behavior: supply the full
mapping or explicitly combine it. Keep `system_snapshots_audit_only: true`.
`system_snapshots_python` defaults to `/usr/bin/python3`, and each external query
has a configurable timeout (`system_snapshots_command_timeout`, 60 seconds).

The expected policy preserves the audited system: root Snapper config for `/`,
Btrfs, timeline/number/empty-pair cleanup, five hourly/seven daily/four weekly/
three monthly retention, 20 normal/five important numbered snapshots, and the
existing minimum ages. These values are checks, not configuration writes.
No quota enforcement, independent backup or transaction snapshot hook is implied.

Required existing tools: Python 3, Snapper, systemctl, findmnt, grub2-script-check
and lsinitrd. Ansible does not install missing prerequisites; it reports them.
The probe uses argument arrays, never a shell, and never sources configuration.
Snapper queries use `--no-dbus` to avoid activating snapperd.
Command stderr is not copied into the report. Configuration parsing supports
literal assignments; unsupported control flow/includes, duplicate expected keys
and dynamic expected values fail for manual review.

## Critical checks

1. Snapper CSV query contains the expected config/subvolume mapping and the
   configuration file matches the declared policy.
2. grub-btrfs daemon and both Snapper timers are active and persistently enabled;
   transient enablement, masked/missing units and failed queries are failures.
3. Main GRUB and generated grub-btrfs menus pass syntax checks; main configuration
   includes the expected menu. Expected grub-btrfs parameters and directory/checker
   settings match, integration is not disabled, and rootflags do not override the
   generated subvolume. Unsupported custom menu locations fail for review.
4. Installed Dracut scripts match repository bytes and are root-owned executable
   regular files without group/world write permission. The expected local Dracut
   directive includes overlayfs. Local omit directives and add-list resets are
   flagged. Effective vendor configuration ordering is not fully emulated.
5. Every generated kernel entry is parsed. Empty/unparsed menus, current-root
   pseudo-snapshot entries, dynamic paths, ambiguous selectors, missing snapshot
   targets, wrong root UUIDs and incorrect overlay activation are failures.
6. The mounted root must be a named Btrfs subvolume; `/boot` must be separately
   mounted with filesystem root `/`. GRUB boot-device searches must match that
   boot filesystem's UUID. Unsupported layouts fail rather than guessing paths.
7. Referenced kernels/initramfs must exist. Each unique image is inspected once
   per run for the custom `overlayfs` module, overlay kernel driver, required
   pre-mount/mount/pre-pivot hooks, and embedded kernel release. Extractable hook
   contents must match repository references. The running kernel image is checked
   even when it is absent from the generated menu.
8. Regular Fedora kernel filenames must agree with the image's module release.
   Each snapshot must contain that release's `/usr/lib/modules/.../modules.dep`.
   Rescue names encode a machine ID: their release is inferred from archive
   contents and explicitly reported as an inference, not binary identity proof.

Fedora's cpio archives can represent the mount hook as a zero-size hard-link
member. `lsinitrd -f` returns empty content for that member. The audit verifies
its listed presence and reports a warning that target/content identity is not
independently verified; it does not falsely call this content drift. Nonempty
extractable companion hooks are still compared. This warning requires separate
archive-level validation before claiming complete embedded-hook identity.

The parser targets the current generated Fedora grub-btrfs structure: menuentry
blocks with literal `linux`/`linuxefi` and `initrd`/`initrdefi` commands and Snapper
paths under the mounted root's `.snapshots/<number>/snapshot`. It is not a GRUB
interpreter, nor proof of firmware selection, effective GRUB prefix/search state,
kernel binary identity, modules.dep integrity or full driver/ABI coverage.

## Automated regression tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_system_snapshots.py -v
```

Tests require the already available PyYAML package. Ansible orchestration tests
run when `ansible-playbook` is available; otherwise unittest marks them skipped.
They use temporary files, fake query responses and a fake probe executable.
They never invoke live Snapper/systemctl/GRUB/initramfs operations.

Coverage includes successful/repeated audits, file-byte/mtime preservation,
retention drift, missing utilities/timeouts, inactive/disabled units, malformed
configuration, syntax/include failures, boot-device mismatch, missing/stale
Dracut scripts and hooks, cpio hard links, incompatible rescue images, missing
snapshot modules/images, stale targets, unsafe paths, malformed/empty menus,
image-query caching, and Ansible success/failure/check-mode/audit-only guards.

## Verification record and unresolved recovery risks

The initial live check-mode run completed all inspection tasks with `changed=0`
and failed at the final validation assertion as intended. It found unsafe rescue
entries, current-root pseudo-snapshot entries, and older snapshots without module
trees for some currently offered kernels. The corrected standalone normal-mode run from `/tmp` also reached the final
assertion with `changed=0`, exit status 2. It inspected 76 generated kernel
entries and four unique initramfs images, reporting 30 errors and 21 warnings:

- Four image-level errors: rescue image lacks the custom module and three hooks.
- Eight snapshot/rescue pairs lack the inferred rescue kernel's modules.dep.
- Fourteen regular-kernel/snapshot pairs lack the selected kernel's modules.dep.
- Four entries target the current root rather than a Snapper recovery point.
- Three cpio hard-link warnings and eighteen rescue-release inference warnings.

Snapper policy, required unit states, GRUB syntax/include/search UUIDs, installed
reference scripts and extractable regular-image hooks passed those checks.
The 26 offline regression tests and Ansible syntax/whitespace checks passed.
Counts describe the inspected menu and may change as normal timers operate.
The audit does not freeze timers or guarantee an atomic view; rerun if artifacts
change concurrently.

No repair is performed when findings are present. In a later approved change,
review menu-generation filters and kernel compatibility rather than editing
snapshots or boot artifacts merely to make an audit green.

Controlled testing is still required for:

- OverlayFS hook ordering, lower-root read-only protection, upper/work filesystem
  behavior and persistence across pivot/reboot.
- Hard-link target identity in the embedded mount hook, kernel binary identity,
  driver availability and SELinux behavior during an actual snapshot boot.
- Separate `/home`, `/boot` and EFI writes during a temporary overlay session.
- Persistent offline root restoration, preserving the nested snapshot store and
  matching restored `/usr/lib/modules` with separately stored boot artifacts.
- Independent backup restoration. Root snapshots exclude separate home/boot/EFI
  and nested subvolume contents; same-device snapshots do not cover disk failure.

Normal boot and fstab explicitly choose `subvol=root`; changing only Btrfs's
default subvolume is not a validated rollback procedure. Do not infer bootability
or persistent recovery from a zero-error static audit.
