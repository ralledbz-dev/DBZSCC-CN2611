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

- By default, MIRO refreshes configured model roots in the background at app startup; when no model-root preference is saved, the default is `D:\Models`. Startup refresh can be disabled in Settings. Runtime-root scans remain explicit: select a root and use the scan action.
- MIRO records filename-based candidates and bounded GGUF metadata; it does not execute runtime candidates, load models, download files, infer model quality, or verify inference capability.
- Runtime connectivity is tested only when requested. Normal chat uses OpenAI-compatible SSE streaming outside the GUI thread; the temporary answer is saved once only after `[DONE]`. Stop, stream errors and empty/incomplete responses discard the assistant draft while retaining the user's already-saved message. The separate file-diff workflow remains non-streaming and independent.
- Project browsing starts from an explicitly selected directory. Git branch, HEAD and status are read-only and refreshed only on request.
- The **Workflows** page accepts only a locally schema-validated action list. On Python 3.14, a second explicit approval can run only the fixed project-local `pytest` and Ruff commands; the app rechecks the project fingerprint and fresh resource plan, supervises the Windows process tree, and stores bounded, redacted console/test events under one persistent run ID with step/state, project-root/fingerprint and MIRO model evidence. Tests can modify project files, and missing packages are not installed automatically.
- After a failed test, the same run continues iteratively until tests pass, the user cancels, required model/registry/project/file evidence changes, no valid safe patch is available, or the same redacted failure or an already-applied file content repeats. There is no ordinary three-patch cap. Each targeted one-file diff stays visible and requires its own review and Apply approval; after Apply, only the matching manifest-bound pytest retest starts automatically without a second run confirmation. Only the chosen file and bounded, redacted test output go to the verified loopback model. Manifest-bound PySide6-module and Uvicorn server/debug consumers also require a separate start confirmation, a fresh resource check and app-owned process supervision; a model suggestion alone never starts them.
- Conversations and MIRO registry data are stored under the user's application-data directory, not in the checkout.
- The separate **Dateiänderungen** page requires a freshly chosen working root and one existing file. It accepts only regular UTF-8 text up to 256 KiB; paths outside the root, symlinks/junctions, binary files and unsupported encodings are rejected.
- A change request sends that file's contents only after the user explicitly requests a proposal, and only after a successful check of a loopback runtime that reports the selected model ID. Model output must be strict JSON; the app derives and displays a unified diff and never executes model text, code or shell commands.
- Applying a proposal requires a separate confirmation after review. The app rechecks containment and the original SHA-256, then atomically replaces the same file. There is no automatic backup; use version control or another backup before approving consequential edits.
- The app-managed CPU build reports llama.cpp `8842 (455d8e4be)`; a real Qwen2.5-0.5B-Instruct-Q8_0 chat through the managed runtime returned `OK` over the UI's SSE path. Upstream origin is not cryptographically verified. Managed GPU startup and the separate file-diff/apply path remain unverified end-to-end; ordinary chat still never changes project files. See the [runtime integration status](doc/LLAMA_CPP_RUNTIME_INTEGRATION_STATUS.md).
- Chat can include a one-request context of up to 8 expressly chosen project files (32 KiB each, 128 KiB total). Hidden paths, `CodeSources`, paths outside the selected root, links/junctions/reparse points, unreadable files, invalid UTF-8 and binary data are rejected. The exact file list is visible and must be confirmed for each send; with file contents, only a configured loopback endpoint is accepted, proxies are disabled and redirects are blocked. The app stores only the user's ordinary question and writes no file content to its own logs; the local runtime's separate logging behavior is outside app control and has not been verified. Chat context alone cannot modify project files; **Dateiänderungen** remains separate.
- Experimental local RAG can manually index only the expressly selected project, after a separate confirmation, and use at most five source/line-tagged CPU E5 excerpts for one confirmed next question. In development it uses an isolated checkout-local Python 3.14 worker because the main Python 3.15 environment has no compatible ONNX Runtime wheel; a standalone bundle has a defined sidecar path, but no installer artifact has been built or qualified end-to-end. There is no background scan or cloud fallback. See the [offline RAG baseline](doc/DBZSCC2611_OFFLINE_RAG_CPU_BASELINE.md) and [app implementation evidence](doc/DBZSCC2611_APP_IMPLEMENTATION.md).
- Speech input now provides explicit push-to-talk, a cancellable local CPU recognizer and an editable transcript preview that is never auto-sent. The pinned whisper.cpp runtime and multilingual German-capable model are downloaded into the project only after the user clicks the setup button. See the [offline speech-input guide](doc/DBZSCC2611_OFFLINE_SPEECH_INPUT.md) and [implementation/evidence notes](doc/DBZSCC2611_APP_IMPLEMENTATION.md).
- Agents, job orchestration, general-purpose tools/terminal execution, hybrid/agent memory, general file attachments and multi-file editing are not implemented. The RAG CPU path remains an experimental local baseline, not a general memory feature or a quality-qualified release.

See [the implementation and evidence notes](doc/DBZSCC2611_APP_IMPLEMENTATION.md) for exact tests, architecture choices and open work, and the [production-readiness plan](doc/DBZSCC2611_PRODUCTION_READINESS.md) for system-wide gaps and acceptance criteria.
