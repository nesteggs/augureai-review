#!/usr/bin/env bash

set -euo pipefail

readonly AUGURE_RELEASE_URL="https://updates.augureai.ca/augure-code"

die() {
  printf 'augure-review: [configuration] %s\n' "$*" >&2
  if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    printf '::error title=Augure review failed (configuration)::%s\n' "$*" >&2
  fi
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf 'failure-category=configuration\n' >>"$GITHUB_OUTPUT"
  fi
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

validate_augure_token() {
  case "${1:-}" in
    aug_sk_live_*) return 0 ;;
    aug_cli_*) return 0 ;;
    "") die "AUGURE_TOKEN is required" ;;
    *) die "AUGURE_TOKEN must start with aug_sk_live_ or aug_cli_" ;;
  esac
}

validate_inputs() {
  [[ -n "${AUGURE_REVIEW_ACTION_ROOT:-}" ]] || die "action root is required"
  [[ "${AUGURE_REVIEW_PROVIDER:-}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die "provider has an invalid format"
  [[ "${AUGURE_REVIEW_MODEL:-}" =~ ^[a-z0-9][a-z0-9._-]*$ ]] || die "model has an invalid format"
  [[ -f "${AUGURE_REVIEW_PROMPT_FILE:-}" ]] || die "prompt file does not exist"
  [[ -n "${AUGURE_REVIEW_REPOSITORY:-}" ]] || die "repository is required"
  [[ "${AUGURE_REVIEW_CHANGE_NUMBER:-}" =~ ^[1-9][0-9]*$ ]] || die "change number must be a positive integer"
  [[ -n "${AUGURE_REVIEW_BASE_REF:-}" ]] || die "base ref is required"
  [[ "${AUGURE_REVIEW_EXPECTED_HEAD_SHA:-}" =~ ^[0-9a-fA-F]{40}$ ]] || die "expected head SHA must contain 40 hexadecimal characters"
  [[ "${AUGURE_REVIEW_CLI_VERSION:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "Augure version must be a release number such as 1.0.7"
  [[ -z "${AUGURE_REVIEW_CLI_SHA256:-}" || "$AUGURE_REVIEW_CLI_SHA256" =~ ^[0-9a-f]{64}$ ]] ||
    die "Augure SHA-256 must contain 64 lowercase hexadecimal characters"
  [[ -n "${AUGURE_REVIEW_STATE_DIR:-}" ]] || die "state directory is required"
  [[ -n "${REVIEW_PROVIDER_TOKEN:-}" ]] || die "REVIEW_PROVIDER_TOKEN is required"
  validate_augure_token "${AUGURE_TOKEN:-}"

  local adapter="$AUGURE_REVIEW_ACTION_ROOT/providers/$AUGURE_REVIEW_PROVIDER.sh"
  [[ -f "$adapter" ]] || die "unsupported provider: $AUGURE_REVIEW_PROVIDER"
}

platform_slug() {
  local os arch
  case "$(uname -s)" in
    Linux) os=linux ;;
    Darwin) os=darwin ;;
    *) die "unsupported runner operating system: $(uname -s)" ;;
  esac
  case "$(uname -m)" in
    x86_64|amd64) arch=x64 ;;
    arm64|aarch64) arch=arm64 ;;
    *) die "unsupported runner architecture: $(uname -m)" ;;
  esac
  printf '%s-%s' "$os" "$arch"
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

# The release server publishes only its current build, so the pinned version is
# enforced by checking the advertised release and the installed binary rather
# than by selecting an older download.
install_augure() {
  local version="$1"
  local work="$2"
  local tarball published expected actual reported

  require_command curl
  require_command tar
  require_command jq
  tarball="augure-$(platform_slug).tar.gz"
  mkdir -p "$work/bin"

  published="$(curl -fsSL --retry 3 "$AUGURE_RELEASE_URL/latest.json" | jq -r '.version // empty')"
  [[ "$published" == "$version" ]] ||
    die "pinned Augure $version is unavailable; the release server publishes ${published:-an unknown version}. Update augure-version after validating that release."

  curl -fsSL --retry 3 "$AUGURE_RELEASE_URL/$tarball" -o "$work/$tarball"
  expected="$(curl -fsSL --retry 3 "$AUGURE_RELEASE_URL/$tarball.sha256" | awk '{print $1}')"
  actual="$(sha256_of "$work/$tarball")"
  [[ -n "$expected" && "$actual" == "$expected" ]] || die "Augure download checksum does not match the published checksum"
  if [[ -n "${AUGURE_REVIEW_CLI_SHA256:-}" && "$actual" != "$AUGURE_REVIEW_CLI_SHA256" ]]; then
    die "Augure download checksum $actual does not match the pinned augure-sha256"
  fi

  tar -xzf "$work/$tarball" -C "$work/bin" augure
  chmod +x "$work/bin/augure"
  export PATH="$work/bin:$PATH"

  reported="$(augure --version)"
  [[ "$reported" == "augure $version" ]] || die "installed Augure reports '$reported', expected 'augure $version'"
  printf 'augure-review: installed %s (sha256 %s)\n' "$reported" "$actual"
}

