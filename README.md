# fedora-setup

Ansible setup for a personal Fedora 44 + Hyprland system.

## Reinstall

Supported platform: **Fedora 44 only**. `bootstrap.sh` exits with an error on any other distribution or Fedora release before changing anything.

```bash
curl -fsSL https://raw.githubusercontent.com/NedaOS/fedora-setup/main/bootstrap.sh | bash
```

`bootstrap.sh` installs `ansible-core`, installs the collections from `requirements.yml`,
then runs `playbooks/default-install.yml` (system) and `playbooks/manual-install.yml` (user level).

## Network failures

Every install group is independent. If something cannot be downloaded or installed (for example
no network), it is recorded and the remaining steps still run. At the end the playbook (and
`bootstrap.sh`) lists what did not complete and exits non-zero. Re-run to retry; completed steps
are skipped. A checksum mismatch for OnionHop is reported as a SECURITY failure and the archive is
not installed. `bootstrap.sh` still stops early only if the system is not Fedora 44 or it cannot install
git/ansible-core (nothing can run without them).

## Verify

```bash
ansible-playbook -i localhost, playbooks/verify.yml
```

## Layout

| Path | Purpose |
|---|---|
| `vars/packages.yml` | Package lists by source: Fedora, COPR, RPM Fusion, hardware, (vendor packages live in `repositories.yml`) |
| `vars/repositories.yml` | Third-party repositories (trust boundaries) and supported Fedora releases |
| `vars/manually.yml` | Tools installed outside RPM (versions, checksums) |

## Third-party trust boundaries

| Source | Provides | Why |
|---|---|---|
| COPR `lionheartp/Hyprland` | hyprland, hyprland-guiutils | No candidate in Fedora 44 repos (checked with `dnf repoquery`) |
| COPR `lihaohong/yazi` | yazi | Same |
| RPM Fusion free/nonfree | ffmpeg, kmod-v4l2loopback | Codecs / kernel module |
| Brave, Microsoft (VS Code) | brave-browser, code | Vendor repositories |

Re-check periodically: `dnf repoquery --qf '%{name} %{version} %{repoid}' hyprland yazi`.

Run playbooks

Run commands from the repository root.

## Checks
```bash
ansible-playbook -i localhost, -c local playbooks/<file name>.yml
```

```bash
ansible-lint playbooks vars
shellcheck bootstrap.sh
```
