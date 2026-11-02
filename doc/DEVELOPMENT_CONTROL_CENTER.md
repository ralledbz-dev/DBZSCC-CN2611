# DBZSCC Development Control Center — Project Memory, Execution & Observer

**Status:** Persistence/service foundation and terminal-completion event wiring implemented; Control Center UI and broader workflow coverage remain pending
**Date:** 2026-10-25
**Target:** DBZSCC !24/10! — `codex/ais2-build-readiness`
**Purpose:** Make the full idea-to-product development loop visible, traceable, inspectable, and safely executable from DBZSCC.

## 1. Product decision

The governing workspace-wide consistency principle is defined in [WORKSPACE_WIDE_SINGLE_SOURCE_OF_TRUTH.md](WORKSPACE_WIDE_SINGLE_SOURCE_OF_TRUTH.md). This Control Center is a view and operator surface over that canonical state—not a separate source of truth.


DBZSCC should have a dedicated **Development Control Center** tab and an independently movable **Project Observer** window.

The tab is the operational cockpit. The Observer is a read-mostly live projection of the same underlying project state. They must not maintain separate task lists or competing versions of the truth.

### Implementation checkpoint (2026-10-25)

A first code slice now exists on this branch: `WorkspaceDB` persists structured project events in a dedicated table with event IDs, source, payload, timestamp, optional project/workspace scope, and correlation ID. `ProjectEventService` provides the application entry point, `EventBus` now lives in `local_llm_chat/event_bus.py`, and `publish_structured(...)` persists before notifying UI listeners. Project creation and project selection emit durable events. Terminal process completion is now exposed through `TerminalWidget.result_ready` and connected to `record_terminal_result(...)` in `local_llm_chat/execution_evidence.py`. Only process metadata (exit code, success, duration, actual CWD and executable) is persisted; raw command text and stdout/stderr are excluded because they may contain secrets. Job-spooler step start, step completion and job completion also emit durable events with IDs, indexes and status only; model responses, step summaries and tool output are not copied into the event payload. Existing `publish(str)` remains ephemeral and is explicitly not test evidence. Focused service, event-bus, and execution-evidence tests were added. These tests have **not yet been executed locally**; broader workflow coverage and the Control Center UI remain pending. The event stream is a foundation, not yet a complete workspace-wide source of truth.

This capability is a first-class part of making DBZSCC professionally capable. It is not merely a dashboard and must not be a one-off AIS2 implementation.

## 2. One project graph, multiple views

The core is a traceable project graph backed by stable IDs and typed relationships, not a collection of manually duplicated Markdown files.

Entities:
- `Project`: canonical repo identity, workspace path, default branch, remotes, active branch and current HEAD.
- `ConversationReference`: source conversation or imported transcript reference, timestamp, source, related project and relevant excerpt/summary. Imported material must retain provenance and must not be silently presented as complete history if only a subset is available.
- `Decision`: question, decision, rationale, author, timestamp, status and supporting evidence.
- `Vision`, `Requirement`, `Task`, `Milestone`: stable IDs, owner/status, dependencies, acceptance criteria and links to parent artifacts.
- `Artifact`: path, type, version/hash, generated-from IDs and last freshness evaluation.
- `RepositorySnapshot`: provider, repository identity, branch, commit SHA, dirty state and observation timestamp.
- `PullRequest`: provider, number/URL, base/head, review/draft/merge state and observed head SHA.
- `ExecutionRun`: exact command, executable, working directory, selected environment profile, start/end, exit code, stdout/stderr artifact references, cancellation and result status.
- `TestEvidence`: test command, commit SHA, environment, counts, failures, report paths and timestamp.
- `SyncOperation`: source/destination refs, preflight snapshot, intended action, diff summary, outcome and rollback/recovery information.
- `Event`: append-only timeline item with event ID, correlation ID, actor, timestamp, entity references, severity and structured payload.

Every material claim should be classed as a user decision, repository observation, command/test evidence, external source, heuristic, or model inference.

## 3. Dedicated tab — compact operator layout

