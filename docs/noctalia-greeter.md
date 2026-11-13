# Noctalia Greeter on Fedora 44

This reproduces the Fedora 44 installation inspected on 2026-11-13. It keeps
greetd on VT 1, `/usr/bin/noctalia-greeter-session`, the `greetd` account,
and the existing quiet Hyprland session. The role never starts, stops, or
restarts a display manager. Boot enablement takes effect on the next boot.

## Package sources and release changes

`default-install.yml` installs Fedora's `greetd`, `greetd-selinux`, and `noctalia`;
Terra supplies `noctalia-greeter` through the existing
vendor-repository tasks. Hyprland remains in the existing COPR package group.
The inspected versions were Noctalia 5.4.1, Greeter 1.7.0, greetd 0.10.3,
and Hyprland 0.57.2. Package versions are observations, not version pins.

The role deliberately supports Fedora 44 only. Before adding another release:

```bash
dnf repoquery --qf '%{name} %{version} %{repoid}' greetd noctalia noctalia-greeter hyprland
rpm -qf /usr/bin/noctalia-greeter-session /usr/bin/start-hyprland /usr/bin/kdeconnectd
rpm -ql noctalia-greeter
systemctl cat greetd.service sddm.service
```

Review package names, the Terra signing key and repository URL, the config
format and installed helper policy, and the paths in the role defaults.
The role checks the installed executable layout before writing login files.
An existing display-manager alias must point to SDDM or greetd; an unexpected
manager is rejected before changing login files.
It leaves Fedora's PAM configuration and SELinux policy under package control.

## Configuration and mutable state

Noctalia's [configuration guide](https://docs.noctalia.dev/greeter/configuration/)
defines `greeter.toml` as administrator configuration and `sync.toml` as mutable
Sync/UI state. Values declared in `greeter.toml` take precedence.

The role manages greetd's configuration, the quiet wrapper, the quiet desktop
entry, and the administrator greeter settings. Changes to managed files keep
Ansible timestamped backups. The state directory is `greetd:greetd`, mode `0750`;
its contents must remain writable by the greeter. Ownership is not recursively
reset on each run.

The administrator greeter settings preserve `session.default = "Hyprland (Quiet)"`
and the observed output: `eDP-1`, 1920×1080, 60 Hz, scale 1.6. They do not pin
the palette or wallpaper, so future Noctalia Sync operations remain effective.
If `sync.toml` is absent, the role seeds the captured dark **Synced** palette
and both wallpaper paths before installing the initial `sync.toml`. A single
repository image supplies both identical wallpaper files. If `sync.toml`
already exists, the role skips the entire seed block, preserving its content,
permissions, last-used session/scheme, and wallpaper images. It does not compare
runtime colors against the original snapshot during verification.

`~/.config/noctalia/greeter-sync.toml` supplies the observed defaults:
`shell.polkit_agent = true` and `shell.greeter_sync.auto_sync = true`.
It also preserves the active wallpaper-based dark theme (`m3-tonal-spot`) and
the `Pictures/Wallpapers/river.jpg` image already supplied by chezmoi. The image
was verified byte for byte against both captured greeter wallpaper files.
It does not replace chezmoi's `config.toml`. Noctalia's app-managed
`~/.local/state/noctalia/settings.toml` takes precedence, so GUI changes remain
effective; the role does not overwrite that file.

The exported Polkit rules contained no Noctalia-specific passwordless allow
rule. The role retains the package's administrator-authenticated Sync action;
it does not add a passwordless rule or invoke the appearance helper at install
time. Auto-Sync uses Noctalia's existing Polkit agent and authorization flow.
See upstream's [Sync guide](https://docs.noctalia.dev/greeter/sync/).

Chezmoi remains responsible for `~/.config/hypr/`, including
`config/autostart.lua` and `config/monitors.lua`. The inspected autostart starts
Noctalia through `scripts/start-noctalia.sh` and invokes `/usr/bin/kdeconnectd`.
The monitor rule uses preferred mode and scale 1.6; `hyprctl monitors -j` confirmed
`eDP-1`, 1920×1080 at 60 Hz. The Ansible role does not replace these Lua files.
The existing bootstrap applies `NedaOS/live-dotfiles` after system configuration.

## Review and install

Run from the repository root as the desktop user. Ansible uses sudo for system
configuration. Syntax checking and the offline tests need no sudo:

```bash
ansible-playbook -i localhost, playbooks/default-install.yml --syntax-check
ansible-playbook -i localhost, playbooks/noctalia-greeter.yml --syntax-check
ansible-playbook -i localhost, playbooks/verify.yml --syntax-check
python3 -m unittest discover -s tests -v
```

Preview configuration on a machine with the prerequisite packages installed:

```bash
ansible-playbook -i localhost, playbooks/noctalia-greeter.yml --check --diff --ask-become-pass
```

A clean machine needs the package install first: check mode cannot make missing
executables/accounts available for subsequent configuration checks. Inspect the
package transaction and repository changes separately. After approving the diff,
install through the normal bootstrap, or run:

```bash
ansible-playbook -i localhost, playbooks/default-install.yml --diff --ask-become-pass
```

To retry only greeter configuration after packages are installed:

```bash
ansible-playbook -i localhost, playbooks/noctalia-greeter.yml --diff --ask-become-pass
```

When configuring a different login account, pass
`-e noctalia_greeter_desktop_user=USERNAME`. This role only configures the boot
selection: an approved installation does not interrupt an existing SDDM or greetd
session. Reboot separately when ready to exercise the graphical login.

