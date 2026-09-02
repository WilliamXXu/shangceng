"""
Finder-like read-only file browser — a NiceGUI demo.

Run:
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python app.py
    →  http://localhost:8765

Read-only: nothing is renamed, moved or deleted. "Open with macOS" only
launches a file with its default app, like double-clicking in Finder.
"""
from __future__ import annotations

import datetime as dt
import functools
import html as html_mod
import os
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

from nicegui import app, ui

from llm_panel import ChatPanel

PORT = 8765
HOME = Path.home().resolve()
DARWIN = sys.platform == 'darwin'

# Read-only static mount so the browser can load local images/videos for previews.
# (Demo only — never serve '/' like this on a network-exposed machine.)
app.add_static_files('/files', '/')

# Paint an explicit page background (a transparent body renders black in dark
# webviews/embedded browsers, white in standalone Chrome).
PAGE_BG_STYLE = '<style>body { background: #ffffff; }</style>'

IMAGE_TYPES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp', '.ico', '.avif'}
VIDEO_TYPES = {'.mp4', '.webm'}
AUDIO_TYPES = {'.mp3', '.m4a', '.wav', '.ogg', '.flac'}
TEXT_TYPES = {
    '.txt', '.md', '.markdown', '.rst', '.log', '.json', '.yaml', '.yml', '.toml',
    '.ini', '.cfg', '.conf', '.csv', '.tsv', '.xml', '.html', '.htm', '.css',
    '.js', '.ts', '.jsx', '.tsx', '.vue', '.svelte', '.py', '.pyi', '.ipynb',
    '.sh', '.bash', '.zsh', '.fish', '.env', '.sql', '.c', '.h', '.cpp', '.hpp',
    '.m', '.mm', '.swift', '.kt', '.java', '.rb', '.go', '.rs', '.php', '.pl',
    '.lua', '.gitignore', '.gitattributes', '.editorconfig', '.lock',
}

SUFFIX_ICONS = {
    '.py': ('code', 'blue-7'), '.ipynb': ('code', 'blue-7'), '.sh': ('code', 'blue-7'),
    '.js': ('code', 'blue-7'), '.ts': ('code', 'blue-7'), '.json': ('code', 'blue-7'),
    '.html': ('code', 'blue-7'), '.css': ('code', 'blue-7'), '.swift': ('code', 'blue-7'),
    '.md': ('description', 'grey-8'), '.txt': ('description', 'grey-8'),
    '.rst': ('description', 'grey-8'), '.log': ('description', 'grey-8'),
    '.pdf': ('picture_as_pdf', 'red-6'),
    '.png': ('image', 'green-6'), '.jpg': ('image', 'green-6'), '.jpeg': ('image', 'green-6'),
    '.gif': ('image', 'green-6'), '.webp': ('image', 'green-6'), '.svg': ('image', 'green-6'),
    '.heic': ('image', 'green-6'), '.bmp': ('image', 'green-6'),
    '.mp3': ('music_note', 'purple-6'), '.m4a': ('music_note', 'purple-6'),
    '.wav': ('music_note', 'purple-6'), '.flac': ('music_note', 'purple-6'),
    '.aiff': ('music_note', 'purple-6'),
    '.mp4': ('movie', 'pink-6'), '.mov': ('movie', 'pink-6'), '.avi': ('movie', 'pink-6'),
    '.mkv': ('movie', 'pink-6'),
    '.zip': ('archive', 'brown'), '.tar': ('archive', 'brown'), '.gz': ('archive', 'brown'),
    '.tgz': ('archive', 'brown'), '.7z': ('archive', 'brown'), '.rar': ('archive', 'brown'),
    '.dmg': ('album', 'indigo-6'), '.pkg': ('album', 'indigo-6'),
    '.doc': ('article', 'blue-7'), '.docx': ('article', 'blue-7'),
    '.xls': ('table_chart', 'green-7'), '.xlsx': ('table_chart', 'green-7'),
    '.csv': ('table_chart', 'green-7'), '.tsv': ('table_chart', 'green-7'),
    '.ppt': ('slideshow', 'orange-7'), '.pptx': ('slideshow', 'orange-7'),
    '.key': ('slideshow', 'orange-7'),
}
DEFAULT_ICON = ('insert_drive_file', 'grey-7')
DIR_ICON = ('folder', 'amber-6')