The tab should use a compact, low-clutter layout with a shared project selector and clear health/status strip. Avoid deeply nested card rectangles.

### Overview
- Active project/repo, canonical workspace, branch and HEAD.
- Dirty/clean state and remotes; last observed sync status.
- Current milestone, next action, blockers and latest verified commit.
- Distinguish observed data from stale or unavailable provider data.

### Project memory
- Searchable decision/assumption log and linked conversation references.
- Imported conversation/plan material with source provenance and coverage notes.
- Links from a decision to affected requirements, tasks, docs, PRs and test evidence.
- Never imply that the entire ChatGPT conversation history has been imported unless that is demonstrably true.

### GitHub / Gitea / local repositories
- Repository identity, remotes, branches, commit graph, working-tree status, PRs/issues/reviews and Actions/workflow runs.
- Local Git is authoritative for local uncommitted state; remote API observations are timestamped snapshots.
- Show remote/provider unavailability rather than synthesizing a status.
- Actions that write, push, create PRs, close PRs or merge require explicit confirmation and a visible preflight diff. Never merge autonomously.

### PowerShell / execution
- Allowlisted and reviewable command plans, with exact executable and CWD shown before execution.
- Capture the actual child-process CWD/executable, command, start/end, exit code, stdout/stderr, cancellation and generated artifacts.
- Default to non-destructive checks. Require a second confirmation for destructive or broad-impact commands.
- Secrets are passed through environment variables; never render secret values in logs or persist them in run records.
- Execution from DBZSCC must be explicitly enabled/configured for the local machine; remote UI access must not imply remote shell access.

### Tests / evidence
- Run focused tests, full test suites, lint/type checks and build commands as separate named jobs.
- Attach every result to the exact repository, commit SHA, command, interpreter/runtime and environment.
- A changed HEAD or dirty working tree invalidates the claim that earlier test evidence proves the current code.
- A test result is not a commit by itself; committing evidence requires review and explicit approval.

### Sync / release
- Compare local workspace, GitHub and Gitea by repository identity, branch, commit SHA and dirty state.
- Show ahead/behind, divergence, uncommitted changes, missing remotes and conflicts before proposing sync.
- Present a plan and expected diff before pull/push/mirror operations. Never overwrite divergent work silently.
- Separate mirror sync from CI/CD execution; both get individual outcomes and logs.

## 4. Independent Project Observer

A separate movable/resizable window reads the same project graph/event stream as the tab. It is a live projection, not a second controller.

Default timeline/board columns:
- **Planning** — vision, requirements, architecture and milestones in preparation.
- **Todo** — approved work ready to start.
- **Pending** — waiting on a user decision, dependency, provider or external response.
- **In Progress** — work with a recorded owner/run.
- **Blocked** — an explicit blocker and reason.
- **Review** — output awaits validation/approval.
- **Done** — acceptance criteria met with evidence.
- **Failed** — an attempted action/test failed and needs triage.

The Observer should offer:
- Timeline of status changes with correlation IDs.
- Task dependency graph and milestone progress.
- Recent executions, test evidence, PR/review states and sync discrepancies.
- Stale-document and missing-evidence warnings.
- Filters by project, milestone, repo, task, status and time.
- Click-through from a task to its plan, changed files, commit, PR, test result and docs.

A task may not be marked Done solely because a model says it is done. Done requires its declared acceptance criteria and the relevant evidence, or an explicitly recorded manual exception.

## 5. Documentation freshness and provenance

Markdown remains a useful export/presentation format, not an independent competing source of truth.

- Keep structured canonical state for IDs, statuses, links and evidence.
- Generate or update `PROJECT_STATUS.md`, `TODO.md`, `HANDOVER.md`, relevant plans and verification summaries from that state where possible.
- Preserve human-authored sections; generated sections must be clearly delimited and safely replaceable.
- Each generated document records the source state/version and generation time.
- Report drift when docs were manually changed or source state changed since generation.
- Never silently overwrite user-authored content.
- Update docs in the same review cycle as a material code change; show exactly which docs are proposed for update.

