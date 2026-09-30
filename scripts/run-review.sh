#!/usr/bin/env bash

set -euo pipefail

readonly PUBLISHED_MARKER="AUGURE_REVIEW_PUBLISHED"
readonly MISSING_INTENT_MARKER="AUGURE_REVIEW_BLOCKED_MISSING_INTENT"

die() {
  printf 'augure-review: %s\n' "$*" >&2
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
  [[ -n "${REVIEW_PROVIDER_TOKEN:-}" ]] || die "REVIEW_PROVIDER_TOKEN is required"
  validate_augure_token "${AUGURE_TOKEN:-}"

  local adapter="$AUGURE_REVIEW_ACTION_ROOT/providers/$AUGURE_REVIEW_PROVIDER.sh"
  [[ -f "$adapter" ]] || die "unsupported provider: $AUGURE_REVIEW_PROVIDER"
}

install_augure() {
  local installer="$1/install.sh"

  require_command curl
  curl -fsSL --retry 3 https://augureai.ca/install.sh -o "$installer"
  AUGURE_NON_INTERACTIVE=1 sh "$installer"

  if ! command -v augure >/dev/null 2>&1 && [[ -x "$HOME/.local/bin/augure" ]]; then
    export PATH="$HOME/.local/bin:$PATH"
  fi
  require_command augure
  augure --version
}

write_augure_config() {
  local config_dir="$1"
  local instructions_file="$2"
  local model="$3"
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
model_instructions_file = "$instructions_file"

[permissions.review]
description = "Read-only pull request review with provider API access"
extends = ":read-only"

[permissions.review.network]
enabled = true
mode = "full"

[model_providers.augure_ci]
name = "Augure CI"
base_url = "https://api.augureai.ca/v1"
env_key = "AUGURE_TOKEN"
wire_api = "responses"
requires_openai_auth = false

[shell_environment_policy]
inherit = "all"
exclude = ["AUGURE_TOKEN", "AUGURE_API_KEY", "OPENAI_API_KEY", "REVIEW_PROVIDER_TOKEN"]
EOF
  chmod 600 "$config_dir/config.toml"
}

build_prompt() {
  local output_file="$1"

  cat "$AUGURE_REVIEW_PROMPT_FILE" >"$output_file"
  printf '\n\n' >>"$output_file"
  provider_prompt >>"$output_file"
}

check_result() {
  local result_file="$1"

  if grep -Fxq "$PUBLISHED_MARKER" "$result_file"; then
    return 0
  fi

  if grep -Fxq "$MISSING_INTENT_MARKER" "$result_file"; then
    die "review stopped because the pull request has no clear intent or goals"
  fi

  die "Augure did not confirm that it published a complete review"
}

main() {
  validate_inputs
  require_command git

  local state_root
  state_root="$(mktemp -d "${RUNNER_TEMP:-/tmp}/augure-review.XXXXXX")"
  AUGURE_REVIEW_STATE_ROOT="$state_root"
  trap 'rm -rf -- "$AUGURE_REVIEW_STATE_ROOT"' EXIT
  chmod 700 "$state_root"

  export AUGURE_HOME="$state_root/augure-home"
  export CODEX_HOME="$AUGURE_HOME"

  # Load only the selected provider. Each adapter implements the same functions.
  # shellcheck source=/dev/null
  source "$AUGURE_REVIEW_ACTION_ROOT/providers/$AUGURE_REVIEW_PROVIDER.sh"

  provider_validate
  provider_prepare_environment
  provider_verify_head

  git check-ref-format --branch "$AUGURE_REVIEW_BASE_REF" >/dev/null 2>&1 ||
    die "base ref has an invalid format"
  git rev-parse --verify "origin/${AUGURE_REVIEW_BASE_REF}^{commit}" >/dev/null 2>&1 ||
    die "base ref is not available: origin/$AUGURE_REVIEW_BASE_REF"

  install_augure "$state_root"
  build_prompt "$state_root/prompt.md"
  write_augure_config "$AUGURE_HOME" "$state_root/prompt.md" "$AUGURE_REVIEW_MODEL"

  augure \
    --enable use_legacy_landlock \
    --ask-for-approval never \
    exec \
    --model "$AUGURE_REVIEW_MODEL" \
    --ephemeral \
    --output-last-message "$state_root/result.txt" \
    "Perform and publish the bounded pull request review defined by your instructions. Review only origin/$AUGURE_REVIEW_BASE_REF...$AUGURE_REVIEW_EXPECTED_HEAD_SHA."

  [[ -s "$state_root/result.txt" ]] || die "Augure returned no final result"
  provider_verify_head
  check_result "$state_root/result.txt"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
