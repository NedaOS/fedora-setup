#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(
  cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd
)"
REPO_DIR="$(dirname -- "$SCRIPT_DIR")"

log() {
  printf '\n[bootstrap] %s\n' "$*"
}

die() {
  printf '[bootstrap] ERROR: %s\n' "$*" >&2
  exit 1
}

# Verify the operating system.
if [[ ! -r /etc/os-release ]]; then
  die "Cannot identify the operating system."
fi

# shellcheck disable=SC1091
source /etc/os-release

[[ "${ID:-}" == "fedora" ]] ||
  die "This bootstrap script is intended for Fedora."

# Verify required commands.
for cmd in ansible-playbook chezmoi; do
  command -v "$cmd" >/dev/null 2>&1 ||
    die "Required command not found: $cmd"
done

# Verify the Ansible repository.
[[ -f "$REPO_DIR/playbooks/default-install.yml" ]] ||
  die "Ansible playbook not found in $REPO_DIR."

[[ -f "$REPO_DIR/playbooks/manual-install.yml" ]] ||
  die "Manual-install playbook not found in $REPO_DIR."

# Verify the chezmoi source directory.
CHEZMOI_SOURCE="$(chezmoi source-path)"

git -C "$CHEZMOI_SOURCE" rev-parse --is-inside-work-tree \
  >/dev/null 2>&1 ||
  die "chezmoi source is not a Git repository: $CHEZMOI_SOURCE"

# Install system packages and configure repositories.
log "Installing system packages and configuring repositories..."
ansible-playbook \
  "$REPO_DIR/playbooks/default-install.yml" \
  --ask-become-pass

# Install user-level tools without privilege escalation.
log "Installing user-level tools..."
ansible-playbook \
  "$REPO_DIR/playbooks/manual-install.yml"

# Apply dotfiles and user configuration.
log "Applying chezmoi configuration..."
chezmoi apply

log "Bootstrap completed successfully."
