#!/usr/bin/env bash

set -euo pipefail

readonly TEST_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck source=../scripts/run-review.sh
source "$TEST_ROOT/scripts/run-review.sh"
# shellcheck source=../providers/github.sh
source "$TEST_ROOT/providers/github.sh"

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

test_result_markers() {
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' RETURN

  printf '%s\n' "$PUBLISHED_MARKER" >"$test_dir/published"
  assert_succeeds check_result "$test_dir/published"

  printf '%s\n' "$MISSING_INTENT_MARKER" >"$test_dir/missing"
  assert_fails check_result "$test_dir/missing"

  printf 'unexpected\n' >"$test_dir/invalid"
  assert_fails check_result "$test_dir/invalid"
}

test_github_prompt() {
  AUGURE_REVIEW_REPOSITORY='acme/example'
  AUGURE_REVIEW_CHANGE_NUMBER='42'
  AUGURE_REVIEW_BASE_REF='main'
  AUGURE_REVIEW_EXPECTED_HEAD_SHA='0123456789abcdef0123456789abcdef01234567'

  local prompt
  prompt="$(provider_prompt)"
  grep -Fq 'Repository: acme/example' <<<"$prompt" || fail 'repository missing from prompt'
  grep -Fq 'Pull request: 42' <<<"$prompt" || fail 'change number missing from prompt'
  grep -Fq "$PUBLISHED_MARKER" <<<"$prompt" || fail 'published marker missing from prompt'
  grep -Fq "$MISSING_INTENT_MARKER" <<<"$prompt" || fail 'missing-intent marker missing from prompt'
  grep -Fq 'event=COMMENT' <<<"$prompt" || fail 'missing-intent review event missing from prompt'
  if grep -Fq 'gh pr comment' <<<"$prompt"; then
    fail 'missing-intent handling must not require issues: write'
  fi
}

test_review_policy() {
  grep -Fq 'Call 10 must be' "$TEST_ROOT/tests/fixtures/review-policy.md" ||
    fail 'review policy must reserve the final tool call for publication'
}

test_augure_config() {
  local test_dir
  test_dir="$(mktemp -d)"
  trap 'rm -rf "$test_dir"' RETURN
  printf 'review instructions\n' >"$test_dir/prompt.md"

  write_augure_config "$test_dir/home" "$test_dir/prompt.md" 'tofino-3'
  grep -Fq "model_instructions_file = \"$test_dir/prompt.md\"" "$test_dir/home/config.toml" ||
    fail 'model instructions file missing from config'
  grep -Fq 'env_key = "AUGURE_TOKEN"' "$test_dir/home/config.toml" ||
    fail 'Augure token environment mapping missing from config'
  grep -Fq 'model_context_window = 262144' "$test_dir/home/config.toml" ||
    fail 'Augure model context window missing from config'
  grep -Fq 'default_permissions = "review"' "$test_dir/home/config.toml" ||
    fail 'review permission profile missing from config'
  grep -Fq 'extends = ":read-only"' "$test_dir/home/config.toml" ||
    fail 'review permission profile must extend read-only access'
  grep -Fq 'enabled = true' "$test_dir/home/config.toml" ||
    fail 'provider network access missing from review permission profile'
  if grep -Fq 'model_reasoning_effort' "$test_dir/home/config.toml"; then
    fail 'config must not set a reasoning effort'
  fi
}

test_provider_environment() {
  REVIEW_PROVIDER_TOKEN='provider-token'
  GH_TOKEN=''
  provider_prepare_environment
  [[ "$GH_TOKEN" == "$REVIEW_PROVIDER_TOKEN" ]] || fail 'provider token was not mapped to GH_TOKEN'
}

test_main_with_mocks() {
  local test_dir="$1"
  local mock_bin="$test_dir/bin"
  mkdir -p "$mock_bin" "$test_dir/home" "$test_dir/runner-temp"

  cat >"$mock_bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
output=''
while (($#)); do
  if [[ "$1" == '-o' ]]; then
    output="$2"
    shift 2
  else
    shift
  fi
done
cat >"$output" <<'INSTALLER'
#!/usr/bin/env bash
set -eu
mkdir -p "$HOME/.local/bin"
cat >"$HOME/.local/bin/augure" <<'AUGURE'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == '--version' ]]; then
  printf 'augure test-version\n'
  exit 0
fi
printf '%s\n' "$*" >"$MOCK_AUGURE_ARGS"
result=''
while (($#)); do
  if [[ "$1" == '--output-last-message' ]]; then
    result="$2"
    shift 2
  else
    shift
  fi
done
[[ -n "$result" ]]
printf 'AUGURE_REVIEW_PUBLISHED\n' >"$result"
AUGURE
chmod +x "$HOME/.local/bin/augure"
INSTALLER
EOF
  chmod +x "$mock_bin/curl"

  cat >"$mock_bin/gh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$MOCK_HEAD_SHA"
EOF
  chmod +x "$mock_bin/gh"

  cat >"$mock_bin/jq" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
  chmod +x "$mock_bin/jq"

  cat >"$mock_bin/git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  check-ref-format|rev-parse) exit 0 ;;
  *) printf 'unexpected git invocation: %s\n' "$*" >&2; exit 1 ;;
esac
EOF
  chmod +x "$mock_bin/git"

  (
    export HOME="$test_dir/home"
    export PATH="$mock_bin:/usr/bin:/bin"
    export RUNNER_TEMP="$test_dir/runner-temp"
    export MOCK_AUGURE_ARGS="$test_dir/augure-args"
    export MOCK_HEAD_SHA='0123456789abcdef0123456789abcdef01234567'
    export AUGURE_TOKEN='aug_cli_example'
    export REVIEW_PROVIDER_TOKEN='provider-token'
    export AUGURE_REVIEW_ACTION_ROOT="$TEST_ROOT"
    export AUGURE_REVIEW_PROVIDER='github'
    export AUGURE_REVIEW_MODEL='tofino-3'
    export AUGURE_REVIEW_PROMPT_FILE="$TEST_ROOT/tests/fixtures/review-policy.md"
    export AUGURE_REVIEW_REPOSITORY='acme/example'
    export AUGURE_REVIEW_CHANGE_NUMBER='42'
    export AUGURE_REVIEW_BASE_REF='dev/pr/214-sync'
    export AUGURE_REVIEW_EXPECTED_HEAD_SHA="$MOCK_HEAD_SHA"
    main
  )

  grep -Fq -- '--enable use_legacy_landlock' "$test_dir/augure-args" ||
    fail 'Augure review invocation did not select the Landlock sandbox'
  grep -Fq 'exec --model tofino-3' "$test_dir/augure-args" ||
    fail 'Augure review invocation did not include the expected model'
  grep -Fq 'Review only origin/dev/pr/214-sync...0123456789abcdef0123456789abcdef01234567.' "$test_dir/augure-args" ||
    fail 'Augure review invocation did not include the expected diff range'
  if grep -Fq -- '--effort' "$test_dir/augure-args"; then
    fail 'Augure invocation must not set an effort'
  fi
}

test_token_validation
test_result_markers
test_github_prompt
test_review_policy
test_augure_config
test_provider_environment
integration_dir="$(mktemp -d)"
trap 'rm -rf "$integration_dir"' EXIT
test_main_with_mocks "$integration_dir"
printf 'augure-review tests passed\n'