## Expected diffs

| Location | Existing working installation | Clean Fedora installation |
|---|---|---|
| `/etc/greetd/config.toml` | No content change | Greeter command, `greetd` user, VT 1 |
| Quiet wrapper and `.desktop` | No content change | Create the inspected files |
| `greeter.toml` | No content change | Quiet session default and observed output |
| `sync.toml`, `wallpaper*.jpg` | No writes when Sync state exists | Seed the captured appearance once |
| `~/.config/noctalia/greeter-sync.toml` | New defaults matching current effective values | Auto-Sync, Polkit agent, active theme/wallpaper defaults |
| Boot selection | greetd stays enabled, SDDM stays disabled | Disable SDDM if installed, enable greetd, select graphical target |
| Terra repository | May normalize repository description/key URL | Add the vendor repository and import its signing key |
| Hyprland Lua / KDE Connect autostart | No role changes | Restored by the existing chezmoi bootstrap stage |

SDDM is removed from the desired package list, not uninstalled. Existing SDDM
packages remain available for recovery. The default installer still reports
independent package/repository failures and continues other groups; a greeter
failure is recorded and makes the final run fail. Re-run after resolving it.

## Verification

The repository verification playbook checks packages, greetd/SDDM enablement,
managed-file drift, state-directory ownership/mode, session discovery, the boot
target, and whether `greetd` can parse/write its TOML files and read referenced
wallpapers. It reads protected files with sudo but does not modify them:

```bash
ansible-playbook -i localhost, playbooks/verify.yml --ask-become-pass
```

```bash
systemctl is-enabled greetd.service
systemctl is-enabled sddm.service
readlink -f /etc/systemd/system/display-manager.service
systemctl get-default
stat -c '%a %U:%G %n' /var/lib/noctalia-greeter
noctalia-greeter sessions
sh -n /usr/local/bin/hyprland-quiet-session
noctalia config validate
hyprctl monitors -j
pgrep -ax kdeconnectd
```

Expected: greetd enabled, SDDM disabled (or absent), the display-manager alias
points to greetd, `graphical.target` is the default, the state directory is
`750 greetd:greetd`, and `Hyprland (Quiet)` appears in the session list.
After a deliberate reboot, check the selected session, wallpaper/palette,
1920×1080 at 60 Hz and scale 1.6, and KDE Connect. Startup output should appear
only in `~/.local/state/hyprland-startup.log`. Noctalia's existing keyviz widget
produced a configuration warning during inspection; it is unrelated to login.
The desktop entry retains its working `DesktopNames=Hyprland` session key;
the role validates it with Python's INI parser because the generic
`desktop-file-validate` utility rejects that session-specific key.

To inspect failures without changing anything:

```bash
sudo journalctl -b -u greetd.service --no-pager
tail -n 100 ~/.local/state/hyprland-startup.log
sudo ls -la /var/lib/noctalia-greeter
sudo cat /etc/greetd/config.toml
sudo cat /var/lib/noctalia-greeter/greeter.toml
```

## TTY recovery

The following commands are for recovery after an intentional installation or
reboot. Do not run stop/start commands from the graphical session you want to
keep: stopping its display manager ends that session.

1. Press **Ctrl+Alt+F3** and log in as the normal desktop user. If no TTY is
   available, edit the Fedora entry in GRUB (`e`), append
   `systemd.unit=multi-user.target` to the kernel command line, and boot with
   **Ctrl+x**. This boot-only option leaves graphical boot configured normally.
2. Inspect `sudo journalctl -b -u greetd.service --no-pager`. Keep this TTY open.
   To try the installed desktop without a greeter:

   ```bash
   sudo systemctl stop greetd.service
   /usr/bin/start-hyprland
   ```

   Run Hyprland as your normal user. If it works, the desktop is intact and the
   login configuration can be repaired from the TTY. Exit Hyprland to return
   to the TTY before continuing.
3. Repair the managed files using `playbooks/noctalia-greeter.yml` as above, or
   restore the specific timestamped Ansible backup of the broken file:

   ```bash
   sudo ls -lt /etc/greetd/config.toml*
   # Replace BACKUP_PATH with the inspected backup filename.
   sudo cp -a BACKUP_PATH /etc/greetd/config.toml
   sudo restorecon -v /etc/greetd/config.toml
   ```

   Preserve `sync.toml` and wallpaper files while repairing administrator
   configuration. Do not delete the state directory or recursively chmod it.
4. To return to SDDM instead, ensure it is installed and select it for next boot:

   ```bash
   sudo dnf install sddm
   sudo systemctl disable greetd.service
   sudo systemctl enable --force sddm.service
   readlink -f /etc/systemd/system/display-manager.service
   sudo reboot
   ```

   If this boot used `multi-user.target`, the one-time GRUB argument disappears
   on reboot. To return to greetd later:

   ```bash
   sudo systemctl disable sddm.service
   sudo systemctl enable --force greetd.service
   sudo reboot
   ```

The offline recovery test uses `systemctl --root` with Fedora's actual units to
rehearse SDDM → greetd → SDDM → greetd and checks the alias after every handoff.
It is skipped if SDDM's unit is absent. The seed test runs actual Ansible tasks
in a temporary directory and checks first-run initialization, check mode,
idempotence, preservation of existing images, and later Sync/UI modifications.
The wrapper test runs a fake launcher and checks quiet output, appended logs,
and exit-status propagation. Neither test stops a live display manager. A real
TTY/graphical-login recovery remains a hardware check requiring explicit approval.
