#!/usr/bin/env bash

set -euo pipefail

readonly TEST_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=../scripts/run-review.sh
source "$TEST_ROOT/scripts/run-review.sh"

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  exit 1
}

assert_succeeds() {
  "$@" || fail "expected success: $*"
}

assert_fails() {
  if ("$@") >/dev/null 2>&1; then
    fail "expected failure: $*"
  fi
}

test_token_validation() {
  assert_succeeds validate_augure_token 'aug_sk_live_example'
  assert_succeeds validate_augure_token 'aug_cli_example'
  assert_fails validate_augure_token 'sk-wrong-provider'
  assert_fails validate_augure_token ''
}

valid_inputs() {
  export AUGURE_REVIEW_ACTION_ROOT="$TEST_ROOT"
  export AUGURE_REVIEW_PROVIDER='github'
  export AUGURE_REVIEW_MODEL='tofino-3'
  export AUGURE_REVIEW_PROMPT_FILE="$TEST_ROOT/tests/fixtures/review-policy.md"
  export AUGURE_REVIEW_REPOSITORY='acme/example'
  export AUGURE_REVIEW_CHANGE_NUMBER='42'
  export AUGURE_REVIEW_BASE_REF='main'
  export AUGURE_REVIEW_EXPECTED_HEAD_SHA='0123456789abcdef0123456789abcdef01234567'
  export AUGURE_REVIEW_CLI_VERSION='1.0.7'
  export AUGURE_REVIEW_CLI_SHA256=''
  export AUGURE_REVIEW_STATE_DIR='/tmp/state'
  export REVIEW_PROVIDER_TOKEN='provider-token'
  export AUGURE_TOKEN='aug_cli_example'
}

test_input_validation() {
  (valid_inputs && validate_inputs) || fail 'valid inputs were rejected'
  assert_fails bash -c "source '$TEST_ROOT/scripts/run-review.sh'; $(declare -f valid_inputs); valid_inputs; AUGURE_REVIEW_CLI_VERSION=latest validate_inputs"
  assert_fails bash -c "source '$TEST_ROOT/scripts/run-review.sh'; $(declare -f valid_inputs); valid_inputs; AUGURE_REVIEW_CLI_SHA256=abc validate_inputs"
  assert_fails bash -c "source '$TEST_ROOT/scripts/run-review.sh'; $(declare -f valid_inputs); valid_inputs; AUGURE_REVIEW_PROVIDER=gitlab validate_inputs"
  assert_fails bash -c "source '$TEST_ROOT/scripts/run-review.sh'; $(declare -f valid_inputs); valid_inputs; AUGURE_REVIEW_EXPECTED_HEAD_SHA=main validate_inputs"
}

test_review_policy() {
  if grep -Eqi 'Call 10 must be|publication' "$TEST_ROOT/tests/fixtures/review-policy.md"; then
    fail 'review policy must not own tool budgets or publication; the orchestrator does'
  fi
}

test_augure_config() {
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' RETURN

  write_augure_config "$test_dir/home" 'tofino-3'
  local config="$test_dir/home/config.toml"
  grep -Fq 'env_key = "AUGURE_TOKEN"' "$config" || fail 'Augure token environment mapping missing from config'
  grep -Fq 'model_context_window = 262144' "$config" || fail 'Augure model context window missing from config'
  grep -Fq 'default_permissions = "review"' "$config" || fail 'review permission profile missing from config'
  grep -Fq 'extends = ":read-only"' "$config" || fail 'review permission profile must extend read-only access'
  grep -Fq '"REVIEW_PROVIDER_TOKEN"' "$config" || fail 'provider token must be excluded from the shell environment'
  if grep -Fq 'enabled = true' "$config"; then
    fail 'review sessions must not have network access; publication is scripted'
  fi
  if grep -Fq 'model_reasoning_effort' "$config"; then
    fail 'config must not set a reasoning effort'
  fi
}

# Serves a release directory the way the Augure update server does.
write_mock_curl() {
  local mock_bin="$1"
  cat >"$mock_bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
output=''
url=''
while (($#)); do
  case "$1" in
    -o) output="$2"; shift 2 ;;
    --retry) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
file="$MOCK_RELEASE_DIR/${url##*/}"
[[ -f "$file" ]] || { printf 'curl: (22) 404 %s\n' "$url" >&2; exit 22; }
if [[ -n "$output" ]]; then cp "$file" "$output"; else cat "$file"; fi
EOF
  chmod +x "$mock_bin/curl"
}

make_release() {
  local release_dir="$1"
  local version="$2"
  local stage tarball
  stage="$(mktemp -d)"
  tarball="augure-$(platform_slug).tar.gz"
  cp "$TEST_ROOT/tests/mocks/augure" "$stage/augure"
  tar -czf "$release_dir/$tarball" -C "$stage" augure
  rm -rf "$stage"
  printf '{"version":"%s"}\n' "$version" >"$release_dir/latest.json"
  printf '%s  %s\n' "$(sha256_of "$release_dir/$tarball")" "$tarball" >"$release_dir/$tarball.sha256"
}

