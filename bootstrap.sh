#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="https://github.com/NedaOS/fedora-setup.git"
CHEZMOI_REPO_URL="https://github.com/NedaOS/live-dotfiles.git"
REPO_DIR="${FEDORA_SETUP_DIR:-${HOME}/Ansible}"

log() {
  printf '\n[bootstrap] %s\n' "$*"
}

die() {
  printf '\n[bootstrap] ERROR: %s\n' "$*" >&2
  exit 1
}

# Verify the operating system.
[[ -r /etc/os-release ]] ||
  die "Cannot identify the operating system."

# shellcheck disable=SC1091
source /etc/os-release

[[ "${ID:-}" == "fedora" ]] ||
  die "This bootstrap script is intended for Fedora."

# This script must run as a regular user.
[[ "${EUID}" -ne 0 ]] ||
  die "Run this script as a regular user, not as root."

command -v sudo >/dev/null 2>&1 ||
  die "sudo is required to install system packages."

command -v dnf >/dev/null 2>&1 ||
  die "dnf is required to install system packages."

sudo -v

# Install bootstrap prerequisites.
for cmd in git ansible-playbook ansible-galaxy chezmoi; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    log "Installing bootstrap prerequisites..."
    sudo dnf install -y git ansible-core chezmoi
    break
  fi
done

# Verify required commands after installation.
for cmd in git ansible-playbook ansible-galaxy chezmoi; do
  command -v "$cmd" >/dev/null 2>&1 ||
    die "Required command not found: $cmd"
done

# Clone the Ansible repository if it does not exist.
if [[ ! -d "$REPO_DIR/.git" ]]; then
  [[ ! -e "$REPO_DIR" ]] ||
    die "Target path exists but is not a Git repository: $REPO_DIR"

  log "Cloning the Ansible repository..."
  git clone "$REPO_URL" "$REPO_DIR"
fi

# Verify the Ansible repository.
git -C "$REPO_DIR" rev-parse --is-inside-work-tree \
  >/dev/null 2>&1 ||
  die "Invalid Git repository: $REPO_DIR"

for file in \
  requirements.yml \
  playbooks/default-install.yml \
  playbooks/manual-install.yml; do
  [[ -f "$REPO_DIR/$file" ]] ||
    die "Required file not found: $REPO_DIR/$file"
done

# Install required Ansible collections.
log "Installing Ansible collections..."
ansible-galaxy collection install 
  -r "$REPO_DIR/requirements.yml"

# Install system packages and configure repositories.
log "Installing system packages and configuring repositories..."
ansible-playbook 
  "$REPO_DIR/playbooks/default-install.yml" 
  --ask-become-pass

# Install user-level tools.
log "Installing user-level tools..."
ansible-playbook 
  "$REPO_DIR/playbooks/manual-install.yml"

# Initialize or validate chezmoi source.
CHEZMOI_SOURCE="$(chezmoi source-path)"

if [[ -d "$CHEZMOI_SOURCE/.git" ]]; then
  CHEZMOI_REMOTE="$(
    git -C "$CHEZMOI_SOURCE" remote get-url origin
  )" || die "Cannot determine chezmoi source remote."

  case "$CHEZMOI_REMOTE" in
    git@github.com:NedaOS/live-dotfiles.git|
    https://github.com/NedaOS/live-dotfiles.git|\
    https://github.com/NedaOS/live-dotfiles)
      ;;
    *)
      die "Unexpected chezmoi source remote: $CHEZMOI_REMOTE"
      ;;
  esac

  log "Applying existing chezmoi configuration..."
  chezmoi apply
else
  [[ ! -e "$CHEZMOI_SOURCE" ]] ||
    die "Chezmoi source path exists but is not a Git repository: $CHEZMOI_SOURCE"

  log "Initializing chezmoi from NedaOS/live-dotfiles..."
  chezmoi init --apply "$CHEZMOI_REPO_URL"
fi

log "Bootstrap completed successfully."
