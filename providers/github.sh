#!/usr/bin/env bash
# GitHub provider adapter.
#
# Usage: github.sh validate | head | context | publish PAYLOAD_FILE | review REVIEW_ID

set -euo pipefail

die() {
  printf 'github provider: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

pull_endpoint() {
  printf 'repos/%s/pulls/%s' "$AUGURE_REVIEW_REPOSITORY" "$AUGURE_REVIEW_CHANGE_NUMBER"
}

cmd_validate() {
  require_command gh
  require_command jq
  [[ "${AUGURE_REVIEW_REPOSITORY:-}" =~ ^[^/[:space:]]+/[^/[:space:]]+$ ]] ||
    die "GitHub repository must use owner/name format"
  [[ "${AUGURE_REVIEW_CHANGE_NUMBER:-}" =~ ^[1-9][0-9]*$ ]] ||
    die "pull request number must be a positive integer"
}

cmd_head() {
  gh api "$(pull_endpoint)" --jq '.head.sha'
}

# Runs in a subshell so that its cleanup trap cannot outlive it.
cmd_context() (
  work="$(mktemp -d)"
  trap 'rm -rf -- "$work"' EXIT

  gh api "$(pull_endpoint)" >"$work/pull.json"
  gh api --paginate "$(pull_endpoint)/reviews" --jq '.[]' | jq -s '.' >"$work/reviews.json"
  gh api --paginate "$(pull_endpoint)/comments" --jq '.[]' | jq -s '.' >"$work/comments.json"

  jq -n \
    --slurpfile pull "$work/pull.json" \
    --slurpfile reviews "$work/reviews.json" \
    --slurpfile comments "$work/comments.json" \
    '{
      title: ($pull[0].title // ""),
      body: ($pull[0].body // ""),
      author: ($pull[0].user.login // ""),
      head_sha: $pull[0].head.sha,
      base_ref: $pull[0].base.ref,
      reviews: [$reviews[0][] | {
        id, user: (.user.login // ""), state, body: (.body // ""), commit_id, submitted_at
      }],
      review_comments: [$comments[0][] | {
        id, user: (.user.login // ""), path, line, original_line, side,
        body: (.body // ""), in_reply_to_id, commit_id, created_at
      }]
    }'
)

cmd_publish() {
  local payload="${1:-}"
  [[ -f "$payload" ]] || die "review payload file is required"
  gh api --method POST "$(pull_endpoint)/reviews" --input "$payload"
}

cmd_review() {
  local review_id="${1:-}"
  [[ "$review_id" =~ ^[1-9][0-9]*$ ]] || die "review ID must be a positive integer"
  gh api "$(pull_endpoint)/reviews/$review_id"
}

main() {
  [[ -n "${REVIEW_PROVIDER_TOKEN:-}" ]] || die "REVIEW_PROVIDER_TOKEN is required"
  export GH_TOKEN="$REVIEW_PROVIDER_TOKEN"

  local command="${1:-}"
  shift || true
  case "$command" in
    validate) cmd_validate ;;
    head) cmd_head ;;
    context) cmd_context ;;
    publish) cmd_publish "$@" ;;
    review) cmd_review "$@" ;;
    *) die "unknown command: ${command:-<none>}" ;;
  esac
}

main "$@"
