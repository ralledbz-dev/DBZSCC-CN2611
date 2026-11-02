# DBZSCC 2611

DBZSCC 2611 is a local-first, single-user PySide6 desktop workspace centered on a chat and project view. It integrates the existing MIRO local model-discovery pipeline rather than implementing a second scanner or GGUF parser.

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

The default chat endpoint is `http://127.0.0.1:8080/v1`, suitable for an explicitly started local llama.cpp-compatible server. The app does not start or probe that server automatically. Configure another OpenAI-compatible `/v1` endpoint in Settings if desired. An optional API key is read from the `DBZSCC_LLM_API_KEY` environment variable; it is never stored in application settings.

## Safety and current boundaries

- Model and runtime directory scans begin only after the user explicitly selects roots and presses the scan action.
- MIRO records filename-based candidates and bounded GGUF metadata; it does not execute runtime candidates, load models, download files, infer model quality, or verify inference capability.
- Runtime connectivity is tested only when requested. Normal chat uses OpenAI-compatible SSE streaming outside the GUI thread; the temporary answer is saved once only after `[DONE]`. Stop, stream errors and empty/incomplete responses discard the assistant draft while retaining the user's already-saved message. The separate file-diff workflow remains non-streaming and independent.
- Project browsing starts from an explicitly selected directory. Git branch, HEAD and status are read-only and refreshed only on request.
- Conversations and MIRO registry data are stored under the user's application-data directory, not in the checkout.
- The separate **Dateiänderungen** page requires a freshly chosen working root and one existing file. It accepts only regular UTF-8 text up to 256 KiB; paths outside the root, symlinks/junctions, binary files and unsupported encodings are rejected.
- A change request sends that file's contents only after the user explicitly requests a proposal, and only after a successful check of a loopback runtime that reports the selected model ID. Model output must be strict JSON; the app derives and displays a unified diff and never executes model text, code or shell commands.
- Applying a proposal requires a separate confirmation after review. The app rechecks containment and the original SHA-256, then atomically replaces the same file. There is no automatic backup; use version control or another backup before approving consequential edits.
- No real local runtime or inference was verified in the current checkout, so file-change requests remain unavailable until the user starts and manually checks a local runtime. Sending an ordinary chat message never changes project files.
- Speech input is not implemented. The next prioritized slice is explicit push-to-talk with local/offline recognition only; no audio is sent to a cloud service. This RAZOR environment has QtMultimedia capture APIs but no installed STT engine or local recognition model; see the [implementation and evidence notes](doc/DBZSCC2611_APP_IMPLEMENTATION.md) for feasible local backends and the exact next integration step.
- Agents, job orchestration, tools/terminal execution, shared memory, multi-file editing and RAG are not implemented in this application slice.

See [the implementation and evidence notes](doc/DBZSCC2611_APP_IMPLEMENTATION.md) for exact tests, architecture choices and open work.