## 6. Learning loop without unsafe self-modification

The loop is:

1. Observe a source-controlled workspace, provider snapshot, user decision or execution result.
2. Correlate the observation with a task/requirement and the exact commit.
3. Detect drift, failed acceptance criteria, stale docs, repeated errors or missing evidence.
4. Propose a diagnosis and ranked corrective plan with evidence and uncertainty.
5. Show a reviewable diff/command plan and request approval where required.
6. Execute the approved change, test it, and record results.
7. Update project state and propose documentation updates.
8. Compare actual results with acceptance criteria; reopen or block tasks when evidence fails.

No hidden self-modification, unreviewed push, automatic merge, or success claims without evidence. Learning means durable, source-backed project understanding and measured feedback—not unrestricted autonomy.

## 7. Integration boundaries

- Use existing `WorkspaceDB` for current project/session/job/message persistence after schema review; do not create a parallel database without a migration decision.
- Reuse the existing Git/Gitea clients, terminal execution layer, job spooler, event bus and test infrastructure where their contracts fit.
- Extend current execution events with stable event/correlation IDs rather than adding a disconnected logging system.
- Keep local execution, GitHub API access, Gitea API access and CI runners as separate capability providers with explicit health/configuration.
- Keep secrets in environment variables, following the project's existing policy.
- The Observer reads shared structured state and events; it does not independently run commands or mutate task status.
- Keep DBZSCC and AIS2 workspaces, repos, PRs and release lifecycles strictly separate.

## 8. Suggested implementation sequence

### Slice 1 — Read-only inventory and evidence model
- Define typed records and status transitions for repository snapshots, tasks, execution runs and test evidence.
- Build a read-only overview for current workspace, Git status and known remotes.
- Add focused unit tests for identity, state transitions, stale evidence and redaction.

### Slice 2 — Execution and test evidence
- Add an execution-run registry and event correlation IDs to the existing terminal/job layer.
- Store exact CWD, executable, command, exit code and artifact references.
- Add focused test runner actions with explicit confirmation and commit binding.

### Slice 3 — GitHub/Gitea PR and workflow snapshots
- Integrate provider observations with timestamps and stable external IDs.
- Surface PR state, CI run results, and differences between local and remote refs.
- Keep write actions disabled until their preflight/approval path is tested.

### Slice 4 — Sync planner
- Implement read-only local/GitHub/Gitea comparison first.
- Add dry-run sync plans and conflict handling.
- Only then add approved push/mirror actions and record their results.

### Slice 5 — Project Observer
- Build an independent Qt window driven by the shared state/event model.
- Add status columns, dependency links, evidence drill-down and stale-doc alerts.

### Slice 6 — Documentation generator and feedback loop
- Generate safe, delimited status sections from canonical state.
- Detect drift and missing evidence; propose corrective actions.
- Require review/approval before code changes or high-impact commands.

## 9. Acceptance criteria

- A user can identify the exact active project, workspace, branch, HEAD and remotes at a glance.
- Local/remote provider states show source and last-observed time; unavailable is never shown as healthy.
- Every execution/test record captures exact command, CWD, executable, commit binding and exit code.
- Test results are never carried forward as proof for a different commit.
- PRs and CI runs link back to the repository/task that produced them.
- Sync preview exposes divergence and destructive impact before action.
- The Observer updates from shared state without duplicating or inventing task state.
- Every Done task has acceptance evidence or a documented exception.
- Generated docs preserve manual sections and report stale state.
- No secret values are persisted or displayed.
- Destructive operations, push, merge and project creation use explicit approval gates.
- Automated tests cover core state transitions, evidence freshness, failure paths and secret redaction.

## 10. Non-goals for the first slice

- No autonomous merge or push.
- No implicit import claim for complete prior ChatGPT history.
- No remote PowerShell execution by default.
- No replacement of Git, GitHub or Gitea as source-control authorities.
- No second, independent task database or status board.
- No claim that AIS2 already has implementation code.
