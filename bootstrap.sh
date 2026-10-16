#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="https://github.com/NedaOS/fedora-setup.git"
CHEZMOI_REPO_URL="https://github.com/NedaOS/live-dotfiles.git"
REPO_DIR="${FEDORA_SETUP_DIR:-${HOME}/Ansible}"

# The only supported platform: Fedora 44. Anything else is rejected before any change is made.
SUPPORTED_FEDORA_VERSION="44"

log() {
  printf '\n[bootstrap] %s\n' "$*"
}

die() {
  printf '\n[bootstrap] ERROR: %s\n' "$*" >&2
  exit 1
}

# Run a stage; if it fails (for example no network), record it and keep going.
FAILED_STAGES=()
run_stage() {
  local name="$1"
  shift
  if ! "$@"; then
    FAILED_STAGES+=("$name")
    printf '\n[bootstrap] WARNING: stage failed, continuing: %s\n' "$name" >&2
  fi
}

# Verify the operating system.
[[ -r /etc/os-release ]] ||
  die "Cannot identify the operating system."

# shellcheck disable=SC1091
source /etc/os-release

[[ "${ID:-}" == "fedora" ]] ||
  die "Unsupported operating system: ${PRETTY_NAME:-${ID:-unknown}}. This bootstrap script supports Fedora ${SUPPORTED_FEDORA_VERSION} only."

[[ "${VERSION_ID:-}" == "$SUPPORTED_FEDORA_VERSION" ]] ||
  die "Unsupported Fedora release: ${VERSION_ID:-unknown}. This bootstrap script supports Fedora ${SUPPORTED_FEDORA_VERSION} only."

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
run_stage "Ansible collections" \
  ansible-galaxy collection install \
  -r "$REPO_DIR/requirements.yml"

# Install system packages and configure repositories.
log "Installing system packages and configuring repositories..."
run_stage "System packages (default-install.yml)" \
  ansible-playbook \
  -i localhost, \
  "$REPO_DIR/playbooks/default-install.yml" \
  --ask-become-pass

# Install user-level tools.
log "Installing user-level tools..."
run_stage "User-level tools (manual-install.yml)" \
  ansible-playbook \
  -i localhost, \
  "$REPO_DIR/playbooks/manual-install.yml"

# Initialize or validate chezmoi source.
CHEZMOI_SOURCE="$(chezmoi source-path)"

if [[ -d "$CHEZMOI_SOURCE/.git" ]]; then
  CHEZMOI_REMOTE="$(
    git -C "$CHEZMOI_SOURCE" remote get-url origin
  )" || die "Cannot determine chezmoi source remote."

  case "$CHEZMOI_REMOTE" in
  git@github.com:NedaOS/live-dotfiles.git | \
    https://github.com/NedaOS/live-dotfiles.git | \
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

if ((${#FAILED_STAGES[@]} > 0)); then
  printf '\n[bootstrap] Finished with failed stages (re-run this script to retry; steps are idempotent):\n' >&2
  printf '  - %s\n' "${FAILED_STAGES[@]}" >&2
  exit 1
fi

log "Bootstrap completed successfully."
