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

`prompt-file` points to your repository's review policy.

The runner needs Bash, Git, curl, the GitHub CLI, and jq. The action installs Augure during execution. Credentials are supplied through environment variables and must not be committed.

## Validation

Run `bash tests/test.sh` from any working directory. The suite uses a bundled policy fixture and mocked installer, CLI, and GitHub API commands; it does not publish a review or call a live model.
