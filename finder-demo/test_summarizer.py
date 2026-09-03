"""Tests for the AI file-name summarizer (summarizer.py + the app button).

Unit tests cover the cache store, reply cleaning and the LLM glue; the
app-level tests run the real NiceGUI page through NiceGUI's official User
simulation with the LLM stream stubbed at `summarizer.make_stream_fn` (the
same boundary test_llm_panel.py stubs at `agents`):

    .venv/bin/pytest test_summarizer.py -v
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
import types

import pytest
from nicegui.testing import User

import app as finder_app  # noqa: F401  (reloaded by the marker below)
import llm_panel
import summarizer

pytestmark = pytest.mark.module_under_test(finder_app)


async def wait_until(pred, timeout=5.0, what='condition'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f'{what} not met within {timeout}s')


def setup_ai_env(monkeypatch, tmp_path, fake_stream):
    """Point the page at tmp_path and stub the LLM + model discovery."""
    monkeypatch.setattr(finder_app, 'HOME', tmp_path)
    store = summarizer.SummaryStore(tmp_path / '.shangceng')
    monkeypatch.setattr(summarizer, 'store', store)
    monkeypatch.setattr(summarizer, 'make_stream_fn', lambda chat: fake_stream)
    monkeypatch.setattr(llm_panel.agents, 'list_opencode_model_details',
                        lambda **kw: {'mock-prov/mock-model': {'variants': {'high': {}}}})
    return store


# ---- SummaryStore (the ~/.shangceng cache) ----------------------------------

def test_store_roundtrip_and_invalidation(tmp_path):
    store = summarizer.SummaryStore(tmp_path)
    assert len(store) == 0

    store.put('/notes.txt', 'Meeting notes', mtime=100.0, size=42, provider='OpenCode')
    assert store.get('/notes.txt', 100.0, 42) == 'Meeting notes'
    store.save()

    # a fresh instance reads the same file back
    reloaded = summarizer.SummaryStore(tmp_path)
    assert reloaded.get('/notes.txt', 100.0, 42) == 'Meeting notes'

    # changed mtime or size invalidates the record (the file's content changed)
    assert reloaded.get('/notes.txt', 200.0, 42) is None
    assert reloaded.get('/notes.txt', 100.0, 43) is None
    assert reloaded.get('/other.txt', 100.0, 42) is None


def test_store_survives_corrupt_cache_file(tmp_path):
    (tmp_path / summarizer.CACHE_FILE_NAME).write_text('{not json', 'utf-8')
    store = summarizer.SummaryStore(tmp_path)
    assert len(store) == 0
    store.put('/a.txt', 'A', mtime=1.0, size=1)
    store.save()  # overwrites the broken file with a valid one
    assert summarizer.SummaryStore(tmp_path).get('/a.txt', 1.0, 1) == 'A'


def test_store_honors_shangceng_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv('SHANGCENG_HOME', str(tmp_path / 'custom'))
    store = summarizer.SummaryStore()
    assert store.file == tmp_path / 'custom' / summarizer.CACHE_FILE_NAME


# ---- reply cleaning ----------------------------------------------------------

def test_clean_summary_takes_first_usable_line():
    assert summarizer.clean_summary('Budget overview\nand more') == 'Budget overview'
    assert summarizer.clean_summary('  "Quoted name."  ') == 'Quoted name'
    assert summarizer.clean_summary('1. Numbered name') == 'Numbered name'
    assert summarizer.clean_summary('`Backticked`') == 'Backticked'


def test_clean_summary_drops_tool_markers_and_blanks():
    noisy = '🛠 bash: ls -la\n\nBudget overview\n🛠 read: file'
    assert summarizer.clean_summary(noisy) == 'Budget overview'
    assert summarizer.clean_summary('') == ''
    assert summarizer.clean_summary('🛠 bash: ls only') == ''


def test_clean_summary_caps_length_on_word_boundary():
    long = 'A very long descriptive name that keeps going well past the limit'
    out = summarizer.clean_summary(long, max_chars=30)
    assert 0 < len(out) <= 30
    assert long.startswith(out)


# ---- LLM glue ----------------------------------------------------------------

def test_make_stream_fn_opencode_passes_model_and_filters_markers(monkeypatch):
    captured = {}

    def fake_stream(prompt, **kwargs):
        captured.update(kwargs)
        yield '🛠 bash: ls\n'
        yield 'Setup guide'

    monkeypatch.setattr(llm_panel.agents, 'opencode_chat_stream', fake_stream)
    chat = types.SimpleNamespace(
        provider='OpenCode',
        _resolve_model=lambda: ('mock-prov/mock-model', None),
        opencode_variant=types.SimpleNamespace(value='high'),
    )
    assert summarizer.make_stream_fn(chat)('p') == 'Setup guide'
    assert captured['model'] == 'mock-prov/mock-model'
    assert captured['variant'] == 'high'
    assert captured['hardened'] is False


def test_make_stream_fn_raises_without_model():
    chat = types.SimpleNamespace(
        provider='OpenCode',
        _resolve_model=lambda: (None, 'Pick a model (⟳ runs `opencode models`).'),
    )
    with pytest.raises(RuntimeError, match='Pick a model'):
        summarizer.make_stream_fn(chat)


def test_summarize_file_sends_name_and_content_and_cleans(tmp_path):
    prompts = []
    report = tmp_path / 'report_final_v2.txt'
    report.write_text('Quarterly revenue report with charts.', 'utf-8')
    summary = summarizer.summarize_file(
        report, lambda p: (prompts.append(p), 'Revenue report')[1])

    assert summary == 'Revenue report'
    assert 'File: report_final_v2.txt' in prompts[0]
    assert 'Quarterly revenue report with charts.' in prompts[0]

    # unreadable and empty files yield nothing usable
    assert summarizer.summarize_file(tmp_path / 'missing.txt', lambda p: 'X') is None
    empty = tmp_path / 'empty.txt'
    empty.write_text('   \n', 'utf-8')
    assert summarizer.summarize_file(empty, lambda p: 'X') is None


def test_sensitive_looking_names_are_flagged():
    for name in ('.env', 'app.env', 'secrets.txt', 'aws_credentials.csv',
                 'my_passwords.md', 'auth_token.json', 'private_key.pem'):
        assert summarizer.is_sensitive(name), name
    for name in ('notes.txt', 'quarterly report.md', 'setup.py'):
        assert not summarizer.is_sensitive(name), name


# ---- the app button, end to end -----------------------------------------------

async def test_button_summarizes_displays_and_caches(user: User, monkeypatch, tmp_path):
    calls = []

    def fake_stream(prompt):
        calls.append(prompt)
        name = re.search(r'File: (.+)', prompt).group(1)
        return f'AI {name}'

    store = setup_ai_env(monkeypatch, tmp_path, fake_stream)
    (tmp_path / 'a.txt').write_text('meeting notes about the budget', 'utf-8')
    (tmp_path / 'b.md').write_text('# Setup guide\ninstall things', 'utf-8')
    (tmp_path / 'pic.png').write_bytes(b'\x89PNG fake bytes')  # image: never summarized

    await user.open('/')
    await user.should_see('a.txt')  # real names first
    user.find('ai-names-btn').click()
    await wait_until(lambda: len(store) == 2, what='both summaries cached')

    # only the text files were sent to the LLM, one prompt each
    assert len(calls) == 2
    assert not any('pic.png' in p for p in calls)
    assert 'meeting notes about the budget' in calls[0]  # content travels in the prompt

    # display switched on: AI names replace the real ones
    await user.should_see('AI a.txt')
    await user.should_see('AI b.md')

    # toggle off → real names; toggle on again → fully cached, no new LLM calls
    user.find('ai-names-btn').click()
    await asyncio.sleep(0.1)
    await user.should_see('a.txt')
    user.find('ai-names-btn').click()
    await user.should_see('AI a.txt')
    assert len(calls) == 2


async def test_failed_summary_is_not_cached(user: User, monkeypatch, tmp_path):
    calls = []

    def failing_stream(prompt):
        calls.append(prompt)
        raise RuntimeError('agent down')

    store = setup_ai_env(monkeypatch, tmp_path, failing_stream)
    (tmp_path / 'good.txt').write_text('hello world content', 'utf-8')

    await user.open('/')
    user.find('ai-names-btn').click()
    await wait_until(lambda: len(calls) == 1, what='the LLM call attempted')
    await asyncio.sleep(0.3)  # let the job finish

    assert len(store) == 0  # failures are never cached — the next run retries
    await user.should_see('good.txt')  # no AI name available: real name shown


async def test_button_clicks_are_ignored_while_job_runs(user: User, monkeypatch, tmp_path):
    calls = []
    started = threading.Event()
    resume = threading.Event()

    def slow_stream(prompt):
        calls.append(prompt)
        started.set()
        resume.wait(timeout=5)  # hold the first job open
        return 'Slow name'

    store = setup_ai_env(monkeypatch, tmp_path, slow_stream)
    (tmp_path / 'a.txt').write_text('content', 'utf-8')

    await user.open('/')
    user.find('ai-names-btn').click()
    await wait_until(started.is_set, what='job started')
    user.find('ai-names-btn').click()  # while running → must be ignored
    await asyncio.sleep(0.2)
    assert len(calls) == 1
    resume.set()
    await wait_until(lambda: len(store) == 1, what='summary cached')

    assert len(calls) == 1
    await user.should_see('Slow name')


async def test_stop_button_halts_after_current_file(user: User, monkeypatch, tmp_path):
    calls = []
    started = threading.Event()
    resume = threading.Event()

    def slow_stream(prompt):
        calls.append(prompt)
        name = re.search(r'File: (.+)', prompt).group(1)
        if name == 'a.txt':
            started.set()
            resume.wait(timeout=5)  # hold a.txt in flight
        return f'AI {name}'

    store = setup_ai_env(monkeypatch, tmp_path, slow_stream)
    (tmp_path / 'a.txt').write_text('first', 'utf-8')
    (tmp_path / 'b.txt').write_text('second', 'utf-8')

    await user.open('/')
    user.find('ai-names-btn').click()
    await wait_until(started.is_set, what='first file summarizing')
    user.find('ai-stop-btn').click()
    await asyncio.sleep(0.2)  # let the cancel flag land before releasing the file
    resume.set()
    await wait_until(lambda: len(store) == 1, what='a.txt cached')

    # b.txt was never sent: the job stopped after the in-flight file, whose
    # summary was still kept
    assert len(calls) == 1
    stat = (tmp_path / 'a.txt').stat()
    assert store.get(str(tmp_path / 'a.txt'), stat.st_mtime, stat.st_size) == 'AI a.txt'
    await user.should_see('AI a.txt')  # partial results are shown
