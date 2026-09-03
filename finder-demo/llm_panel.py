"""LLM chat panel for the Finder demo, powered by md_llm's stdlib clients.

The panel lives in a right-hand drawer: pick a provider, list/pick a model,
and chat — replies stream token-by-token. The preview dialog's "Chat about
this file" button attaches the previewed text as leading context, mirroring
md_llm's Reader→chat flow.

The providers are the agent CLIs from ``md_llm.agents`` — OpenCode, Cline
and ZCode: coding agents that run tools (bash/read/edit/…) in a working
directory. They take ONE flattened prompt per run (no message array), so the
context + Q&A history are rendered as labelled User/Assistant blocks
(md_llm's ``_turns_to_opencode_prompt`` shape) and the document is capped
(agent prompts travel as one argv string, so only the first ``AGENT_DOC_CAP``
characters are included). Tool activity arrives inline as one-line markers,
which the markdown bubble renders fine. (md_llm's plain-API endpoints —
Ollama / OpenRouter / OpenAI-compatible — are deliberately not wired up for
now; the panel is agents-only.)

No sandbox: the agent CLIs run unconfined in the server's current working
directory (shown in the drawer). That's a supported md_llm path, not a fork —
its chat-stream APIs take ``workdir``/``hardened`` parameters and the panel
just passes the cwd with Seatbelt off, so md_llm itself stays untouched and
no sandbox is ever created.
ZCode has no per-run model flag — its picker rewrites ZCode's own config via
``set_zcode_model`` (a GLOBAL switch, like the TUI's /model).

md_llm is imported from its checkout without dragging in Streamlit: the
package __init__ pulls in the Streamlit panels, so only the framework-free
modules (agents, core) are loaded from the source tree through
a namespace shim, and the host contract is satisfied with ``core.init()``
(settings stay in memory — this demo never writes md_llm's settings file).
Set ``MD_LLM_SRC`` to point somewhere other than ``~/md_llm/src/md_llm``.
"""
from __future__ import annotations

import importlib
import os
import sys
import threading
import types
from pathlib import Path

from nicegui import run, ui

DEFAULT_MD_LLM_SRC = Path.home() / 'md_llm' / 'src' / 'md_llm'

PROVIDERS = ['OpenCode', 'Cline', 'ZCode']
AGENT_PROVIDERS = {'OpenCode', 'Cline', 'ZCode'}

CONTEXT_PREAMBLE = (
    'You are a helpful assistant embedded in a file browser. The user has the '
    'following document open and will ask questions about it.\n'
    'Document: {name}\n\n'
)

# Agents get the document as part of one argv-borne prompt, so it is capped
# well under the OS argv limit (API providers keep the full 200 KB preview).
AGENT_DOC_CAP = 24_000

MD_LLM_BASE_DIR = Path.home() / '.md_llm'   # md_llm data root (host contract only)


def _load_md_llm():
    """Import md_llm's framework-free modules from source, bypassing Streamlit.

    md_llm/__init__.py imports the Streamlit panels, so mount its source
    directory as a bare package namespace and import only ``core`` (the host
    contract) and ``agents`` (agent CLIs) — neither imports Streamlit.
    (``agents`` pulls in md_llm's ``sandbox`` module for its Seatbelt helper,
    but with ``hardened=False`` nothing ever creates a sandbox or profile.)
    """
    src = Path(os.environ.get('MD_LLM_SRC') or DEFAULT_MD_LLM_SRC).expanduser()
    if not (src / 'agents.py').is_file():
        raise ImportError(
            f'md_llm sources not found at {src} — set MD_LLM_SRC to the '
            "cloned repo's src/md_llm directory"
        )
    name = '_md_llm_src'
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(src)]
        sys.modules[name] = package
    core = importlib.import_module(f'{name}.core')
    # Host contract: settings resolve against Core. Settings stay
    # in memory (settings_path=None) — this demo never persists md_llm state.
    core.init(core.Core(
        base_dir=str(MD_LLM_BASE_DIR),
        markdown_dirs=(str(MD_LLM_BASE_DIR / 'uploads'),),
        chat_save_dir=str(MD_LLM_BASE_DIR / 'uploads'),
    ))
    return importlib.import_module(f'{name}.agents')


agents = _load_md_llm()

# Handle on the most recently created panel (one per browser connection) —
# lets tests reach the server-side instance without a global-variable relay.
last_panel: ChatPanel | None = None