test_pinned_install() {
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' RETURN
  mkdir -p "$test_dir/bin" "$test_dir/release"
  write_mock_curl "$test_dir/bin"
  make_release "$test_dir/release" '1.0.7'
  local tarball="augure-$(platform_slug).tar.gz"

  (
    export PATH="$test_dir/bin:$PATH" MOCK_RELEASE_DIR="$test_dir/release"
    install_augure '1.0.7' "$test_dir/ok" >/dev/null
    [[ "$(command -v augure)" == "$test_dir/ok/bin/augure" ]]
  ) || fail 'pinned Augure install failed'

  (
    export PATH="$test_dir/bin:$PATH" MOCK_RELEASE_DIR="$test_dir/release"
    install_augure '1.0.8' "$test_dir/newer"
  ) >/dev/null 2>&1 && fail 'install must fail when the release server publishes another version'

  (
    export PATH="$test_dir/bin:$PATH" MOCK_RELEASE_DIR="$test_dir/release"
    export AUGURE_REVIEW_CLI_SHA256="$(printf '0%.0s' {1..64})"
    install_augure '1.0.7' "$test_dir/pinned"
  ) >/dev/null 2>&1 && fail 'install must fail when the pinned checksum differs'

  printf 'tampered' >>"$test_dir/release/$tarball"
  (
    export PATH="$test_dir/bin:$PATH" MOCK_RELEASE_DIR="$test_dir/release"
    install_augure '1.0.7' "$test_dir/tampered"
  ) >/dev/null 2>&1 && fail 'install must fail when the published checksum differs'

  make_release "$test_dir/release" '1.0.7'
  (
    export PATH="$test_dir/bin:$PATH" MOCK_RELEASE_DIR="$test_dir/release" MOCK_AUGURE_VERSION='1.0.6'
    install_augure '1.0.7' "$test_dir/mislabelled"
  ) >/dev/null 2>&1 && fail 'install must fail when the binary reports another version'
  return 0
}

test_state_dir_must_be_empty() {
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' RETURN
  touch "$test_dir/leftover"
  assert_fails prepare_state_dir "$test_dir"
  assert_succeeds prepare_state_dir "$test_dir/new"
}

test_main_with_mocks() {
  local test_dir="$1"
  local repo="$test_dir/repo"
  mkdir -p "$test_dir/bin" "$test_dir/release" "$test_dir/gh" "$test_dir/runner-temp" "$repo"
  write_mock_curl "$test_dir/bin"
  make_release "$test_dir/release" '1.0.7'
  cp "$TEST_ROOT/tests/mocks/gh" "$test_dir/bin/gh"

  git -C "$repo" init -q -b main
  git -C "$repo" config user.email test@example.com
  git -C "$repo" config user.name Test
  printf 'def main():\n    return 1\n' >"$repo/app.py"
  git -C "$repo" add -A && git -C "$repo" commit -qm base
  git -C "$repo" update-ref refs/remotes/origin/main HEAD
  printf 'def main(value):\n    return value\n' >"$repo/app.py"
  git -C "$repo" commit -qam head
  local head
  head="$(git -C "$repo" rev-parse HEAD)"
  printf '{"title":"feat: [#1] x","body":"Intent: x","user":{"login":"a"},"head":{"sha":"%s"},"base":{"ref":"main"}}\n' \
    "$head" >"$test_dir/gh/pull.json"

  local status=0
  (
    cd "$repo"
    valid_inputs
    export PATH="$test_dir/bin:$PATH"
    export RUNNER_TEMP="$test_dir/runner-temp"
    export MOCK_RELEASE_DIR="$test_dir/release"
    export MOCK_GH_DIR="$test_dir/gh"
    export MOCK_AUGURE_LOG="$test_dir/augure.log"
    export AUGURE_REVIEW_EXPECTED_HEAD_SHA="$head"
    export AUGURE_REVIEW_STATE_DIR="$test_dir/state"
    export GITHUB_OUTPUT="$test_dir/output"
    main
  ) >"$test_dir/main.log" 2>&1 || status=$?
  [[ "$status" == 0 ]] || { cat "$test_dir/main.log" >&2; fail "main exited with $status"; }

  grep -Fq 'event=APPROVE' "$test_dir/output" || fail 'main did not report the published event'
  grep -Eq '^review-id=[0-9]+$' "$test_dir/output" || fail 'main did not report the review ID'
  [[ -f "$test_dir/state/augure-config.toml" ]] || fail 'Augure config was not preserved'
  [[ -f "$test_dir/state/receipt.json" ]] || fail 'publication receipt was not preserved'
  if grep -rFq -e 'provider-token' -e 'aug_cli_example' "$test_dir/state"; then
    fail 'secrets were written to the preserved state'
  fi
  [[ -z "$(ls -A "$test_dir/runner-temp")" ]] || fail 'the work directory was not removed'
  grep -Fq '"--ephemeral"' "$test_dir/augure.log" || fail 'sessions must be ephemeral'
  grep -Fq '"--output-schema"' "$test_dir/augure.log" || fail 'sessions must use structured output'
  if grep -Fq -- '--effort' "$test_dir/augure.log"; then
    fail 'Augure invocation must not set an effort'
  fi
}

test_token_validation
test_input_validation
test_review_policy
test_augure_config
test_pinned_install
test_state_dir_must_be_empty
integration_dir="$(mktemp -d)"
trap 'rm -rf "$integration_dir"' EXIT
test_main_with_mocks "$integration_dir"
python3 -m unittest discover -b -s "$TEST_ROOT/tests"
printf 'augure-review tests passed\n'
