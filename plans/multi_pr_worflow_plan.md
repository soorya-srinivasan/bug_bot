# Multi-PR Support & Code Review Subagent

## Context

The current Bug Bot can only create **one PR per investigation** — the `pr_url` field is a single string in the DB model, output schema, response schemas, and Slack messages. When a bug spans multiple services in different repos (e.g., a Payment API change broke an AFT service endpoint), the agent cannot create fixes for all affected repos.

Additionally, the agent creates PRs without any self-review step, risking low-quality or insecure fixes reaching GitHub.

This plan addresses both:
1. **Multi-PR**: Allow the agent to create PRs across multiple repos in a single investigation
2. **Code Review Subagent**: Add a review step before any PR is created, using the Claude Agent SDK's subagent support

---

## Part 1: Multi-PR Support

### 1.1 Alembic Migration — Add `pr_urls` JSONB Column

**Create**: `alembic/versions/<hash>_add_pr_urls_jsonb.py`

- Add `pr_urls` (JSONB, nullable, default `[]`) to `investigations` and `investigation_followups`
- Backfill existing `pr_url` values into `pr_urls` as single-element arrays
- Keep `pr_url` column for backward compat (drop in a future migration)

### 1.2 ORM Models

**File**: `src/bug_bot/models/models.py`

Add to `Investigation` (after line 56) and `InvestigationFollowup` (after line 266):
```python
pr_urls: Mapped[list | None] = mapped_column(JSONB, nullable=True, default=list)
```

Keep existing `pr_url` field untouched.

### 1.3 Repository Layer

**File**: `src/bug_bot/db/repository.py`

- Add a `_normalize_pr_urls(result)` helper that: uses `pr_urls` if present, else wraps `pr_url` into a single-element list
- Update `save_investigation` (line 209) and `save_followup_investigation` (line 681) to persist `pr_urls`

### 1.4 Agent Output Schema

**File**: `src/bug_bot/agent/runner.py` — `_OUTPUT_SCHEMA` (line 35)

Add after `pr_url` (line 46):
```python
"pr_urls": {
    "type": ["array", "null"],
    "items": {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "branch": {"type": "string"},
            "pr_url": {"type": "string"},
            "service": {"type": "string"},
        },
        "required": ["pr_url"],
    },
},
```

### 1.5 System Prompt & Investigation Prompt

**File**: `src/bug_bot/agent/runner.py` — `_build_system_prompt()` (line 79)

Add multi-repo instructions after the existing PR creation steps:
- If bug spans multiple services in different repos, create a separate PR for each repo
- Populate `pr_urls` as a list of `{repo, branch, pr_url, service}` objects
- Set `pr_url` to the first/primary PR URL for backward compat

**File**: `src/bug_bot/agent/prompts.py` — `build_investigation_prompt()` (line 141-176)

Update the code fix action to instruct the agent to create PRs for all affected repos and populate both `pr_url` and `pr_urls`.

### 1.6 Pydantic Response Schemas

**File**: `src/bug_bot/schemas/admin.py`

Add `PRUrlEntry` model:
```python
class PRUrlEntry(BaseModel):
    repo: str = ""
    branch: str = ""
    pr_url: str
    service: str = ""
```

Add `pr_urls: list[PRUrlEntry] = []` to `InvestigationResponse` (line 83) and `InvestigationFollowupResponse` (line 100).

### 1.7 API Route Updates

**File**: `src/bug_bot/api/admin.py`

Update investigation and followup response construction to include `pr_urls` from the DB model.

### 1.8 Slack Message Formatting

**File**: `src/bug_bot/slack/messages.py`

Update all three formatting functions to render multiple PRs:
- `format_investigation_result` (line 65-74): Show numbered list if >1 PR
- `format_summary_message` (line 110-111): Show all PR links
- `format_investigation_as_markdown` (line 160-161): List all PRs

Each function falls back to `pr_url` if `pr_urls` is empty (backward compat).

### 1.9 Workflow PR Logging

**File**: `src/bug_bot/temporal/workflows/bug_investigation.py` (line 262-268)

Change from logging a single `pr_created` event to looping over `pr_urls` and logging one event per PR.

### 1.10 PR Execution Skill

**File**: `skills/pr_execution.md`

Add a "Multi-Repo PR Workflow" section with step-by-step instructions for creating PRs across multiple repos.

---

## Part 2: Code Review Subagent

### SDK Support Findings

The Claude Agent SDK **does support subagents**. Two approaches exist:

1. **Filesystem-based**: `.claude/agents/code-reviewer.md` — simple, but won't work here because the agent's `cwd` is `/tmp/bugbot-workspace/<bug_id>`, not the project root
2. **Programmatic**: Pass `agents` dict to `ClaudeAgentOptions` — reliable, works regardless of `cwd`

**Recommendation**: Use the programmatic approach since the agent runs in a temporary workspace.

### 2.1 Define the Code Review Subagent

**File**: `src/bug_bot/agent/runner.py`

Add a `_CODE_REVIEWER_PROMPT` constant with review instructions covering:
- **Correctness**: Fix addresses root cause, no new bugs, edge cases handled
- **Security**: No hardcoded secrets, no injection vulnerabilities, input validation
- **Minimal change**: Only necessary lines changed, no refactoring or formatting changes
- **No regressions**: Existing error handling preserved, API contracts intact

Add `agents` parameter to `ClaudeAgentOptions` in `_build_options()` (line 162):

```python
agents={
    "code-reviewer": {
        "description": "Reviews proposed code changes for correctness, security, and minimal change principle. Invoke before creating any PR.",
        "prompt": _CODE_REVIEWER_PROMPT,
        "tools": ["Read", "Grep", "Glob", "mcp__github__*"],
        "model": "sonnet",
    }
}
```

