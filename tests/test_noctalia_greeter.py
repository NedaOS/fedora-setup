"""Offline checks; every write and systemctl operation targets a temporary root."""

import configparser
import json
import os
import pwd
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import unittest

import jinja2
import yaml


REPO = Path(__file__).resolve().parents[1]
ROLE = REPO / "roles/noctalia_greeter"


class GreeterTests(unittest.TestCase):
    def test_templates_parse_and_keep_the_existing_session(self):
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        defaults["noctalia_greeter_desktop_home"] = "/home/tester"
        env = jinja2.Environment(keep_trailing_newline=True)
        env.filters.update(to_json=json.dumps, quote=shlex.quote)
        rendered = {}
        for path in (ROLE / "templates").glob("*.j2"):
            content = env.from_string(path.read_text()).render(defaults)
            rendered[path.name] = content
            if ".toml." in path.name:
                tomllib.loads(content)
        config = tomllib.loads(rendered["greetd-config.toml.j2"])
        self.assertEqual(config["default_session"]["command"], "/usr/bin/noctalia-greeter-session")
        self.assertEqual(config["default_session"]["user"], "greetd")
        admin = tomllib.loads(rendered["greeter.toml.j2"])
        self.assertEqual(admin["session"]["default"], "Hyprland (Quiet)")
        self.assertEqual(admin["output"], defaults["noctalia_greeter_output"])
        self.assertNotIn("appearance", admin)
        desktop = configparser.ConfigParser(interpolation=None)
        desktop.read_string(rendered["hyprland-quiet.desktop.j2"])
        self.assertEqual(desktop["Desktop Entry"]["Name"], "Hyprland (Quiet)")
        self.assertEqual(desktop["Desktop Entry"]["Exec"], defaults["noctalia_greeter_quiet_wrapper"])
        self.assertEqual(desktop["Desktop Entry"]["DesktopNames"], "Hyprland")

    def test_seed_is_idempotent_and_preserves_mutable_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir(mode=0o750)
            play = [{
                "name": "Exercise only seed tasks in a temporary directory",
                "hosts": "localhost",
                "connection": "local",
                "gather_facts": False,
                "become": False,
                "vars": {
                    "noctalia_greeter_state_dir": str(state),
                    "noctalia_greeter_user": pwd.getpwuid(os.getuid()).pw_name,
                    "noctalia_greeter_group": str(os.getgid()),
                },
                "tasks": [{"ansible.builtin.include_role": {
                    "name": str(ROLE), "tasks_from": "seed",
                }}],
            }]
            playbook = root / "seed.yml"
            playbook.write_text(yaml.safe_dump(play))

            def run(*args):
                result = subprocess.run(
                    ["ansible-playbook", "-i", "localhost,", str(playbook), *args],
                    cwd=REPO,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            run("--check", "--diff")
            self.assertEqual(list(state.iterdir()), [])
            # An existing image is preserved even when initializing missing Sync state.
            (state / "wallpaper.jpg").write_bytes(b"existing image")
            run()
            sync = state / "sync.toml"
            data = tomllib.loads(sync.read_text())
            self.assertEqual(data["appearance"]["scheme"], "Synced")
            self.assertEqual(data["appearance"]["wallpaper"]["path"], str(state / "wallpaper.jpg"))
            self.assertEqual((state / "wallpaper.jpg").read_bytes(), b"existing image")
            self.assertEqual((state / "wallpaper-eDP-1.jpg").read_bytes(), (ROLE / "files/wallpaper.jpg").read_bytes())
            self.assertIn("changed=0", run())
            (state / "greeter.toml").write_text('[session]\ndefault = "Hyprland (Quiet)"\n')
            verifier = next(
                task["ansible.builtin.command"]["argv"][2]
                for task in yaml.safe_load((ROLE / "tasks/verify.yml").read_text())
                if task["name"] == "Check that mutable state and referenced wallpapers are accessible to greetd"
            )
            def verify():
                return subprocess.run(
                    ["python3", "-c", verifier, str(state)], capture_output=True, text=True,
                )
            self.assertEqual(verify().returncode, 0)
            (state / "wallpaper-eDP-1.jpg").unlink()
            self.assertNotEqual(verify().returncode, 0)
            (state / "wallpaper-eDP-1.jpg").write_bytes(b"restored wallpaper")
            # Simulate later UI/Sync choices, permissions, and wallpaper changes.
            sync.write_text('[session]\nlast = "Other session"\n[appearance]\nscheme = "Noctalia"\n')
            sync.chmod(0o600)
            (state / "wallpaper-eDP-1.jpg").write_bytes(b"new synced wallpaper")
            before = {path.name: (path.read_bytes(), path.stat().st_mode) for path in state.iterdir()}
            self.assertIn("changed=0", run("--check", "--diff"))
            self.assertIn("changed=0", run())
            self.assertEqual(before, {path.name: (path.read_bytes(), path.stat().st_mode) for path in state.iterdir()})

    def test_quiet_wrapper_preserves_output_and_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            start = root / "start-hyprland"
            start.write_text("#!/bin/sh\necho stdout\necho stderr >&2\nexit 17\n")
            start.chmod(0o755)
            env = jinja2.Environment(keep_trailing_newline=True)
            env.filters["quote"] = shlex.quote
            wrapper = env.from_string(
                (ROLE / "templates/hyprland-quiet-session.j2").read_text()
            ).render(noctalia_greeter_hyprland_start=str(start))
            subprocess.run(["sh", "-n"], input=wrapper, text=True, check=True)
            for _ in range(2):
                result = subprocess.run(
                    ["sh", "-c", wrapper],
                    env={**os.environ, "HOME": directory},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 17)
                self.assertEqual(result.stdout + result.stderr, "")
            self.assertEqual(
                (root / ".local/state/hyprland-startup.log").read_text(),
                "stdout\nstderr\nstdout\nstderr\n",
            )

    @unittest.skipUnless(
        Path("/usr/lib/systemd/system/sddm.service").exists(),
        "The recovery rehearsal needs Fedora's installed SDDM unit",
    )
    def test_recovery_display_manager_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            units = root / "usr/lib/systemd/system"
            units.mkdir(parents=True)
            (root / "etc/systemd/system").mkdir(parents=True)
            for service in ("greetd.service", "sddm.service"):
                # Rehearse with the actual Fedora units, without contacting systemd.
                shutil.copy(Path("/usr/lib/systemd/system") / service, units)

            def systemctl(*args):
                return subprocess.run(
                    ["systemctl", "--root", directory, *args],
                    capture_output=True,
                    text=True,
                    check=True,
                )

            alias = root / "etc/systemd/system/display-manager.service"
            systemctl("enable", "sddm.service")
            systemctl("disable", "sddm.service")
            systemctl("enable", "--force", "greetd.service")
            self.assertEqual(alias.readlink().name, "greetd.service")
            # The documented TTY fallback, then the return to greetd.
            systemctl("disable", "greetd.service")
            systemctl("enable", "--force", "sddm.service")
            self.assertEqual(alias.readlink().name, "sddm.service")
            systemctl("disable", "sddm.service")
            systemctl("enable", "--force", "greetd.service")
            self.assertEqual(alias.readlink().name, "greetd.service")

    def test_role_never_starts_or_stops_services(self):
        def inspect(tasks):
            for task in tasks:
                for name, args in task.items():
                    if name in ("ansible.builtin.systemd_service", "ansible.builtin.service"):
                        self.assertNotIn("state", args)
                    if name in ("block", "rescue", "always"):
                        inspect(args)

        for path in (ROLE / "tasks").glob("*.yml"):
            inspect(yaml.safe_load(path.read_text()))
        self.assertFalse((ROLE / "handlers").exists())


if __name__ == "__main__":
    unittest.main()