write_augure_config() {
  local config_dir="$1"
  local model="$2"
  local context_window_line=""

  case "$model" in
    rosedale-1) context_window_line="model_context_window = 1024000" ;;
    ossington-5|tofino-3) context_window_line="model_context_window = 262144" ;;
  esac

  mkdir -p "$config_dir"
  chmod 700 "$config_dir"
  cat >"$config_dir/config.toml" <<EOF
model_provider = "augure_ci"
$context_window_line
default_permissions = "review"
check_for_update_on_startup = false

[permissions.review]
description = "Read-only pull request review; publication is performed by the orchestrator"
extends = ":read-only"

[model_providers.augure_ci]
name = "Augure CI"
base_url = "https://api.augureai.ca/v1"
env_key = "AUGURE_TOKEN"
wire_api = "responses"
requires_openai_auth = false

[shell_environment_policy]
inherit = "all"
exclude = ["AUGURE_TOKEN", "AUGURE_API_KEY", "OPENAI_API_KEY", "REVIEW_PROVIDER_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
EOF
  chmod 600 "$config_dir/config.toml"
}

prepare_state_dir() {
  local state_dir="$1"
  if [[ -e "$state_dir" ]] && [[ -n "$(ls -A "$state_dir" 2>/dev/null)" ]]; then
    die "state directory already contains files: $state_dir"
  fi
  mkdir -p "$state_dir"
  chmod 700 "$state_dir"
}

main() {
  validate_inputs
  require_command git
  require_command python3

  git check-ref-format --branch "$AUGURE_REVIEW_BASE_REF" >/dev/null 2>&1 ||
    die "base ref has an invalid format"
  git rev-parse --verify "refs/remotes/origin/${AUGURE_REVIEW_BASE_REF}^{commit}" >/dev/null 2>&1 ||
    die "base ref is not available: origin/$AUGURE_REVIEW_BASE_REF"

  prepare_state_dir "$AUGURE_REVIEW_STATE_DIR"

  # Augure's home holds only non-secret configuration, but it is kept outside
  # the preserved state so that nothing else the CLI writes there is archived.
  local work
  work="$(mktemp -d "${RUNNER_TEMP:-/tmp}/augure-review-work.XXXXXX")"
  AUGURE_REVIEW_WORK_DIR="$work"
  trap 'rm -rf -- "$AUGURE_REVIEW_WORK_DIR"' EXIT
  chmod 700 "$work"

  export AUGURE_HOME="$work/augure-home"
  export CODEX_HOME="$AUGURE_HOME"

  write_augure_config "$AUGURE_HOME" "$AUGURE_REVIEW_MODEL"
  install_augure "$AUGURE_REVIEW_CLI_VERSION" "$work"
  cp "$AUGURE_HOME/config.toml" "$AUGURE_REVIEW_STATE_DIR/augure-config.toml"

  # The runner signals only this shell on cancellation. Forward it so that the
  # orchestrator can stop its sessions and record the cancellation.
  PYTHONPATH="$AUGURE_REVIEW_ACTION_ROOT/scripts${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -m augure_review &
  AUGURE_REVIEW_PID=$!
  trap 'kill -TERM "$AUGURE_REVIEW_PID" 2>/dev/null || true' TERM INT
  local status=0
  wait "$AUGURE_REVIEW_PID" || status=$?
  # A trapped signal interrupts wait before the orchestrator has finished.
  while kill -0 "$AUGURE_REVIEW_PID" 2>/dev/null; do
    status=0
    wait "$AUGURE_REVIEW_PID" || status=$?
  done
  return "$status"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