The subagent gets read-only tools plus GitHub MCP (for reading files in the repo). It uses `sonnet` for speed/cost.

### 2.2 Update System Prompt

**File**: `src/bug_bot/agent/runner.py` — `_build_system_prompt()` (line 79)

Add instruction: "Before committing and pushing your fix, invoke the code-reviewer subagent to review your proposed changes. Only proceed if the reviewer approves. If changes are requested, address them first."

### 2.3 Update Investigation Prompt

**File**: `src/bug_bot/agent/prompts.py` — `build_investigation_prompt()` (line 141)

Add to the code fix action step: "Before committing, invoke the code-reviewer subagent with the file paths and proposed changes. Only commit and create the PR after the reviewer approves."

---

---

## Part 3: Frontend UI Changes (firstline-ai)

**Codebase**: `/Users/suriyasrinivasan/shoptech/firstline-ai` (React 18 + TypeScript + Vite + Tailwind + Shadcn/ui)

### 3.1 TypeScript Types

**File**: `src/types/bug.ts`

Add `PRUrlEntry` interface:
```typescript
export interface PRUrlEntry {
  repo: string;
  branch: string;
  pr_url: string;
  service: string;
}
```

Add `pr_urls` to `InvestigationResponse` (line 74) and `InvestigationFollowup` (line 61):
```typescript
pr_urls: PRUrlEntry[];
```

Keep existing `pr_url: string | null` for backward compat.

### 3.2 InvestigationPanel — Multi-PR Display

**File**: `src/components/investigation/InvestigationPanel.tsx` (lines 118-135)

Replace the single PR link card with a multi-PR renderer:

- Derive `prUrls` from `data.pr_urls` (if non-empty), else fall back to wrapping `data.pr_url` into a single-element array
- **Single PR**: Render the existing card style (no visual change for the common case)
- **Multiple PRs**: Render a "Pull Requests" header with a list of PR cards, each showing:
  - Service name and repo (as label)
  - PR URL (as clickable link with `ExternalLink` icon)
  - Reuse existing styling: `border-primary/20`, `bg-primary/5`, `GitPullRequest` icon

### 3.3 FollowupsPanel — Multi-PR Display

**File**: `src/components/followups/FollowupsPanel.tsx` (lines 132-144)

Same pattern as InvestigationPanel:
- Derive `prUrls` from `followup.pr_urls` or fall back to `followup.pr_url`
- Single PR: existing compact card
- Multiple PRs: list of compact cards with service/repo labels

### 3.4 Mock Client Update

**File**: `src/api/mockClient.ts`

Update mock investigation data (lines 71, 96) to include `pr_urls` arrays alongside existing `pr_url` values. Add a mock with multiple PRs to test the multi-PR rendering.

---

## Implementation Order

| Phase | Files | Depends On |
|-------|-------|------------|
| 1. Migration + Models | `alembic/versions/...`, `models.py` | — |
| 2. Repository | `repository.py` | Phase 1 |
| 3. Output Schema | `runner.py` (_OUTPUT_SCHEMA) | — |
| 4. Subagent | `runner.py` (agents param, prompt) | — |
| 5. Prompts | `runner.py` (system prompt), `prompts.py`, `skills/pr_execution.md` | Phases 3-4 |
| 6. Response Schemas | `admin.py` (schemas) | Phase 1 |
| 7. API Routes | `admin.py` (routes) | Phases 1, 2, 6 |
| 8. Slack Messages | `messages.py` | — |
| 9. Workflow | `bug_investigation.py` | Phase 2 |
| 10. Frontend Types | `firstline-ai/src/types/bug.ts` | Phase 6 (API contract) |
| 11. Frontend Components | `InvestigationPanel.tsx`, `FollowupsPanel.tsx` | Phase 10 |
| 12. Frontend Mock Data | `mockClient.ts` | Phase 10 |

Phases 3, 4, 8, 10-12 are independent and can be done in parallel.

---

## Backward Compatibility

- `pr_url` (string) stays alongside `pr_urls` (JSONB list) in DB, schemas, and output
- Agent populates both: `pr_url` = first PR URL, `pr_urls` = all PRs
- All consumers (backend, Slack, frontend) check `pr_urls` first, fall back to `pr_url`
- Frontend derives display list: `const prUrls = data.pr_urls?.length ? data.pr_urls : data.pr_url ? [{ pr_url: data.pr_url }] : []`
- Future cleanup migration drops `pr_url` once all consumers are verified

---

## Verification

1. **Migration**: Run `alembic upgrade head`, verify columns exist with `\d investigations` and `\d investigation_followups`
2. **Multi-PR**: Trigger a bug report that spans two services (e.g., Payment + AFT). Verify the agent creates branches and PRs in both repos, and `pr_urls` contains both entries
3. **Code Review**: Trigger a code-fix investigation. Verify the agent invokes the code-reviewer subagent before committing. Check the conversation history for the subagent's review output
4. **Slack**: Verify the Slack messages in `#bug-reports` and `#bug-summaries` show all PR links
5. **API**: Call `GET /admin/bugs/{bug_id}/investigation` and verify `pr_urls` is populated
6. **Backward compat**: Verify that existing investigations with only `pr_url` still render correctly
7. **Frontend — single PR**: Open a bug detail with one PR, confirm the existing card renders unchanged
8. **Frontend — multi PR**: Open a bug detail with multiple PRs (use mock data), confirm all PRs render as a labeled list
9. **Frontend — no PR**: Open a bug detail with no PRs, confirm no PR section appears
