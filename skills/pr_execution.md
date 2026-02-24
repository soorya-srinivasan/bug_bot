# PR Execution

Use this guide when a root cause has been identified and a code fix is warranted.

All repository operations use the **GitHub MCP** (`mcp__github__*`) via the GitHub API.
No local git clone, no Bash git commands.

## Pre-PR Checklist

Before creating a branch or writing code, confirm:
- [ ] Root cause is clearly understood (not just a symptom)
- [ ] Fix scope is minimal — only change what is necessary
- [ ] Fix does not require a data migration (if so, loop in the owning team)
- [ ] You have identified the correct repo and base branch (usually `main` or `master`)

---

## Step 1 — Read the File(s) to Change

```
mcp__github__get_file_contents(
  owner: "<GITHUB_ORG>",
  repo:  "<repo>",
  path:  "path/to/file.cs",
  ref:   "main"
)
```

Save the returned `sha` — you will need it in Step 3 to update the file.

---

## Step 2 — Create a Branch

Branch naming convention: `bugfix/<bug-id>-<short-description>`

```
mcp__github__create_branch(
  owner:       "<GITHUB_ORG>",
  repo:        "<repo>",
  branch:      "bugfix/<bug-id>-<short-description>",
  from_branch: "main"
)
```

---

## Step 3 — Commit the Fix

### Single file

```
mcp__github__create_or_update_file(
  owner:   "<GITHUB_ORG>",
  repo:    "<repo>",
  path:    "path/to/file.cs",
  message: "fix(<service>): <short description>\n\n<body>\n\nBug: <bug-id>",
  content: "<base64-encoded new file content>",
  branch:  "bugfix/<bug-id>-<short-description>",
  sha:     "<sha from Step 1>"
)
```

### Multiple files (single commit)

```
mcp__github__push_files(
  owner:   "<GITHUB_ORG>",
  repo:    "<repo>",
  branch:  "bugfix/<bug-id>-<short-description>",
  message: "fix(<service>): <short description>\n\n<body>\n\nBug: <bug-id>",
  files: [
    { path: "path/to/file1.cs", content: "<new content>" },
    { path: "path/to/file2.cs", content: "<new content>" }
  ]
)
```

`push_files` content is plain text (not base64); `create_or_update_file` content is base64.

---

## Step 4 — Create the PR

```
mcp__github__create_pull_request(
  owner: "<GITHUB_ORG>",
  repo:  "<repo>",
  title: "fix(<service>): <short description> [<bug-id>]",
  body:  "<PR body — see template below>",
  head:  "bugfix/<bug-id>-<short-description>",
  base:  "main"
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

---

## Multi-Repo PR Workflow

When a bug spans multiple services in different repositories, create a separate PR for each affected repo:

1. **Identify all affected repos** — from your investigation, list every repo that needs a fix.
2. **For each repo**, repeat Steps 1–4 above:
   - Read the file(s) to change
   - Create a branch (`bugfix/<bug-id>-<short-description>`)
   - Commit the fix
   - Create the PR
3. **Invoke the code-reviewer subagent** for each set of changes before committing.
4. **Populate output fields:**
   - Set `pr_urls` to a list of objects: `[{"repo": "<repo>", "branch": "<branch>", "pr_url": "<url>", "service": "<service>"}, ...]`
   - Set `pr_url` to the first/primary PR URL (backward compatibility)
5. **Cross-reference PRs** — in each PR body, link to the other related PRs under a "Related PRs" section.

---

## Step 5 — Post-PR

- Add the owning team as reviewers (from `service_team_fetching.md`)
- Do NOT merge the PR — leave merge to the owning team
- Post the PR link in the bug report / Slack thread with a summary of the root cause