FAVORITES = [  # (icon, quasar color, label, path) — non-existent ones are skipped
    ('desktop_mac', 'grey-8', 'Macintosh HD', Path('/')),
    ('home', 'grey-8', None, HOME),  # label filled in with the user name
    ('desktop_mac', 'grey-7', 'Desktop', HOME / 'Desktop'),
    ('article', 'grey-7', 'Documents', HOME / 'Documents'),
    ('file_download', 'grey-7', 'Downloads', HOME / 'Downloads'),
    ('photo', 'grey-7', 'Pictures', HOME / 'Pictures'),
    ('library_music', 'grey-7', 'Music', HOME / 'Music'),
    ('movie', 'grey-7', 'Movies', HOME / 'Movies'),
    ('apps', 'indigo-6', 'Applications', Path('/Applications')),
]

SORT_KEYS = {
    'name': lambda e: e['name'].lower(),
    'size': lambda e: -e['size'],
    'modified': lambda e: -e['mtime'],
    'kind': lambda e: e['kind'].lower(),
}


def human_size(n: float) -> str:
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


def fmt_time(ts: float) -> str:
    try:
        return dt.datetime.fromtimestamp(ts).strftime('%b %-d, %Y at %H:%M')
    except ValueError:  # %-d is not portable everywhere
        return dt.datetime.fromtimestamp(ts).strftime('%b %d, %Y at %H:%M')


def icon_for(name: str, is_dir: bool) -> tuple[str, str]:
    if is_dir:
        return ('apps', 'indigo-6') if name.endswith('.app') else DIR_ICON
    return SUFFIX_ICONS.get(Path(name).suffix.lower(), DEFAULT_ICON)


def kind_for(name: str, is_dir: bool) -> str:
    if is_dir:
        return 'Application' if name.endswith('.app') else 'Folder'
    suffix = Path(name).suffix.lower()
    return f'{suffix[1:].upper()} file' if suffix else 'Document'


def is_hidden(name: str) -> bool:
    return name.startswith('.') or name.startswith('._')


def scan(folder: Path, show_hidden: bool, query: str, sort: str):
    """Return (entries, error, total_file_bytes). entries: list of dicts, sorted."""
    entries: list[dict] = []
    total = 0
    try:
        with os.scandir(folder) as it:
            for entry in it:
                name = entry.name
                if not show_hidden and is_hidden(name):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if not is_dir and entry.is_dir(follow_symlinks=True):
                        is_dir = True  # symlink pointing at a folder behaves like a folder
                except OSError:
                    continue  # broken symlink or unreadable entry — skip like Finder would
                if query and query not in name.lower():
                    continue
                size = 0 if is_dir else st.st_size
                total += size
                icon, color = icon_for(name, is_dir)
                entries.append({
                    'name': name,
                    'path': str(Path(folder) / name),
                    'is_dir': is_dir,
                    'size': size,
                    'mtime': st.st_mtime,
                    'icon': icon,
                    'icon_color': color,
                    'kind': kind_for(name, is_dir),
                    'size_h': '--' if is_dir else human_size(size),
                    'date_h': fmt_time(st.st_mtime),
                })
    except OSError as e:
        return None, f'{e.strerror or e}', 0

    entries.sort(key=lambda e: (0 if e['is_dir'] else 1, SORT_KEYS[sort](e)))
    return entries, None, total


