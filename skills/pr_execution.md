# PR Execution

Use this guide when a root cause has been identified and a code fix is warranted.

## Pre-PR Checklist

Before creating a branch or writing code, confirm:
- [ ] Root cause is clearly understood (not just a symptom)
- [ ] Fix scope is minimal — only change what is necessary
- [ ] Fix does not require a data migration (if so, loop in the owning team)
- [ ] You have identified the correct repo and base branch (usually `main` or `master`)

## Step 1 — Create a Branch

Branch naming convention:
```
bugfix/<bug-id>-<short-description>
```

Example: `bugfix/BUG-1234-nil-payment-reference`

```
git.create_branch(
  repo: "shopuptech/<repo>",
  branch: "bugfix/<bug-id>-<short-description>",
  from: "main"
)
```

## Step 2 — Apply the Fix

- Search for the relevant file via `github.search_code` before editing
- Make the smallest change that fixes the root cause
- For .NET services: follow existing code style; do not remove existing null checks
- For Rails services: follow RuboCop conventions; prefer safe navigation (`&.`) over explicit nil checks

## Step 3 — Commit

Commit message format:
```
fix(<service>): <short description>

<one paragraph explaining root cause and how the fix addresses it>

Bug: <bug-id>
```

## Step 4 — Create the PR

```
github.create_pull_request(
  repo: "shopuptech/<repo>",
  title: "fix(<service>): <short description>",
  body: "<PR body — see template below>",
  head: "bugfix/<bug-id>-<short-description>",
  base: "main"
)
```

### PR Body Template

```markdown
## Summary
<!-- One sentence describing the fix -->

## Root Cause
<!-- What was the underlying bug? -->

## Changes
- <!-- File and what changed -->

## Testing
- [ ] Existing unit tests pass
- [ ] New test added for this scenario (if applicable)

## References
- Bug ID: <bug-id>
- Investigation log: <link or summary>
```

## Step 5 — Post-PR

- Add the owning team as reviewers (from `service_team_fetching.md`)
- Do NOT merge the PR — leave merge to the owning team
- Post the PR link in the bug report / Slack thread with a summary of the root cause
