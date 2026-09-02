"""Integration tests for the finder-demo LLM chat panel (agent CLIs).

Runs the real NiceGUI app in-process and simulates a user through NiceGUI's
official test harness — no browser needed. Agent streams are stubbed at the
`agents` module boundary; FINDER_TEST_REAL_AGENTS=1 additionally runs one
REAL `opencode run` end-to-end through the panel (spends CLI quota).

    .venv/bin/pip install pytest pytest-asyncio
    .venv/bin/pytest test_llm_panel.py -v
"""
from __future__ import annotations

import asyncio
import os

import pytest
from nicegui.testing import User

import app as finder_app  # noqa: F401  (reloaded by the marker below)
import llm_panel

# NiceGUI's test harness resets the global app state before each test and
# re-imports the module registered with this marker, so its @ui.page('/')
# registration survives the reset.
pytestmark = pytest.mark.module_under_test(finder_app)


def panel():
    """The ChatPanel created by the most recently opened simulated page."""
    return llm_panel.last_panel


async def drive_stream(panel, max_ticks=200):
    """Run the stream-update tick loop (in production driven by ui.timer)."""
    for _ in range(max_ticks):
        panel._tick()
        if panel._stream is None:
            return
        await asyncio.sleep(0.05)
    raise AssertionError('stream did not finish in time')


def capture_fake_stream(captured, chunks=None, error=None):
    """A stand-in *(prompt, **kwargs) -> generator the tests can spy on."""
    def fake_stream(prompt, **kwargs):
        captured['prompt'] = prompt
        captured.update(kwargs)
        if error is not None:
            raise error
        yield from chunks or []
    return fake_stream


async def test_panel_mounts_in_drawer(user: User):
    await user.open('/')
    await user.should_see('Provider & model')
    p = panel()
    assert p is not None
    assert p.provider == 'OpenCode'  # agents-only panel, OpenCode is the default
    user.find('llm-chat-input').type('hello')
    assert p.input.value == 'hello'


async def test_provider_switch_reveals_cline_settings(user: User):
    await user.open('/')
    p = panel()
    p.provider_sel.value = 'Cline'
    await asyncio.sleep(0.05)
    assert p.provider == 'Cline'
    assert p.cline_box.visible
    assert not p.opencode_box.visible
    assert not p.zcode_box.visible
    # the agent-dir chip follows the provider
    await user.should_see(f'Agent dir: {os.getcwd()}')


async def test_streaming_reply_reaches_history(user: User, monkeypatch):
    await user.open('/')
    p = panel()
    monkeypatch.setattr(llm_panel.agents, 'list_opencode_model_details', lambda **kw: {
        'mock-prov/mock-model': {'variants': {'high': {}}},
    })
    await p._discover('OpenCode')

    captured = {}
    monkeypatch.setattr(llm_panel.agents, 'opencode_chat_stream',
                        capture_fake_stream(captured, ['AGENT ', 'reply']))
    user.find('llm-chat-input').type('hello agent')
    user.find('llm-send').click()
    await asyncio.sleep(0.05)
    await drive_stream(p)

    assert p.messages[0] == {'role': 'user', 'content': 'hello agent'}
    assert p.messages[1] == {'role': 'assistant', 'content': 'AGENT reply'}
    await user.should_see('AGENT reply')

    # second turn: the flattened agent prompt carries the whole conversation
    user.find('llm-chat-input').type('and again')
    user.find('llm-send').click()
    await asyncio.sleep(0.05)
    await drive_stream(p)
    assert len(p.messages) == 4
    assert 'Assistant:\nAGENT reply' in captured['prompt']
    assert captured['prompt'].rstrip().endswith('User:\nand again')


async def test_agent_error_is_surfaced_in_bubble(user: User, monkeypatch):
    await user.open('/')
    p = panel()
    monkeypatch.setattr(llm_panel.agents, 'list_opencode_model_details', lambda **kw: {
        'mock-prov/mock-model': {},
    })
    await p._discover('OpenCode')

    captured = {}
    monkeypatch.setattr(
        llm_panel.agents, 'opencode_chat_stream',
        capture_fake_stream(captured, error=RuntimeError('opencode exploded')))
    user.find('llm-chat-input').type('anyone there?')
    user.find('llm-send').click()
    await asyncio.sleep(0.05)
    await drive_stream(p)

    assert p._stream is None
    assert p._stream_md.content == '⚠️ opencode exploded'
    # the failed turn is not added to the history, so a retry is clean
    assert len(p.messages) == 1


async def test_new_chat_keeps_context_and_clears_turns(user: User):
    await user.open('/')
    p = panel()
    p.chat_about_file(__file__, 'sample document text')
    assert p.messages[0]['role'] == 'system'
    assert 'sample document text' in p.messages[0]['content']

    user.find('llm-new-chat').click()
    assert len(p.messages) == 1  # only the system context remains
    assert p.context_name.endswith('test_llm_panel.py')

    p._detach_context()
    user.find('llm-new-chat').click()
    assert p.messages == []
    assert p.context_name is None


# ---- agent CLI providers (OpenCode / Cline / ZCode) -------------------------