@ui.page('/')
def index():
    ui.dark_mode(False)  # Finder look: force light even if the browser prefers dark
    ui.add_head_html(PAGE_BG_STYLE)
    state = {'cwd': HOME, 'view': 'grid', 'search': '', 'show_hidden': False, 'sort': 'name'}
    history = [HOME]
    hidx = [0]

    # ---- behaviour (defined first; UI elements below are closed over) ------

    def navigate(path: Path, push: bool = True):
        try:
            path = path.resolve()
        except OSError:
            pass
        if not path.is_dir():
            ui.notify(f"Can't open “{path.name}” — not a folder", type='warning')
            return
        state['cwd'] = path
        if push and history[hidx[0]] != path:
            del history[hidx[0] + 1:]
            history.append(path)
            hidx[0] = len(history) - 1
        render_location()
        render_content()

    def go_back():
        if hidx[0] > 0:
            hidx[0] -= 1
            navigate(history[hidx[0]], push=False)

    def go_forward():
        if hidx[0] < len(history) - 1:
            hidx[0] += 1
            navigate(history[hidx[0]], push=False)

    def on_search(e):
        state['search'] = (e.value or '').lower()
        render_content()

    def on_sort_change(e):
        state['sort'] = e.value
        render_content()

    def set_view(v):
        state['view'] = v
        grid_btn.props('color=primary' if v == 'grid' else 'color=grey-7')
        list_btn.props('color=primary' if v == 'list' else 'color=grey-7')
        render_content()

    def toggle_hidden():
        state['show_hidden'] = not state['show_hidden']
        hidden_btn.props('icon=visibility color=primary' if state['show_hidden']
                         else 'icon=visibility_off color=grey-7')
        render_content()

    def open_entry(entry: dict):
        if entry['is_dir']:
            navigate(Path(entry['path']))
        else:
            preview(Path(entry['path']), entry)

    def _on_row_click(e):
        args = e.args if isinstance(e.args, tuple) else (e.args, )
        row = next((a for a in args if isinstance(a, dict)), None)
        if row:
            open_entry(row)

    def render_location():
        breadcrumbs.clear()
        with breadcrumbs:
            with ui.button(on_click=lambda: navigate(Path('/'))) \
                    .props('flat no-caps dense size=sm'):
                ui.icon('desktop_mac').classes('text-base')
                ui.label('Macintosh HD').classes('text-[13px] ml-1')
            acc = Path('/')
            for part in state['cwd'].parts[1:]:
                acc = acc / part
                last = acc == state['cwd']
                ui.icon('chevron_right', color='grey-5').classes('text-sm')
                ui.button(part, on_click=functools.partial(navigate, acc)) \
                    .props('flat no-caps dense size=sm') \
                    .classes('text-[13px]' + (' font-semibold' if last else ''))
        back_btn.props('disable') if hidx[0] == 0 else back_btn.props(remove='disable')
        fwd_btn.props('disable') if hidx[0] >= len(history) - 1 else fwd_btn.props(remove='disable')
        render_sidebar()

    def render_sidebar():
        sidebar_box.clear()
        with sidebar_box:
            ui.label('Favorites').classes('text-[11px] font-semibold text-gray-500 px-2 pt-1 pb-1')
            for icon, color, label, path in FAVORITES:
                if label is None:
                    label = path.name
                if not path.exists():
                    continue
                active = state['cwd'] == path
                ui.button(label, icon=icon, on_click=functools.partial(navigate, path)) \
                    .props('flat no-caps align=left') \
                    .classes('w-full text-gray-900' +
                             (' bg-black/10 rounded-lg' if active else ''))
            ui.separator().classes('my-2')
            ui.label('Locations').classes('text-[11px] font-semibold text-gray-500 px-2 pb-1')
            try:
                usage = shutil.disk_usage('/')
                ui.linear_progress(min(usage.used / usage.total, 1.0), show_value=False) \
                    .classes('w-full')
                ui.label(f'{human_size(usage.free)} available of {human_size(usage.total)}') \
                    .classes('text-[11px] text-gray-500 px-2')
            except OSError:
                pass

    def render_content():
        content.clear()
        entries, err, total = scan(state['cwd'], state['show_hidden'], state['search'],
                                   state['sort'])
        try:
            free_h = human_size(shutil.disk_usage(state['cwd']).free)
        except OSError:
            free_h = ''
        with content:
            if err is not None:
                with ui.card().classes('w-full'):
                    ui.icon('lock', color='red-6').classes('text-4xl')
                    ui.label("Can't open this folder").classes('font-semibold text-lg')
                    ui.label(err).classes('text-sm text-gray-600')
                    ui.label('macOS may require Full Disk Access for the app running '
                             'this server (System Settings → Privacy & Security).') \
                        .classes('text-xs text-gray-400')
            elif not entries:
                with ui.column().classes('w-full items-center py-16 text-gray-400 gap-2'):
                    ui.icon('search_off' if state['search'] else 'folder_open').classes('text-5xl')
                    ui.label('No matches' if state['search'] else 'Empty folder')
            elif state['view'] == 'grid':
                render_grid(entries)
            else:
                render_table(entries)

        label = f'{len(entries):,} item{"s" if len(entries) != 1 else ""}'
        if total:
            label += f' · {human_size(total)} in files'
        if free_h:
            label += f' · {free_h} available'
        items_label.text = label

    def render_grid(entries):
        with ui.row().classes('w-full flex-wrap gap-1 content-start'):
            for entry in entries[:2000]:
                with ui.button(on_click=functools.partial(open_entry, entry)) \
                        .props('flat no-caps padding=6px') \
                        .classes('w-[104px] h-[104px] rounded-lg hover:bg-black/5'):
                    with ui.column().classes('items-center justify-start gap-1 w-full'):
                        ui.icon(entry['icon'], color=entry['icon_color']).classes('text-[40px]')
                        ui.label(entry['name']) \
                            .classes('text-[11px] leading-tight break-all line-clamp-2 text-gray-800')
            if len(entries) > 2000:
                ui.label(f'… and {len(entries) - 2000:,} more — narrow with search') \
                    .classes('text-xs text-gray-400 p-2')

    def render_table(entries):
        columns = [
            {'name': 'icon', 'label': '', 'field': 'icon', 'align': 'left'},
            {'name': 'name', 'label': 'Name', 'field': 'name', 'align': 'left', 'sortable': True},
            {'name': 'kind', 'label': 'Kind', 'field': 'kind', 'align': 'left', 'sortable': True},
            {'name': 'size', 'label': 'Size', 'field': 'size_h', 'align': 'right'},
            {'name': 'modified', 'label': 'Date Modified', 'field': 'date_h', 'align': 'left'},
        ]
        table = ui.table(columns=columns, rows=entries[:2000], row_key='path')
        table.props('flat dense rowsPerPage=0').classes('w-full')
        table.on('rowClick', handler=_on_row_click,
                 js_handler='(evt, row, index) => emit(row)')
        table.add_slot('body-cell-icon', '''
            <q-td :props="props" style="width: 32px">
              <q-icon :name="props.row.icon" :color="props.row.icon_color" size="22px"/>
            </q-td>''')
        if len(entries) > 2000:
            ui.label(f'… and {len(entries) - 2000:,} more — narrow with search') \
                .classes('text-xs text-gray-400')

    def preview(path: Path, entry: dict):
        src = '/files' + urllib.parse.quote(str(path))
        suffix = path.suffix.lower()
        preview_text: str | None = None  # attachable chat context for text files
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        with ui.dialog() as dlg, ui.card().style('width: 760px; max-width: 92vw'):
            with ui.row().classes('w-full items-center gap-3'):
                ui.icon(entry['icon'], color=entry['icon_color']).classes('text-4xl')
                with ui.column().classes('gap-0'):
                    ui.label(path.name).classes('font-semibold text-lg break-all')
                    ui.label(f"{entry['kind']} · {entry['size_h']} · {entry['date_h']}") \
                        .classes('text-xs text-gray-500')
            ui.separator()
            if suffix in IMAGE_TYPES:
                ui.image(src).classes('max-h-[55vh] w-auto rounded')
            elif suffix in VIDEO_TYPES:
                ui.html(f'<video controls src="{src}" style="max-height:55vh;width:100%"></video>')
            elif suffix in AUDIO_TYPES:
                ui.html(f'<audio controls src="{src}" style="width:100%"></audio>')
            elif size is not None and size <= 1_000_000 and (suffix in TEXT_TYPES or not suffix):
                try:
                    text = path.read_bytes()[:200_000].decode('utf-8', 'replace')
                    preview_text = text
                except OSError as e:
                    text = f'Could not read file: {e}'
                with ui.scroll_area().classes('w-full max-h-[55vh]'):
                    if suffix in {'.md', '.markdown'}:
                        ui.markdown(text)
                    else:
                        ui.html('<pre style="white-space:pre-wrap;word-break:break-word;'
                                'font-family:ui-monospace,Menlo,monospace;font-size:12px;'
                                'margin:0">' + html_mod.escape(text) + '</pre>')
            else:
                with ui.column().classes('w-full items-center py-8 text-gray-400 gap-2'):
                    ui.icon('visibility_off').classes('text-5xl')
                    ui.label('No preview available for this file type')
            with ui.row().classes('w-full items-center'):
                ui.label(str(path)).classes('text-[11px] text-gray-400 break-all flex-1')
                if preview_text is not None:
                    ui.button('Chat about this file', icon='forum',
                              on_click=lambda: chat.chat_about_file(path, preview_text)) \
                        .props('flat no-caps color=primary') \
                        .tooltip('Open the LLM chat with this file as context')
                if DARWIN:
                    ui.button('Open with macOS', icon='open_in_new',
                              on_click=lambda: subprocess.run(['open', str(path)])) \
                        .props('flat no-caps')
                ui.button('Done', on_click=dlg.close).props('unelevated no-caps color=primary')
        dlg.open()

    # ---- UI ------------------------------------------------------------------

    chat = ChatPanel()  # streaming LLM chat in a right drawer (md_llm clients)

    with ui.header().classes('bg-[#f6f6f6] text-gray-800 border-b border-gray-200'):
        with ui.row().classes('w-full items-center flex-wrap gap-x-2 gap-y-1'):
            for c in ('#ff5f57', '#febc2e', '#28c840'):
                ui.element('div').classes('w-3 h-3 rounded-full').style(f'background:{c}')
            back_btn = ui.button(icon='arrow_back', on_click=go_back).props('flat round dense')
            fwd_btn = ui.button(icon='arrow_forward', on_click=go_forward).props('flat round dense')
            breadcrumbs = ui.row().classes('items-center gap-0 flex-1 min-w-[220px] flex-wrap')

            search = ui.input(placeholder='Search', on_change=on_search)
            search.props('dense outlined rounded-lg debounce=300').classes('w-44 bg-white')
            ui.select(
                {'name': 'Sort: Name', 'size': 'Sort: Size', 'modified': 'Sort: Date',
                 'kind': 'Sort: Kind'},
                value='name', on_change=on_sort_change,
            ).props('dense outlined rounded-lg').classes('w-36 bg-white')

            grid_btn = ui.button(icon='view_module', on_click=lambda: set_view('grid'))
            grid_btn.props('flat dense').tooltip('Icon view')
            list_btn = ui.button(icon='view_list', on_click=lambda: set_view('list'))
            list_btn.props('flat dense').tooltip('List view')
            hidden_btn = ui.button(icon='visibility_off', on_click=toggle_hidden)
            hidden_btn.props('flat dense').tooltip('Show hidden files')
            chat_btn = ui.button(icon='forum', on_click=chat.toggle)
            chat_btn.props('flat dense').tooltip('LLM chat')

    with ui.left_drawer().props('bordered').classes('bg-[#f2f2f4] text-gray-800'):
        sidebar_box = ui.column().classes('p-3 gap-0 w-full')

    with ui.footer().classes('bg-[#f6f6f6] text-gray-600 border-t border-gray-200'):
        with ui.row().classes('w-full items-center'):
            items_label = ui.label('…').classes('text-xs')
            ui.space()
            ui.label('Read-only NiceGUI demo').classes('text-[11px] text-gray-400')

    with ui.column().classes('w-full p-4 gap-2') as content:
        pass

    # ---- first paint ----------------------------------------------------------
    grid_btn.props('color=primary')
    list_btn.props('color=grey-7')
    hidden_btn.props('color=grey-7')
    navigate(HOME, push=False)


def main():
    ui.run(title='Finder — NiceGUI demo', port=PORT, reload=False, show=False, favicon='🖥️')


if __name__ == '__main__':
    main()
