#!/usr/bin/env python3
"""Render the report page from the synthetic demo dataset.

    python3 demo/render_demo.py                 # writes demo/out/demo-report.html
    python3 demo/render_demo.py --redact        # also writes the shareable copy
    python3 demo/render_demo.py --out some.html
    python3 demo/render_demo.py --site          # rewrites docs/index.html, the hosted live demo

Nothing on this computer is scanned and no history file is read or written:
every project, name, chat title, path and number comes from demo-scan.json,
which is invented. The page is drawn by the same render_html() a real scan
uses, so this is what a real report looks like.
"""
import argparse
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / 'scripts' / 'agent_copy_sort.py'
DATA = HERE / 'demo-scan.json'


def load_tool():
    spec = importlib.util.spec_from_file_location('agent_copy_sort', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def copy_path(home, tool, project, name):
    if tool == 'Claude Code':
        return f'{home}/code/{project}/.claude/worktrees/{name}'
    if tool == 'Codex':
        return f'{home}/.codex/worktrees/{name}/{project}'
    if tool == 'Cursor':
        return f'{home}/.cursor/worktrees/{project}/{name}'
    return f'/private/tmp/{name}'


def build(acs, data=None):
    """Return (report, history) in the shapes a real scan hands to render_html()."""
    data = data or json.loads(DATA.read_text())
    home = data['home']
    copies = []
    for group, tool, project, name, mb, touch, chat, why, codes in data['copies']:
        path = copy_path(home, tool, project, name)
        copies.append({'id': acs.copy_id(path), 'path': path, 'name': name, 'kind': tool, 'project': project,
                       'group': group, 'why': why, 'chat': chat, 'size_mb': mb, 'last_touch_days': touch,
                       'blockers': [{'code': c, 'text': why} for c in codes], 'git': {}})
    settings = {'home': home, 'recent_days': 4.0, 'roots': [], 'temp_roots': ['/private/tmp'],
                'codex_home': f'{home}/.codex', 'claude_dir': f'{home}/.claude', 'cursor_home': f'{home}/.cursor',
                'github': True, 'allow_caches': False, 'platform': 'Darwin'}
    stores = [
        {'name': 'Claude Code worktrees and repositories', 'path': f'{home}/code', 'state': 'searched', 'detail': ''},
        {'name': 'Codex worktrees', 'path': f'{home}/.codex/worktrees', 'state': 'searched', 'detail': ''},
        {'name': 'Cursor worktrees', 'path': f'{home}/.cursor/worktrees', 'state': 'searched', 'detail': ''},
        {'name': 'Temp folder clones', 'path': '/private/tmp', 'state': 'searched', 'detail': ''},
        {'name': 'Claude desktop sessions', 'path': f'{home}/Library/Application Support/Claude/claude-code-sessions',
         'state': 'searched', 'detail': '64 record(s)'},
        {'name': 'Claude Code transcripts', 'path': f'{home}/.claude/projects', 'state': 'searched',
         'detail': '118 transcript(s)'},
        {'name': 'Codex thread records', 'path': f'{home}/.codex/state.sqlite', 'state': 'searched', 'detail': ''},
    ]
    report = {
        'tool': acs.TOOL, 'plan_version': acs.PLAN_VERSION, 'plan_id': 'demo0000demo0000',
        'generated_at': data['generated_at'], 'settings': settings, 'stores': stores,
        'counts': {g: sum(1 for c in copies if c['group'] == g) for g in acs.GROUPS},
        'sizes_mb': {g: sum(c['size_mb'] or 0 for c in copies if c['group'] == g) for g in acs.GROUPS},
        'caches': {'allowed': False, 'folders': 0, 'size_mb': 0},
        'disk': data['disk'], 'leftovers': {'empty': data['empty_leftovers'], 'non_git': []},
        'commons': [], 'changed_note': 'No working copy was changed.', 'warnings': [], 'copies': copies,
    }
    now = data['now']
    scope = acs.history_scope(settings)
    history = [{'t': int(now - days * 86400), 'scope': scope, 'free': free, 'total': data['disk']['total_gb'],
                'snaps': None, 'n': n, 'mb': mb, 'partial': 0} for days, free, mb, n in data['history']]
    history.append(acs.history_point(report, now))
    return report, acs.clean_history(history, now)


REPO_URL = 'https://github.com/BTCElectrician/agent-copy-sort'
SITE_STYLE = ('<style>.site-strip{font-size:15.5px;padding:11px 16px;border:1px solid var(--rule);border-radius:6px;'
              'max-width:none}.site-strip a{font-weight:650;text-underline-offset:3px}</style>\n')
SITE_STRIP = ('<p class="site-strip"><b>Live demo.</b> This is the page agent-copy-sort writes after a scan. Every '
              f'project, name and number on it is invented. <a href="{REPO_URL}">Get the skill on GitHub</a></p>\n')


def site_page(acs, report, history):
    """The hosted live demo: the tool's own page, plus one strip at the top that
    says the data is invented and links to the repository. Nothing else differs."""
    page = acs.render_html(report, history=history)
    head, wrap = '</head><body>\n', '<div class="wrap">\n'
    assert page.count(head) == 1 and page.count(wrap) == 1
    return page.replace(head, SITE_STYLE + head).replace(wrap, wrap + SITE_STRIP)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=str(HERE / 'out' / 'demo-report.html'), help='where to write the page')
    ap.add_argument('--redact', action='store_true', help='also write the shareable copy next to it')
    ap.add_argument('--site', action='store_true', help='rewrite docs/index.html, the hosted live demo, and stop')
    args = ap.parse_args(argv)
    acs = load_tool()
    report, history = build(acs)
    if args.site:
        site = HERE.parent / 'docs' / 'index.html'
        site.write_text(site_page(acs, report, history))
        print(f'live demo page: {site}')
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(acs.render_html(report, history=history))
    print(f'page: {out}')
    if args.redact:
        share = out.with_name(out.stem + '-shareable' + out.suffix)
        share.write_text(acs.render_html(report, redact=True, history=history))
        print(f'shareable page: {share}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