class ChatPanel:
    """A streaming LLM chat in a right drawer — one instance per page."""

    def __init__(self):
        # ---- state --------------------------------------------------------
        self.provider = os.environ.get('FINDER_LLM_PROVIDER', 'OpenCode')
        self.messages: list[dict] = []          # {"role","content"} sent to the LLM
        self.context_name: str | None = None    # attached document, if any
        self.context_text: str | None = None    # its text (agents send a capped copy)
        self._stream: dict | None = None        # in-flight stream (see _send)
        self._models_loaded: set[str] = set()   # providers whose catalog was fetched
        self._opencode_details: dict = {}       # model → metadata (variants)

        # ---- drawer & layout ----------------------------------------------
        with ui.right_drawer(value=False, bordered=True).props('width=440') as self.drawer:
            with ui.column().classes('w-full h-full p-3 gap-2'):
                with ui.expansion('Provider & model', icon='tune', value=True) \
                        .classes('w-full').props('dense header-class="text-[13px] text-gray-600"'):
                    self._build_settings()

                self.workdir_row = ui.row() \
                    .classes('w-full items-center gap-1 no-wrap bg-blue-50 '
                             'border border-blue-200 rounded-lg px-2 py-1')

                self.context_row = ui.row() \
                    .classes('w-full items-center gap-1 no-wrap bg-amber-50 '
                             'border border-amber-200 rounded-lg px-2 py-1')

                self.chat_scroll = ui.scroll_area().style('flex: 1 1 0; min-height: 0') \
                    .classes('w-full')
                with self.chat_scroll:
                    self.msg_col = ui.column().classes('w-full gap-2 p-1')

                with ui.row().classes('w-full items-center gap-1 no-wrap'):
                    ui.button(icon='add_comment', on_click=self.new_chat) \
                        .props('flat round dense') \
                        .tooltip('New chat (keeps the attached document)').mark('llm-new-chat')
                    self.input = ui.input(placeholder='Ask about the open document…') \
                        .props('dense outlined rounded-lg') \
                        .classes('flex-1').mark('llm-chat-input')
                    self.input.on('keydown.enter', self._send)
                    self.send_btn = ui.button(icon='send', on_click=self._send) \
                        .props('unelevated round dense color=primary').tooltip('Send') \
                        .mark('llm-send')
                    self.stop_btn = ui.button(icon='stop_circle', on_click=self._stop) \
                        .props('flat round dense color=negative').tooltip('Stop generating') \
                        .classes('hidden').mark('llm-stop')
                self.status = ui.label('').classes('text-[11px] text-gray-400 px-1 break-all') \
                    .mark('llm-status')

        self._tick_timer = ui.timer(0.15, self._tick, active=False)
        self._render_workdir_row()
        self._render_context()
        self._update_status()
        global last_panel
        last_panel = self

    # ---- settings UI -------------------------------------------------------

    def _build_settings(self):
        self.provider_sel = ui.select(
            PROVIDERS, value=self.provider, on_change=self._on_provider_change,
            label='Provider').props('dense outlined').classes('w-full').mark('llm-provider')

        with ui.column().classes('w-full gap-1') as self.opencode_box:
            with ui.row().classes('w-full items-center gap-1 no-wrap'):
                self.opencode_model = ui.select([], label='Model',
                                                on_change=self._on_opencode_model_change) \
                    .props('dense outlined').classes('flex-1').mark('llm-oc-model')
                ui.button(icon='refresh', on_click=lambda: self._discover('OpenCode')) \
                    .props('flat dense round').tooltip('Run `opencode models`')
            self.opencode_variant = ui.select([], label='Reasoning variant (auto: highest)') \
                .props('dense outlined').classes('w-full').mark('llm-oc-variant')

        with ui.column().classes('w-full gap-1') as self.cline_box:
            with ui.row().classes('w-full items-center gap-1 no-wrap'):
                self.cline_model = ui.select(
                    {'': '(default — via cline auth)'}, label='Model') \
                    .props('dense outlined').classes('flex-1').mark('llm-cline-model') \
                    .tooltip('Picking a model persists it as Cline\'s new default')
                ui.button(icon='refresh', on_click=lambda: self._discover('Cline')) \
                    .props('flat dense round').tooltip('Fetch the model catalog (network)')
            self.cline_thinking = ui.select(
                {'': '(default)', **{lv: lv for lv in agents.CLINE_THINKING_LEVELS}},
                value='', label='Thinking level') \
                .props('dense outlined').classes('w-full').mark('llm-cline-thinking')

        with ui.column().classes('w-full gap-1') as self.zcode_box:
            with ui.row().classes('w-full items-center gap-1 no-wrap'):
                self.zcode_model = ui.select({}, label='Model (from zcode config)',
                                             on_change=self._on_zcode_model_change) \
                    .props('dense outlined').classes('flex-1').mark('llm-zcode-model') \
                    .tooltip('Switching rewrites ZCode\'s config — a global change')
                ui.button(icon='refresh', on_click=lambda: self._discover('ZCode')) \
                    .props('flat dense round').tooltip('Re-read zcode config')

        self._show_provider_boxes()

    def _show_provider_boxes(self):
        self.opencode_box.set_visibility(self.provider == 'OpenCode')
        self.cline_box.set_visibility(self.provider == 'Cline')
        self.zcode_box.set_visibility(self.provider == 'ZCode')

    async def _on_provider_change(self, e):
        self.provider = e.value
        self._show_provider_boxes()
        self._render_workdir_row()
        self._update_status()
        auto = {'OpenCode', 'ZCode'}  # Cline's catalog is a network fetch
        if self.provider in auto and self.provider not in self._models_loaded:
            await self._discover(self.provider)

    async def _discover(self, provider: str):
        """Fetch the model catalog for one provider into its select (threaded)."""
        if provider == 'OpenCode':
            details = await run.io_bound(agents.list_opencode_model_details)
            self._opencode_details = details
            models = sorted(details) or await run.io_bound(agents.list_opencode_models)
            self.opencode_model.options = models or []
            if models:
                # prefer the CLI's own configured default (what `opencode run`
                # would use); sorted-first can land on an end-of-life catalog entry
                default = agents.read_opencode_default_model()
                self.opencode_model.value = default if default in models else models[0]
                self._refresh_opencode_variants(self.opencode_model.value)
        elif provider == 'Cline':
            models = await run.io_bound(agents.list_cline_models)
            self.cline_model.options = {'': '(default — via cline auth)',
                                        **{m: m for m in models}}
            self.cline_model.value = ''
        elif provider == 'ZCode':
            refs = await run.io_bound(agents.list_zcode_model_refs)
            current = agents.read_zcode_model()
            options = {r: r for r in refs}
            if current and current not in options:
                options[current] = f'{current} (current)'
            self.zcode_model.options = options
            if current:
                self.zcode_model.value = current
        else:
            return
        self._models_loaded.add(provider)
        empty_hint = {
            'OpenCode': 'Is the opencode CLI installed and on PATH?',
            'Cline': 'Could not fetch the Cline catalog.',
            'ZCode': 'Could not read the zcode config.',
        }
        options = self._provider_select(provider).options
        if provider != 'Cline' and not options:  # Cline always has "(default)"
            ui.notify(f'No models found — {empty_hint[provider]}', type='warning')
        self._update_status()

    def _provider_select(self, provider: str):
        return {'OpenCode': self.opencode_model, 'Cline': self.cline_model,
                'ZCode': self.zcode_model}[provider]

    def _refresh_opencode_variants(self, model: str):
        """Set the variant dropdown to the model's efforts, highest preselected."""
        variants = agents.opencode_variants_for(self._opencode_details, model)
        ordered = agents.order_opencode_variants(variants)
        self.opencode_variant.options = ordered
        self.opencode_variant.value = agents.highest_opencode_variant(variants) or None
        self.opencode_variant.set_enabled(bool(ordered))

    def _on_opencode_model_change(self, e):
        if e.value:
            self._refresh_opencode_variants(e.value)

    def _on_zcode_model_change(self, e):
        ref = e.value
        if not ref:
            return
        try:
            agents.set_zcode_model(ref)
            ui.notify(f'ZCode model switched to {ref} (global — affects all zcode runs)',
                      type='positive', timeout=2500)
        except (ValueError, RuntimeError, OSError) as ex:
            ui.notify(f'Could not switch ZCode model: {ex}', type='negative')
        self._update_status()

    # ---- agent working directory ----------------------------------------------

    def _render_workdir_row(self):
        """Show where agent tools run: the server's cwd, unsandboxed."""
        self.workdir_row.clear()
        with self.workdir_row:
            if self.provider in AGENT_PROVIDERS:
                ui.icon('folder_special').classes('text-blue-700 text-[18px]')
                ui.label(f'Agent dir: {os.getcwd()}') \
                    .classes('text-[12px] text-blue-800 break-all flex-1') \
                    .tooltip('The agent CLIs run unconfined in this directory '
                             "(the server's working directory)")
        self.workdir_row.set_visibility(self.provider in AGENT_PROVIDERS)

    # ---- drawer lifecycle ---------------------------------------------------

    # Providers whose catalog is cheap to fetch automatically when the drawer
    # opens (local subprocess / config read). Cline stays manual — its
    # catalog is a network call.
    AUTO_DISCOVER = {'OpenCode', 'ZCode'}

    async def toggle(self):
        opening = not self.drawer.value
        self.drawer.value = opening
        if opening and self.provider in self.AUTO_DISCOVER \
                and self.provider not in self._models_loaded:
            await self._discover(self.provider)

    async def ensure_models(self):
        """Fetch the provider's model catalog if its picker is still empty —
        used by the AI-name summarizer before its first run."""
        if self.provider in self.AUTO_DISCOVER \
                and self.provider not in self._models_loaded:
            await self._discover(self.provider)

    def chat_about_file(self, path: Path | str, text: str):
        """Attach a document as context and start a fresh chat about it."""
        path = Path(path)
        self.context_name = path.name
        self.context_text = text
        self.messages = [{'role': 'system',
                          'content': CONTEXT_PREAMBLE.format(name=path.name) + text}]
        self._reset_bubbles()
        self._render_context()
        self._update_status()
        self.drawer.value = True
        ui.notify(f'Chatting about {path.name}', type='positive', timeout=1500)

    def new_chat(self):
        """Clear the conversation, keeping the attached document (if any)."""
        if self.context_name:
            self.messages = [m for m in self.messages if m['role'] == 'system']
        else:
            self.messages = []
        self._reset_bubbles()
        self._update_status()

    def _detach_context(self):
        self.context_name = None
        self.context_text = None
        self.messages = []
        self._reset_bubbles()
        self._render_context()
        self._update_status()

    # ---- chat rendering ------------------------------------------------------

    def _reset_bubbles(self):
        self.msg_col.clear()

    def _push_user(self, text: str):
        with self.msg_col, ui.row().classes('w-full justify-end'):
            ui.chat_message(text, name='You', sent=True) \
                .props('bg-color=grey-3 text-color=grey-9')
        self._scroll_down()

    def _push_assistant(self, model: str) -> ui.markdown:
        with self.msg_col:
            with ui.chat_message(name=model or 'Assistant', sent=False):
                md = ui.markdown('')
        self._scroll_down()
        return md

    def _scroll_down(self):
        self.chat_scroll.scroll_to(percent=100)

    def _render_context(self):
        self.context_row.clear()
        with self.context_row:
            if self.context_name:
                ui.icon('description').classes('text-amber-600 text-[18px]')
                ctx = ui.label(f'Context: {self.context_name} (first 200 KB)')
                ctx.classes('text-[12px] text-amber-800 break-all flex-1')
                ctx.tooltip('Sent as leading context; agent CLIs receive the '
                            f'first {AGENT_DOC_CAP // 1000} KB (single-prompt limit)')
                ui.button(icon='close', on_click=self._detach_context) \
                    .props('flat dense round size=sm color=amber-8') \
                    .tooltip('Detach document and start a general chat')
        self.context_row.set_visibility(bool(self.context_name))

    def _update_status(self):
        p = self.provider
        if p == 'OpenCode':
            model = self.opencode_model.value or 'no model (⟳ runs opencode models)'
        elif p == 'Cline':
            model = self.cline_model.value or '(default model)'
        else:  # ZCode
            model = agents.read_zcode_model() or 'zcode config model'
        turns = f' · {len(self.messages)} message(s)' if self.messages else ''
        self.status.text = f'{p} · {model}{turns}'

    # ---- agent prompt flattening -----------------------------------------------

    def _agent_turns(self) -> list[dict]:
        """Conversation as labelled turns for a one-prompt agent run.

        Mirrors md_llm's shape: the document becomes a leading user turn plus
        an assistant ack (a system role means nothing to an agent CLI); the
        document is capped (the whole prompt travels as one argv string).
        """
        turns = []
        if self.context_text:
            doc = self.context_text[:AGENT_DOC_CAP]
            if len(self.context_text) > AGENT_DOC_CAP:
                doc += '\n\n[… document truncated]'
            turns.append({'role': 'user',
                          'content': f'Here is the document I want to discuss:\n\n{doc}'})
            turns.append({'role': 'assistant',
                          'content': "Got it — I've read the document. "
                                     'What would you like to know?'})
        turns.extend(m for m in self.messages if m['role'] != 'system')
        return turns

    @staticmethod
    def _flatten_agent_prompt(turns: list[dict]) -> str:
        """Render turns as labelled User/Assistant blocks (md_llm's shape)."""
        labels = {'user': 'User', 'assistant': 'Assistant', 'system': 'System'}
        parts = []
        for m in turns:
            role = m.get('role', 'user')
            content = (m.get('content') or '').strip()
            if not content:
                continue
            parts.append(f'{labels.get(role, role.capitalize())}:\n{content}')
        return '\n\n'.join(parts).strip()

    # ---- sending & streaming -------------------------------------------------

    def _resolve_model(self) -> tuple[str | None, str | None]:
        p = self.provider
        if p == 'OpenCode':
            model = self.opencode_model.value
            return model, None if model else 'Pick a model (⟳ runs `opencode models`).'
        if p == 'Cline':
            return self.cline_model.value or None, None  # empty = cline's own default
        return None, None  # ZCode: model routing is zcode's config

    def _make_stream_gen(self, model: str):
        p = self.provider
        # ---- agent CLIs: one flattened prompt, run in the server's cwd ----
        prompt = self._flatten_agent_prompt(self._agent_turns())
        workdir = os.getcwd()
        if p == 'OpenCode':
            variant = self.opencode_variant.value or None
            return agents.opencode_chat_stream(
                prompt, model=model, workdir=workdir,
                variant=variant, hardened=False)
        if p == 'Cline':
            thinking = self.cline_thinking.value or None
            return agents.cline_chat_stream(
                prompt, model=model, workdir=workdir,
                thinking=thinking, hardened=False)
        return agents.zcode_chat_stream(prompt, workdir=workdir, hardened=False)

    async def _send(self):
        text = (self.input.value or '').strip()
        if not text or self._stream is not None:
            return
        model, error = self._resolve_model()
        if error:
            ui.notify(error, type='warning')
            return

        self.input.value = ''
        self.messages.append({'role': 'user', 'content': text})
        self._push_user(text)
        bubble_name = model or ('ZCode' if self.provider == 'ZCode' else self.provider)
        stream_md = self._push_assistant(bubble_name)

        st = {'gen': self._make_stream_gen(model), 'text': '', 'shown': '',
              'done': False, 'error': None}
        self._stream = st
        self._stream_md = stream_md

        def worker():
            try:
                for chunk in st['gen']:
                    st['text'] += chunk
            except Exception as e:  # llm/agents raise RuntimeError/ValueError with detail
                st['error'] = str(e) or type(e).__name__
            finally:
                st['done'] = True

        threading.Thread(target=worker, daemon=True).start()
        self.send_btn.classes('hidden')
        self.stop_btn.classes(remove='hidden')
        self._update_status()
        self._tick_timer.active = True

    def _stop(self):
        if self._stream is None:
            return
        try:
            self._stream['gen'].close()  # GeneratorExit ends the worker's for-loop
        except (ValueError, RuntimeError):
            pass  # generator blocked mid-I/O; it will finish on its own

    def _tick(self):
        st = self._stream
        if st is None:
            self._tick_timer.active = False
            return
        if st['text'] != st['shown']:
            st['shown'] = st['text']
            self._stream_md.content = st['text'] or '…'
            self._scroll_down()
        if st['done']:
            self._finish(st)

    def _finish(self, st: dict):
        self._stream = None
        self._tick_timer.active = False
        self.send_btn.classes(remove='hidden')
        self.stop_btn.classes('hidden')
        if st['error']:
            self._stream_md.content = f'⚠️ {st["error"]}'
            self._stream_md.classes(add='text-red-7')
            ui.notify('The LLM call failed — see the chat for details.', type='negative')
        else:
            reply = st['text'].strip()
            if reply:
                self.messages.append({'role': 'assistant', 'content': reply})
                self._stream_md.content = reply
            else:
                self._stream_md.content = '⚠️ The model returned an empty reply.'
                self._stream_md.classes(add='text-red-7')
        self._update_status()
