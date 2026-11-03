# DBZSCC 2611

DBZSCC 2611 is a local-first, single-user PySide6 desktop workspace centered on a chat and project view. It integrates the existing MIRO local model-discovery pipeline rather than implementing a second scanner or GGUF parser.

> **Reifegrad:** getesteter MVP-/Engineering-Slice, nicht produktionsreif als Gesamtprodukt. Die Vision-Abnahmekriterien und nächsten Slices stehen im [Produktreifebericht](doc/DBZSCC2611_PRODUCTION_READINESS.md).

## Run

On a Python 3.10+ environment with PySide6 installed, run from the checkout:

```powershell
$env:PYTHONPATH = "src"
python -m dbzscc
```

To install the project and its optional developer tools into an isolated environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m dbzscc
```

For the local LangChain workflow planner and the approved pytest/Ruff consumer, start the checkout with:

```powershell
.\run-dbzscc-2611.ps1
```

This launcher uses the isolated `.work/venv314` Python 3.14 environment and leaves `.venv` untouched. The LangChain/LangSmith stack in the current Python 3.15.2 Qt process fails during thread-pool initialization, so workflow planning is deliberately disabled there. If `.work/venv314` is absent, create it with Python 3.14 and install the optional dependencies there:

```powershell
py -3.14 -m venv .work\venv314
.\.work\venv314\Scripts\python.exe -m pip install -e ".[dev,workflows]"
```

The default chat endpoint is `http://127.0.0.1:8080/v1`, suitable for the app-managed local llama.cpp runtime or another OpenAI-compatible server. Nothing starts or probes automatically on app launch; on the Runtime page, an explicitly selected build/model can be started after resource preflight and separate confirmation, then health/model-checked and stopped as an app-owned process. Configure another OpenAI-compatible `/v1` endpoint in Settings if desired. An optional API key is read from the `DBZSCC_LLM_API_KEY` environment variable; it is never stored in application settings.

## Safety and current boundaries

- Model and runtime directory scans begin only after the user explicitly selects roots and presses the scan action.
- MIRO records filename-based candidates and bounded GGUF metadata; it does not execute runtime candidates, load models, download files, infer model quality, or verify inference capability.
- Runtime connectivity is tested only when requested. Normal chat uses OpenAI-compatible SSE streaming outside the GUI thread; the temporary answer is saved once only after `[DONE]`. Stop, stream errors and empty/incomplete responses discard the assistant draft while retaining the user's already-saved message. The separate file-diff workflow remains non-streaming and independent.
- Project browsing starts from an explicitly selected directory. Git branch, HEAD and status are read-only and refreshed only on request.
- The **Workflows** page accepts only a locally schema-validated action list. On Python 3.14, a second explicit approval can run only the fixed project-local `pytest` and Ruff commands; the app rechecks the project fingerprint and fresh resource plan, supervises the Windows process tree, and stores bounded, redacted output events. Tests can modify project files, and missing packages are not installed automatically.
- After a failed test, up to three targeted one-file patch cycles can be proposed; each requires explicit review/application approval and a separately confirmed pytest retest. Only the chosen file and bounded, redacted test output go to the verified loopback model. Manifest-bound PySide6-module and Uvicorn server/debug consumers also require a separate start confirmation, a fresh resource check and app-owned process supervision; a model suggestion alone never starts them.
- Conversations and MIRO registry data are stored under the user's application-data directory, not in the checkout.
- The separate **Dateiänderungen** page requires a freshly chosen working root and one existing file. It accepts only regular UTF-8 text up to 256 KiB; paths outside the root, symlinks/junctions, binary files and unsupported encodings are rejected.
- A change request sends that file's contents only after the user explicitly requests a proposal, and only after a successful check of a loopback runtime that reports the selected model ID. Model output must be strict JSON; the app derives and displays a unified diff and never executes model text, code or shell commands.
- Applying a proposal requires a separate confirmation after review. The app rechecks containment and the original SHA-256, then atomically replaces the same file. There is no automatic backup; use version control or another backup before approving consequential edits.
- The app-managed CPU build reports llama.cpp `8842 (455d8e4be)`; a real Qwen2.5-0.5B-Instruct-Q8_0 chat through the managed runtime returned `OK` over the UI's SSE path. Upstream origin is not cryptographically verified. Managed GPU startup and the separate file-diff/apply path remain unverified end-to-end; ordinary chat still never changes project files. See the [runtime integration status](doc/LLAMA_CPP_RUNTIME_INTEGRATION_STATUS.md).
- Chat can include a one-request context of up to 8 expressly chosen project files (32 KiB each, 128 KiB total). Hidden paths, `CodeSources`, paths outside the selected root, links/junctions/reparse points, unreadable files, invalid UTF-8 and binary data are rejected. The exact file list is visible and must be confirmed for each send; with file contents, only a configured loopback endpoint is accepted, proxies are disabled and redirects are blocked. The app stores only the user's ordinary question and writes no file content to its own logs; the local runtime's separate logging behavior is outside app control and has not been verified. Chat context alone cannot modify files; **Dateiänderungen** remains separate.
- Speech input is not implemented and remains a separate, planned local/offline feature slice with explicit push-to-talk; no audio is sent to a cloud service. The immediate overall priority is the shared foundation and evidence contract, followed by runtime/MIRO and code intelligence. This RAZOR environment has QtMultimedia capture APIs but no installed STT engine or local recognition model; see the [implementation and evidence notes](doc/DBZSCC2611_APP_IMPLEMENTATION.md) for feasible local backends.
- Agents, job orchestration, general-purpose tools/terminal execution, hybrid memory/RAG, general file attachments and multi-file editing are not implemented.

See [the implementation and evidence notes](doc/DBZSCC2611_APP_IMPLEMENTATION.md) for exact tests, architecture choices and open work, and the [production-readiness plan](doc/DBZSCC2611_PRODUCTION_READINESS.md) for system-wide gaps and acceptance criteria.