async def test_opencode_agent_run_in_cwd(user: User, monkeypatch):
    await user.open('/')
    p = panel()
    monkeypatch.setattr(llm_panel.agents, 'list_opencode_model_details', lambda **kw: {
        'mock-prov/mock-model': {'variants': {'low': {}, 'high': {}}},
    })
    await p._discover('OpenCode')
    assert p.opencode_model.value == 'mock-prov/mock-model'
    assert p.opencode_variant.value == 'high'    # highest effort preselected

    captured = {}
    monkeypatch.setattr(llm_panel.agents, 'opencode_chat_stream',
                        capture_fake_stream(captured, ['🛠 bash: ls\n', 'AGENT-OK ', 'reply']))
    p.chat_about_file('/tmp/some-doc.md', 'The document body text.')

    user.find('llm-chat-input').type('summarize it')
    user.find('llm-send').click()
    await asyncio.sleep(0.05)
    await drive_stream(p)

    # the flattened single prompt carries context + ack + the Q&A turn
    assert 'Here is the document I want to discuss' in captured['prompt']
    assert 'The document body text.' in captured['prompt']
    assert "Got it — I've read the document" in captured['prompt']
    assert captured['prompt'].rstrip().endswith('User:\nsummarize it')
    assert not captured['prompt'].startswith('System:')  # context is a User turn for agents
    # the agent runs unconfined in the server's working directory, no Seatbelt
    assert captured['workdir'] == os.getcwd()
    assert os.path.isdir(captured['workdir'])
    assert captured['hardened'] is False
    assert captured['model'] == 'mock-prov/mock-model'
    assert captured['variant'] == 'high'
    # the streamed reply (tool marker included) landed in history
    assert p.messages[-1] == {'role': 'assistant',
                              'content': '🛠 bash: ls\nAGENT-OK reply'}
    await user.should_see('AGENT-OK reply')


async def test_agent_document_cap_runs_in_cwd(user: User, monkeypatch):
    await user.open('/')
    p = panel()
    p.provider_sel.value = 'Cline'
    await asyncio.sleep(0.05)
    p.cline_thinking.value = 'high'

    captured = {}
    monkeypatch.setattr(llm_panel.agents, 'cline_chat_stream',
                        capture_fake_stream(captured, ['CLINE-REPLY']))
    p.chat_about_file('/tmp/big.md', 'A' * (llm_panel.AGENT_DOC_CAP + 500))

    user.find('llm-chat-input').type('short question')
    user.find('llm-send').click()
    await asyncio.sleep(0.05)
    await drive_stream(p)

    assert '[… document truncated]' in captured['prompt']
    assert 'A' * (llm_panel.AGENT_DOC_CAP + 100) not in captured['prompt']
    assert captured['workdir'] == os.getcwd()
    assert captured['hardened'] is False
    assert captured['thinking'] == 'high'
    assert captured['model'] is None  # "(default)" → cline's own configured model


async def test_zcode_uses_config_model_and_switch_persists(user: User, monkeypatch):
    await user.open('/')
    p = panel()
    # never touch the real zcode config in tests
    monkeypatch.setattr(llm_panel.agents, 'read_zcode_model', lambda path=None: 'prov/m-1')
    switched = []
    monkeypatch.setattr(llm_panel.agents, 'set_zcode_model',
                        lambda ref, path=None: switched.append(ref))
    monkeypatch.setattr(llm_panel.agents, 'list_zcode_model_refs',
                        lambda path=None: ['prov/m-1', 'prov/m-2'])

    p.provider_sel.value = 'ZCode'
    await asyncio.sleep(0.05)
    assert p.zcode_box.visible
    await p._discover('ZCode')
    assert p.zcode_model.value == 'prov/m-1'

    captured = {}
    monkeypatch.setattr(llm_panel.agents, 'zcode_chat_stream',
                        capture_fake_stream(captured, ['ZCODE-REPLY']))
    user.find('llm-chat-input').type('do something')
    user.find('llm-send').click()
    await asyncio.sleep(0.05)
    await drive_stream(p)
    assert 'model' not in captured            # zcode takes no per-run model
    assert captured['hardened'] is False
    assert captured['workdir'] == os.getcwd()
    assert 'User:\ndo something' in captured['prompt']

    p.zcode_model.value = 'prov/m-2'          # picker persists via set_zcode_model
    assert 'prov/m-2' in switched             # (programmatic set fires on_change)


async def test_opencode_variant_options_follow_model(user: User, monkeypatch):
    await user.open('/')
    p = panel()
    monkeypatch.setattr(llm_panel.agents, 'list_opencode_model_details', lambda **kw: {
        'prov/a': {'variants': {'low': {}, 'high': {}}},
        'prov/b': {'variants': {}},
    })
    monkeypatch.setattr(llm_panel.agents, 'list_opencode_models', lambda **kw: ['prov/a', 'prov/b'])
    await p._discover('OpenCode')
    assert p.opencode_model.value == 'prov/a'
    assert p.opencode_variant.value == 'high'   # highest effort preselected
    p.opencode_model.value = 'prov/b'
    p._on_opencode_model_change(type('E', (), {'value': 'prov/b'})())
    assert not p.opencode_variant.value         # no variants → omit --variant


async def test_real_opencode_end_to_end(user: User):
    """Opt-in: one REAL `opencode run` through the panel (FINDER_TEST_REAL_AGENTS=1)."""
    if not os.environ.get('FINDER_TEST_REAL_AGENTS'):
        pytest.skip('set FINDER_TEST_REAL_AGENTS=1 to spend real agent quota')
    await user.open('/')
    p = panel()
    await p._discover('OpenCode')
    assert p.opencode_model.options, 'opencode models discovery failed'
    user.find('llm-chat-input').type('Reply with exactly one word: PINEAPPLE')
    user.find('llm-send').click()
    await asyncio.sleep(0.05)
    await drive_stream(p, max_ticks=600)  # real agent runs can take a while
    assert p.messages[-1]['role'] == 'assistant'
    assert 'PINEAPPLE' in p.messages[-1]['content'].upper()
