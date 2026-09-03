# finder-demo — a read-only Finder clone built with NiceGUI

A demo of what "replace Finder" looks like on the stack we discussed:
**NiceGUI** (pure Python UI over FastAPI), no JavaScript, no build step.

![stack](https://img.shields.io/badge/python-3.9%2B-blue) — one dependency: `nicegui`

## Run

```bash
./run.sh
```

The script creates `.venv` on first run, installs dependencies, stops any
previous instance still on the port, starts the server, and opens
<http://localhost:8765> in your browser (macOS).

Manual equivalent:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Then open **http://localhost:8765**.

## What it does

- **Sidebar** with Finder-style favorites (Desktop, Documents, Downloads, Applications…),
  active-folder highlight, and a disk-usage meter.
- **Toolbar** with back/forward history, clickable breadcrumbs ("Macintosh HD → …"),
  live search in the current folder, sort selector, icon/list view toggle,
  show-hidden-files toggle.
- **Icon view** (tile grid) and **List view** (sortable table with kind/size/date).
- Click a folder → navigate. Click a file → **preview dialog**:
  images, video/audio, text & markdown (up to ~1 MB), plus metadata and an
  "Open with macOS" button (equivalent to double-clicking in Finder).
- **Status bar** with item count, bytes in files, and free disk space.
- **AI display names** (`summarizer.py`): the ✨ (auto_awesome) toolbar button
  has the LLM invent a short, sensible display name for every text file in the
  current folder and shows those names in place of the real ones — see below.
- Errors are handled: permission-denied folders show a lock card with a hint about
  macOS Full Disk Access.
- **LLM chat panel** (`llm_panel.py`): a streaming chat in a right-hand drawer
  (forum icon in the toolbar), powered by the framework-free LLM clients from
  the local [md_llm](https://github.com/you/md_llm) checkout — see below.

## LLM chat panel

The chat drawer is **agents-only** for now: it reuses md_llm's framework-free
`agents` / `sandbox` / `core` modules straight from `~/md_llm/src/md_llm`
through an import shim — no Streamlit is installed. Point `MD_LLM_SRC`
elsewhere if the checkout lives somewhere else. The host contract is
satisfied with `core.init()` (settings stay in memory), so managed agent
sandboxes land in `~/opencode-sandboxes` beside md_llm's own. Set
`FINDER_LLM_PROVIDER` to preselect a provider at startup; default `OpenCode`.
(md_llm's plain-API endpoints — Ollama / OpenRouter / OpenAI-compatible —
are not wired up; bring them back from git history if needed.)

**Agent CLIs** (one flattened prompt per run; tool activity streams inline as
`🛠` markers) — auth is each CLI's own:

- **OpenCode** — `opencode run --format json --auto`. ⟳ runs `opencode
  models`; discovery preselects the CLI's configured default (a sorted-first
  pick can land on an end-of-life catalog entry). The reasoning **variant**
  dropdown follows the chosen model and defaults to its highest effort.
- **Cline** — `cline --json`, tools auto-approved. Model "(default)" uses
  whatever `cline auth` configured (passing a model persists as Cline's new
  default — a CLI quirk surfaced in the tooltip); a `--thinking` level can
  be set per run. ⟳ fetches the public catalog (network).
- **ZCode** — `zcode --prompt … --json`, single-result (no stream). No
  per-run model flag: the picker rewrites ZCode's config via `set_zcode_model`
  — a **global** switch, like the TUI's `/model`.

Agent isolation follows md_llm: each chat gets a fresh managed sandbox
(age-GC'd; the drawer chip shows the path with a wipe button); an explicit
**Work dir** overrides it; the **Hardened sandbox** toggle (on by default on
macOS) confines the subprocess with a Seatbelt profile. Because agent
prompts travel as one argv string, attached documents are capped at 24 KB
(`AGENT_DOC_CAP`).

**Chat about this file**: text/markdown previews gain a button that attaches
the previewed text as leading context, mirroring md_llm's Reader→chat flow.
The chip above the chat shows the attached document and detaches it; **New
chat** clears the conversation but keeps the context. Replies stream
token-by-token into a markdown bubble (worker thread + `ui.timer`); failed
calls surface the provider's error message in the chat and don't pollute the
history. Chat state lives per browser tab (in memory) — a refresh starts a
new chat, matching the demo's stateless style.

## AI display names

The ✨ toolbar button turns `quarterly_budget_v3_FINAL.txt` into
"Quarterly budget report": it asks the LLM (the chat panel's current
provider/model) for a short name describing each file's content and displays
those names in the icon and list views. Clicking again toggles back to the
real names (hovering an AI name shows the real one as a tooltip).

- **One file at a time**: a linear progress bar in the footer fills file by
  file ("Summarizing 3/17 · notes.txt"); a stop button halts the run —
  everything already summarized is kept.
- **Cached in `~/.shangceng/filenames.json`**: every finished summary is
  saved immediately (atomic write), so re-runs and later visits are instant —
  only new or modified files (mtime/size changed) hit the LLM again.
- **Text formats only**: files whose extension is in the app's `TEXT_TYPES`
  set (`.txt`, `.md`, `.py`, `.json`, …) up to 1 MB are summarized; images,
  videos and other binaries keep their real names. Only the first 200 KB of
  a file is read, and the prompt carries the first 24 KB (the agent argv cap).
- **Privacy**: files whose names look like secrets (`.env`, `*secret*`,
  `*credential*`, `*password*`, `*token*`, `id_rsa`, `private_key`) are never
  sent to the LLM, even with "show hidden files" on. Jobs are capped at
  200 files per run.
- Failures are never cached, so the next run retries them. Set
  `SHANGCENG_HOME` to move the cache folder (tests use this).

### Testing

`test_llm_panel.py` covers the chat panel and `test_summarizer.py` the AI
names — both run the real app in-process against NiceGUI's official `User`
simulation, no browser needed. LLM streams are stubbed (at the `agents`
boundary for the chat, at `summarizer.make_stream_fn` for summaries);
`FINDER_TEST_REAL_AGENTS=1` additionally runs one REAL `opencode run`
end-to-end through the chat panel (spends CLI quota):

```bash
.venv/bin/pip install pytest pytest-asyncio   # test-only deps
.venv/bin/pytest test_llm_panel.py test_summarizer.py -v
```

Everything is **read-only** — no rename/move/delete anywhere. (The agent
CLIs, by design, can write inside their sandbox.)

## Known demo shortcuts

- Search filters the current folder only (not recursive).
- Folders show `--` for size (no recursive du).
- The app serves `/` as static files (`/files/...`) so previews can load local
  images — fine for localhost, **not** for exposing to a network.
- macOS may ask for Full Disk Access for the terminal running the server
  before Desktop/Documents can be listed.

## Next steps if you take it further

- Recursive search and folder sizes (background tasks / a worker thread).
- Right-click context menus, rename/move (drop the read-only constraint).
- Live updates with `watchdog`.
- Package as a desktop app with `ui.run(native=True)` (pywebview).
