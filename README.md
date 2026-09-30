# Augure AI Review

Run Augure pull request reviews with your repository's review policy.

## GitHub Action

Add the action to your GitHub workflow with `uses: nesteggs/augureai-review@main`.

### Usage

Check out the PR head with full history and fetch its base branch before invoking the action. The action verifies that the PR head matches `expected-head-sha` before and after the review.

```yaml
permissions:
  contents: read
  pull-requests: write
  issues: read

steps:
  - uses: actions/checkout@v7
    with:
      fetch-depth: 0
      ref: ${{ github.event.pull_request.head.sha }}

  - name: Fetch base branch
    shell: bash
    env:
      BASE_REF: ${{ github.event.pull_request.base.ref }}
    run: git fetch --no-tags origin "refs/heads/${BASE_REF}:refs/remotes/origin/${BASE_REF}"

  - uses: nesteggs/augureai-review@main
    with:
      provider: github
      model: ossington-5
      prompt-file: .github/prompts/review.md
      repository: ${{ github.repository }}
      change-number: ${{ github.event.pull_request.number }}
      base-ref: ${{ github.event.pull_request.base.ref }}
      expected-head-sha: ${{ github.event.pull_request.head.sha }}
    env:
      AUGURE_TOKEN: ${{ secrets.AUGURE_TOKEN }}
      REVIEW_PROVIDER_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

The example assumes a `pull_request` workflow limited to non-draft, same-repository PRs. Fork PR tokens generally cannot publish reviews. Consumers can pin a published commit instead of `main`.

`prompt-file` points to your repository's review policy. The policy describes what to look for and how to classify findings. The action owns publication, tool budgets, and output format, so the policy should not instruct the model to publish a review or count its own tool calls; the action's contract takes precedence where they conflict.

The runner needs Bash, Git, Python 3.10 or later, curl, tar, the GitHub CLI, and jq. The action installs Augure during execution. Credentials are supplied through environment variables and must not be committed.

### How a review runs

1. **Freeze and plan.** The action checks that the PR head equals `expected-head-sha`, computes the merge base with `origin/<base-ref>`, and inventories every changed file and hunk. A deterministic planner assigns each file, hunk, or part of an oversized hunk to exactly one chunk within `chunk-budget-bytes`. Related files, including tests and docs next to their sources, same-directory files, and cross-references, are grouped together. It checks the plan before any session runs.
2. **Chunk reviews.** Each chunk gets a fresh, ephemeral, read-only Augure session that must return schema-validated JSON:
   - findings, each with a path, line, severity, failure scenario, evidence, and fix;
   - the units it reviewed and any it could not;
   - changed interfaces, assumptions, and open questions.

   An empty findings list means the units were reviewed and nothing was found. A failed or incomplete session is recorded as a coverage gap.
3. **Integration.** Related chunks are reviewed together from their boundary records and the actual diffs of the files they reference.
4. **Verification.** Findings are deduplicated. A fresh session re-checks each blocking finding against the source and reconciles earlier blocking Augure reviews. Rejected findings are dropped.
5. **Publication.** The action, not the model, builds and validates one review. It checks that inline comments fall on diff lines and that the head is unchanged, then posts a single atomic review and reads it back to confirm its ID, state, and commit. Blocking findings request changes. Only a review with complete coverage and no blocking findings approves. If coverage is incomplete, the action posts a `COMMENT` listing the gaps and then fails the step.

If a pull request has no clear intent, the action comments once and fails with `missing-intent`.

Every session has a wall-clock limit (`session-timeout-minutes`) and a tool-call limit (`session-max-tool-calls`), and the action terminates any session that exceeds either one. Only failed or invalid sessions are retried, up to `max-attempts`; a retry after a budget termination is told to work from the diff with fewer calls. Cancelling the workflow terminates running sessions and reports `cancelled`. Repository contents and PR text are passed to sessions as delimited, untrusted data. Sessions receive `AUGURE_TOKEN` and no other credential.

### Inputs

| Input | Default | Purpose |
| --- | --- | --- |
| `augure-version` | `1.0.7` | Required Augure release. The release server publishes only its latest build, so the run fails if it advertises a different version. Update this after validating a new release. |
| `augure-sha256` | | Optional pinned SHA-256 of the runner platform's release tarball. |
| `chunk-budget-bytes` | `120000` | Maximum instructions and input for any one session. |
| `session-timeout-minutes` | `12` | Wall-clock limit per session. |
| `session-max-tool-calls` | `20` | Tool calls per session before termination. |
| `max-attempts` | `2` | Attempts per failed session. |
| `parallel-sessions` | `2` | Concurrent sessions. |
| `max-integration-passes` | `6` | Integration sessions per run. |
| `layer-map-file` | | Optional JSON object that maps architecture labels to path globs, for example `{"api": ["services/api/*"]}`. Labels are hints; they never decide coverage. |
| `resume-dir` | | Previous run's artifact directory. The action reuses a valid session result only when the model, CLI version, commits, schema, instructions, and input all match. |
| `upload-artifacts` | `true` | Upload the run state as an artifact, even when the run fails. |
| `artifact-name` | `augure-review-<job>-<attempt>` | Set a unique name in matrix jobs. |
| `artifact-retention-days` | `14` | Artifact retention. |

### Outputs and artifacts

Outputs:

- `review-id`, `review-url`, and `event`: the published review;
- `failure-category`: one of `configuration`, `git`, `provider`, `missing-intent`, `budget`, `cli`, `quota`, `invalid-output`, `coverage`, `stale-head`, `publication`, `cancelled`, or `internal`;
- `artifacts-path`.

The artifact contains the following, with secret values redacted:

- the plan, inventory, and manifest, including the CLI version and model;
- each session's instructions, prompt, command, JSON event stream, stderr, exit status, and result;
- findings, coverage, the review payload, and the publication receipt;
- `status.json`.

To resume after a partial failure, download the earlier artifact and pass its directory as `resume-dir`:

```yaml
  - id: previous
    if: github.run_attempt != '1'
    shell: bash
    run: echo "name=augure-review-${GITHUB_JOB}-$((GITHUB_RUN_ATTEMPT - 1))" >>"$GITHUB_OUTPUT"

  - id: download
    if: steps.previous.outputs.name
    continue-on-error: true
    uses: actions/download-artifact@v4
    with:
      name: ${{ steps.previous.outputs.name }}
      path: ${{ runner.temp }}/augure-previous

  - uses: nesteggs/augureai-review@main
    with:
      # ...
      resume-dir: ${{ steps.download.outcome == 'success' && format('{0}/augure-previous', runner.temp) || '' }}
```

## Validation

Run `bash tests/test.sh` from any working directory. The suite uses a bundled policy fixture and mocked installer, Augure CLI, and GitHub API commands, and it runs the Python unit and end-to-end tests. It does not publish a review or call a live model.
