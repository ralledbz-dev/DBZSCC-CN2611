# DBZSCC Architecture Guardrails

**Status:** Binding engineering rule for new work and touched code
**Scope:** DBZS-CODING-CHAT / DBZSCC CODENAME 2611
**Applies to:** Every feature, bug fix, refactor, agent, worker, service, automation, and UI change

## 1. Core rule: improve the touched area

Whenever a change requires editing `app.py` or another module, inspect the touched responsibility. If it contains business logic, persistence logic, orchestration, blocking work, or provider-specific integration that belongs elsewhere, extract it as part of the same change when that can be done safely.

Do not create a broad refactor merely because a file was touched. If extraction is too risky for one change, record a small, explicit follow-up with the reason and boundary.

## 2. Responsibility boundaries

- **UI / `app.py`:** presentation, user interaction, wiring, and delegation. Keep application startup and composition understandable. Do not add new complex business workflows directly to UI classes.
- **Services:** deterministic domain operations, validation, coordination, and explicit application use cases.
- **Workers:** long-running or blocking work off the UI thread. Return structured outcomes; do not silently mutate unrelated UI state.
- **Agents:** bounded reasoning and task execution through the same approved services and contracts as other callers. An agent is not a bypass around safety or architecture.
- **Automations:** trigger and schedule existing use cases through the same service entry points. Avoid a second implementation of business rules.
- **Persistence adapters:** own database/file/provider access behind clear contracts. Reuse existing storage where appropriate; do not introduce competing state stores without an explicit migration decision.
- **Events:** communicate observable state changes with structured, versionable payloads and traceable correlation identifiers. UI notifications are not a substitute for durable evidence.
- **Tests:** exercise service behavior independently, then verify integration and UI wiring at the appropriate level.

## 3. Workspace-wide single source of truth

Every feature must respect `docs/WORKSPACE_WIDE_SINGLE_SOURCE_OF_TRUTH.md`.

- Preserve provenance, timestamps, stable IDs, and source authority where relevant.
- Treat remote provider status as a timestamped observation, not as unquestionable canonical intent.
- Tie execution and test evidence to the command, environment, workspace/revision, exit status, and relevant logs when available.
- Treat generated documentation and Observer views as projections of canonical state, not competing authorities.
- Make conflicts and stale data visible; do not silently overwrite them.
- Keep secrets in environment variables or established external credential mechanisms; never store secret values in project state, events, logs, or generated documentation.

## 4. Safe execution and approvals

- Do not silently push, merge, delete, overwrite user work, or run destructive commands.
- Keep explicit approvals for consequential actions and preserve reviewable diffs.
- Make retries idempotent where possible; use event IDs or operation IDs to prevent duplicate work.
- A task is `Done` only when its acceptance evidence exists or an explicitly approved exception is recorded.
- Never claim a test passed unless its actual result was captured.

## 5. Required checklist for changes

For each meaningful code change, verify:

1. Is the responsibility in the correct layer?
2. Can the business behavior be tested without launching the full UI?
3. Does this reuse existing services, storage, contracts, and event paths?
4. Are long-running/blocking operations outside the UI thread?
5. Are failures, cancellation, and partial results represented explicitly?
6. Are state changes and execution evidence traceable?
7. Are tests added or updated and actually run where the environment permits?
8. Does documentation accurately distinguish implemented behavior from planned behavior?
9. Did the change make the touched module easier to understand—or is a concrete follow-up recorded?

## 6. Incremental refactoring policy

Refactor along the active change boundary, in small reviewable steps:

1. Inspect callers, dependencies, and existing tests.
2. Define or reuse a contract before moving behavior.
3. Extract one coherent responsibility.
4. Update callers to delegate through the contract.
5. Add focused unit tests and integration coverage.
6. Run relevant tests and record the real output.
7. Review the diff for accidental behavior changes.
8. Document any remaining architectural debt with a specific reason and next step.

Avoid speculative layers, one-class-per-function overengineering, and parallel abstractions that do not have a demonstrated use case.

## 7. Definition of done

A change is not complete merely because code was committed. Its completion report must distinguish:
- code changed;
- tests authored;
- tests actually executed and their captured outcome;
- checks not run and why;
- known risks or follow-up work;
- commit/branch/PR references when available.

This policy applies to DBZSCC itself, including its self-development workflow. The system must follow the same architectural rules when improving its own code.
