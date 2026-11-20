#!/usr/bin/env python3
"""Sort coding-agent working copies into four groups.

Claude Code, Codex and Cursor make a separate copy of a codebase for each task
(git worktrees or temp-folder clones). This tool finds those copies, decides
which are still being worked on, and writes a private HTML page plus a scan
plan (JSON).

Groups:
  inuse   In use right now: a program has its working folder or an open file
          inside it, or its open chat was active in the last 24 h and it has
          unsaved changes or an open PR.
  recent  Active in the last few days (owning chat or the copy's git activity).
  decide  Your call: holds work found nowhere else, or anything uncertain.
  done    Finished: nothing local-only found and its work is on a remote
          (cached evidence; checked live again before any removal).

Safety rule: anything uncertain, and any check that failed or did not finish,
puts a copy in "decide". `scan` never changes a working copy (it only cleans up
its own plan and page files older than 14 days). `remove` works from one named
scan plan, re-checks each copy on its own right before acting (identity,
running programs, chat records, git state, live remote) and once more after
the slow steps, and refuses anything that is no longer finished. A linked
worktree is removed with `git worktree remove` (no --force; its branch is
kept); a clone is moved to the Trash.

Removal assumes nothing writes into a copy while it runs: stop the agent or
program working there first. Checks before and after the slow steps catch most
changes, but a write that lands between the last check and the deletion can be
lost.

--time-limit is a best-effort budget: it bounds every git and helper command
and is checked between steps, but one long file copy or hash can overrun it.

Standard library only. Needs git; gh, lsof and tmutil are used when present.
"""
from __future__ import annotations

import argparse
import bisect
import contextlib
import errno
import glob
import hashlib
import html
import json
import math
import os
import platform
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TOOL = 'agent-copy-sort'
PLAN_VERSION = 3
DAY = 86400.0
GROUPS = ('inuse', 'recent', 'decide', 'done')
DEFAULT_ROOTS = ('Desktop', 'code', 'Projects', 'src', 'dev')
# Temp entries skipped by name. Only Claude Code's own per-user scratch folder
# (`claude-<uid>`) is skipped, not every name that starts with `claude-`.
TEMP_SKIP_RE = re.compile(r'^(claude-\d+|com\.apple.*|\..*|tmux-.*|launchd.*|agent-copy-sort.*)$')
DEFAULT_BRANCH_NAMES = ('main', 'master', 'trunk', 'develop')
OUTPUT_KEEP_DAYS = 14
SHA_RE = re.compile(r'^[0-9a-f]{40}([0-9a-f]{24})?$')


# ------------------------------------------------------------ time budget

class Budget:
    """A best-effort deadline on the monotonic clock.

    Every subprocess timeout is capped by it and long loops check it, but a
    single file copy, hash or database read in progress is not interrupted."""

    def __init__(self, seconds, clock=time.monotonic):
        self.clock = clock
        self.end = clock() + float(seconds)

    def left(self):
        return self.end - self.clock()

    def expired(self):
        return self.left() <= 0

    def cap(self, timeout):
        return min(float(timeout), self.left())


BUDGET = Budget(3600)


# ------------------------------------------------------------------ runner

class Res(tuple):
    """(rc, out, err). rc -1: failed or timed out; rc -2: time limit reached."""
    __slots__ = ()

    def __new__(cls, rc, out='', err=''):
        return tuple.__new__(cls, (rc, out, err))

    rc = property(lambda s: s[0])
    out = property(lambda s: s[1])
    err = property(lambda s: s[2])
    ok = property(lambda s: s[0] == 0)


ALLOWED_PROTOCOLS = 'https:ssh:git:file'


def _safe_env():
    # Drop every GIT_* override (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE,
    # GIT_CONFIG_PARAMETERS, ...) so the environment cannot redirect inspection.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_TERMINAL_PROMPT'] = '0'
    env['GIT_OPTIONAL_LOCKS'] = '0'
    # Default-deny transports: only these protocols may be used, after any URL
    # rewrite, so no git-remote-<name> helper and no ext:: command can run.
    env['GIT_ALLOW_PROTOCOL'] = ALLOWED_PROTOCOLS
    # Never fetch missing objects from a promisor remote while inspecting
    # (git 2.45+; older git ignores it, and partial clones are refused anyway).
    env['GIT_NO_LAZY_FETCH'] = '1'
    return env


SAFE_ENV = _safe_env()
# core.fsmonitor=false: never start a repository-configured fsmonitor helper.
# core.hooksPath=/dev/null: no hook can run. protocol.ext.allow=never: no
# command-running transports. --no-optional-locks: never take index locks in
# another agent's copy. None of this sandboxes git; repositories that define
# external filter programs locally are not inspected at all (see config_risks).
GIT_BASE = ['git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
            '-c', 'protocol.ext.allow=never', '-c', 'protocol.allow=never',
            *[x for p in ALLOWED_PROTOCOLS.split(':') for x in ('-c', f'protocol.{p}.allow=always')],
            '-c', 'fetch.recurseSubmodules=false', '--no-optional-locks']


def run(cmd, cwd=None, timeout=30, env=None, input_text=None, budget=None, stdin_path=None, stdout_path=None):
    """Run a command; never raises. Every timeout is capped by the overall budget.

    stdin_path / stdout_path stream a binary file in or out instead of text."""
    b = budget or BUDGET
    t = b.cap(timeout)
    if t <= 0:
        return Res(-2, '', 'time limit reached before this check ran')
    try:
        with contextlib.ExitStack() as st:
            fin = None
            if stdin_path:
                fin = st.enter_context(os.fdopen(os.open(stdin_path, os.O_RDONLY), 'rb'))
            fout = None
            if stdout_path:
                fd = os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                fout = st.enter_context(os.fdopen(fd, 'wb'))
            if fin or fout:
                p = subprocess.run(cmd, cwd=cwd, timeout=t, env=env if env is not None else SAFE_ENV,
                                   stdin=fin if fin else subprocess.DEVNULL,
                                   stdout=fout if fout else subprocess.PIPE, stderr=subprocess.PIPE)
                out = '' if fout else p.stdout.decode('utf-8', 'surrogateescape')
                if fout:
                    fout.flush()
                    os.fsync(fout.fileno())
                return Res(p.returncode, out, p.stderr.decode('utf-8', 'surrogateescape'))
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, errors='surrogateescape',
                               timeout=t, env=env if env is not None else SAFE_ENV,
                               input=input_text, stdin=None if input_text is not None else subprocess.DEVNULL)
            return Res(p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired:
        return Res(-1, '', f'timed out after {t:.0f}s')
    except (OSError, ValueError) as e:
        return Res(-1, '', str(e))


def git(path, *args, timeout=30, input_text=None):
    return run(GIT_BASE + ['-C', str(path), *args], timeout=timeout, input_text=input_text)


# ----------------------------------------------------------------- paths

def real(p):
    return os.path.realpath(os.path.expanduser(str(p)))


def _casefold_inside(child, parent):
    c, p = child.casefold(), parent.casefold()
    return c == p or c.startswith(p.rstrip('/') + '/')


def inside(child, parent):
    """True when child is parent or below it.

    Plain string containment first. On a case-insensitive volume a path that
    differs only in letter case is confirmed through filesystem identity
    (device and inode), never by lowercasing alone.
    """
    if child == parent or child.startswith(parent.rstrip('/') + '/'):
        return True
    if not _casefold_inside(child, parent):
        return False
    try:
        pst = os.stat(parent)
        depth = parent.rstrip('/').count('/')
        cur = child
        while cur.rstrip('/').count('/') > depth and cur not in ('/', ''):
            cur = os.path.dirname(cur)
        return os.path.samestat(os.stat(cur), pst)
    except OSError:
        return False


def copy_id(path):
    return hashlib.sha256(path.encode('utf-8', 'surrogateescape')).hexdigest()[:10]


def has_git(p):
    """True for a real repository or worktree, not a stray empty `.git` marker file."""
    g = os.path.join(p, '.git')
    try:
        if os.path.islink(g):
            return False
        if os.path.isdir(g):
            return os.path.exists(os.path.join(g, 'HEAD'))
        with open(g, 'rb') as fh:
            return fh.read(8) == b'gitdir: '
    except OSError:
        return False


def is_canonical(p):
    """No symlink anywhere in the path."""
    return real(p) == os.path.normpath(os.path.abspath(p))


def identity(path):
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return {'dev': st.st_dev, 'ino': st.st_ino, 'dir': stat.S_ISDIR(st.st_mode),
            'link': stat.S_ISLNK(st.st_mode)}


def same_identity(now, want):
    return bool(now and want) and not now['link'] and now['dir'] == want.get('dir', True) \
        and now['dev'] == want.get('dev') and now['ino'] == want.get('ino')


def mount_point(path):
    """The mount point holding path (path must exist)."""
    p = os.path.abspath(path)
    while not os.path.ismount(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


# --------------------------------------------------------- private output

class OutputError(Exception):
    pass


def ensure_private_dir(path, create=True):
    """Return a private (0700, owned by this user, not a symlink) directory."""
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if not create:
            raise OutputError(f'{path} does not exist.')
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkdir(path, 0o700)
        st = os.lstat(path)
        (path / '.agent-copy-sort').touch(mode=0o600)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise OutputError(f'{path} is a symlink or not a folder; choose another output folder.')
    if hasattr(os, 'getuid') and st.st_uid != os.getuid():
        raise OutputError(f'{path} belongs to another user; choose another output folder.')
    if st.st_mode & 0o022:
        raise OutputError(f'{path} can be written by other users; choose another output folder.')
    if st.st_mode & 0o077:
        if (path / '.agent-copy-sort').is_file():
            os.chmod(path, 0o700)
        else:
            raise OutputError(f'{path} can be read by other users; choose a new, private folder.')
    return path


def fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_private(path, data, overwrite=False, mode=0o600):
    """Create a private file without following symlinks, flushed to disk.

    Plans are never overwritten. With overwrite, a randomly named temp file is
    created exclusively next to it and renamed over it (a symlink at the final
    path is replaced, never followed)."""
    path = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
    target = path.with_name(f'.{path.name}.tmp-{secrets.token_hex(8)}') if overwrite else path
    fd = os.open(target, flags, mode)
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data.encode('utf-8', 'surrogateescape') if isinstance(data, str) else data)
            fh.flush()
            os.fsync(fh.fileno())
        if overwrite:
            os.replace(target, path)
    except BaseException:
        if overwrite:
            with contextlib.suppress(OSError):
                os.unlink(target)
        raise
    fsync_dir(path.parent)
    return path


def read_private_json(path, what='scan plan'):
    """Read a private JSON file through one O_NOFOLLOW descriptor, checked with fstat."""
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        raise OutputError(f'No {what} at {path}.')
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise OutputError(f'{path} is a symlink or not a regular file.')
        raise OutputError(f'{path} could not be opened ({e.strerror}).')
    with os.fdopen(fd, 'rb') as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise OutputError(f'{path} is a symlink or not a regular file.')
        if hasattr(os, 'getuid') and st.st_uid != os.getuid():
            raise OutputError(f'{path} belongs to another user.')
        if st.st_mode & 0o022:
            raise OutputError(f'{path} can be changed by other users.')
        data = fh.read()
    try:
        return json.loads(data.decode('utf-8', 'surrogateescape'))
    except ValueError:
        raise OutputError(f'{path} is not a valid {what}.')


def read_private_bytes(path):
    """Bytes of a regular file opened without following a final symlink, or None."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        return None
    with os.fdopen(fd, 'rb') as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return None
        return fh.read()


def default_state_dir(home):
    base = os.environ.get('XDG_STATE_HOME') if str(home) == str(Path.home()) else None
    return Path(base) / TOOL if base else Path(home) / '.local' / 'state' / TOOL


# ----------------------------------------------------------------- config

class Config:
    def __init__(self, home=None, roots=(), temp_roots=None, codex_home=None, claude_dir=None,
                 cursor_home=None, recent_days=4, github=True, time_limit=300, allow_caches=False):
        global BUDGET
        self.home = Path(real(home or Path.home()))
        self.recent_days = float(recent_days)
        self.allow_caches = bool(allow_caches)
        self.github_requested = bool(github)
        self.github = self.github_requested and shutil.which('gh') is not None
        default_roots = [self.home / r for r in DEFAULT_ROOTS]
        self.explicit_roots = [Path(real(r)) for r in roots]
        self.roots = [r for r in dict.fromkeys(Path(real(r)) for r in default_roots + self.explicit_roots)
                      if r.is_dir()]
        if temp_roots:
            temps = [Path(real(t)) for t in temp_roots]
        else:
            temps = [Path(real('/private/tmp' if sys.platform == 'darwin' else '/tmp')),
                     Path(real(tempfile.gettempdir()))]
        self.temp_roots = [t for t in dict.fromkeys(temps) if t.is_dir()]
        env_home = str(self.home) == str(Path.home())
        self.codex_home = Path(real(codex_home or (env_home and os.environ.get('CODEX_HOME'))
                                    or self.home / '.codex'))
        self.claude_dir = Path(real(claude_dir or (env_home and os.environ.get('CLAUDE_CONFIG_DIR'))
                                    or self.home / '.claude'))
        self.cursor_home = Path(real(cursor_home or self.home / '.cursor'))
        self.state_dir = str(default_state_dir(self.home))
        self.budget = Budget(time_limit)
        BUDGET = self.budget
        self.now = time.time()
        self.warnings = []
        self.stores = []
        self.owner_problems = []
        self.main_checkouts = set()
        self.source_repos = set(source_repositories(self.roots))
        self.discovery_incomplete = False
        self.copy_paths = set()

    def settings(self):
        return {'home': str(self.home), 'recent_days': self.recent_days,
                'roots': [str(r) for r in self.explicit_roots],
                'temp_roots': [str(t) for t in self.temp_roots], 'codex_home': str(self.codex_home),
                'claude_dir': str(self.claude_dir), 'cursor_home': str(self.cursor_home),
                'github': self.github, 'allow_caches': self.allow_caches, 'platform': platform.system()}

    def store(self, name, path, state, detail=''):
        self.stores.append({'name': name, 'path': str(path), 'state': state, 'detail': detail})


# --------------------------------------------------------------- discovery

def classify_kind(path, cfg):
    if '/.claude/worktrees/' in path + '/':
        return 'Claude Code'
    if inside(path, str(cfg.codex_home / 'worktrees')):
        return 'Codex'
    if inside(path, str(cfg.cursor_home / 'worktrees')):
        return 'Cursor'
    for t in cfg.temp_roots:
        if inside(path, str(t)):
            return 'Temp folder'
    return 'Other worktree'


def source_repositories(roots):
    """Repositories (a real `.git` folder) at or up to two levels below each
    search root: the originals. They are never removable, whatever folder
    they sit in."""
    out = set()
    for root in roots:
        for pat in ('.git', '*/.git', '*/*/.git'):
            for g in glob.glob(glob.escape(str(root)) + '/' + pat):
                if os.path.isdir(g) and not os.path.islink(g):
                    out.add(real(os.path.dirname(g)))
    return out


def _safe_iterdir(p):
    try:
        return sorted(Path(p).iterdir())
    except OSError:
        return []


LEFTOVER_MARKERS = {'.codex-worktree-name', '.DS_Store'}


def _is_empty_tree(p, limit=2000):
    """True when a folder holds nothing but empty folders and marker files."""
    n = 0
    for _root, _dirs, files in os.walk(p):
        if set(files) - LEFTOVER_MARKERS:
            return False
        n += 1
        if n > limit:
            return False
    return True


def parse_worktree_list(out):
    """Parse `git worktree list --porcelain -z` (NUL-delimited; paths may hold newlines)."""
    entries, cur = [], {}
    for field in out.split('\0'):
        if field == '':
            if cur:
                entries.append(cur)
                cur = {}
            continue
        key, _, val = field.partition(' ')
        cur[key] = val if val else True
    if cur:
        entries.append(cur)
    return [e for e in entries if 'worktree' in e]


def discover(cfg, owner_cwds):
    found = {}
    leftovers = {'empty': 0, 'non_git': []}
    skipped_links = []

    def add(p, require_git=True, canonical=True):
        if canonical and not is_canonical(p):
            skipped_links.append(str(p))  # a symlink could point at a main checkout
            return
        p = real(p)
        if os.path.isdir(p) and (has_git(p) or not require_git) and p not in found:
            found[p] = classify_kind(p, cfg)

    def out_of_time(where):
        if cfg.budget.expired():
            cfg.discovery_incomplete = True
            cfg.warnings.append(f'Discovery stopped at the time limit while searching {where}; '
                                'some copies may be missing from this report.')
            return True
        return False

    seeds = set()
    # Claude Code: <repo>/.claude/worktrees/<name> under each root. A folder
    # here without git data is still listed, as damaged, never as finished.
    for root in cfg.roots:
        if out_of_time(str(root)):
            break
        n = 0
        for pat in ('.claude/worktrees/*', '*/.claude/worktrees/*'):
            for p in glob.glob(glob.escape(str(root)) + '/' + pat):
                n += 1
                add(p, require_git=False)
        # Seed every repository under the root so its worktrees elsewhere are found.
        for pat in ('.git', '*/.git', '*/*/.git'):
            for g in glob.glob(glob.escape(str(root)) + '/' + pat):
                if os.path.isdir(g) and not os.path.islink(g):
                    seeds.add(os.path.dirname(g))
        cfg.store('Claude Code worktrees and repositories', root, 'searched',
                  f'{n} .claude/worktrees entr(ies)')

    # Codex: <codex home>/worktrees/<name> or .../<id>/<repo>; Cursor the same.
    for label, wt in (('Codex worktrees', cfg.codex_home / 'worktrees'),
                      ('Cursor worktrees', cfg.cursor_home / 'worktrees')):
        if not wt.is_dir():
            cfg.store(label, wt, 'absent')
            continue
        cfg.store(label, wt, 'searched')
        for top in _safe_iterdir(wt):
            if out_of_time(str(wt)):
                break
            if top.is_symlink():
                skipped_links.append(str(top))
                continue
            if not top.is_dir():
                continue
            if has_git(str(top)):
                add(top)
                continue
            kids = [k for k in _safe_iterdir(top) if k.is_dir() and has_git(str(k))]
            for k in kids:
                add(k)
            if not kids:
                if _is_empty_tree(top):
                    leftovers['empty'] += 1
                else:
                    leftovers['non_git'].append(str(top))

    # Temp folders: a clone at depth 1 or 2.
    for t in cfg.temp_roots:
        cfg.store('Temp folder clones', t, 'searched')
        for top in _safe_iterdir(t):
            if out_of_time(str(t)):
                break
            try:
                if TEMP_SKIP_RE.match(top.name) or not top.is_dir():
                    continue
                if top.is_symlink():
                    skipped_links.append(str(top))
                    continue
                if has_git(str(top)):
                    add(top)
                    continue
                for k in _safe_iterdir(top):
                    if k.is_dir() and has_git(str(k)):
                        add(k)
            except OSError:
                continue

    # Every repository seen (seeds, owner cwds, copies found so far): its worktrees.
    commons = set()
    for p in sorted(seeds) + list(owner_cwds) + list(found):
        if out_of_time('repository worktree lists'):
            break
        if not os.path.isdir(p):
            continue
        r = git(p, 'rev-parse', '--path-format=absolute', '--git-common-dir', timeout=10)
        if r.ok and r.out.strip():
            commons.add(real(r.out.strip()))
    for common in sorted(commons):
        if out_of_time('repository worktree lists'):
            break
        r = git(common, 'worktree', 'list', '--porcelain', '-z', timeout=20)
        if not r.ok:
            continue
        entries = parse_worktree_list(r.out)
        for i, e in enumerate(entries):
            p = e['worktree']
            if i == 0:
                cfg.main_checkouts.add(real(p))  # the main checkout is never a copy
            elif not e.get('bare'):
                add(p, canonical=False)  # git registered it; its identity is checked later
    if skipped_links:
        cfg.warnings.append(f'{len(skipped_links)} symlinked folder(s) were not followed, '
                            'so they cannot point the scan at another repository.')
    leftovers['skipped_links'] = skipped_links
    return found, leftovers, commons


# ------------------------------------------------------------------ owners

def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError('not a number')
    return float(v)


def _finite(v, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f'{what} is not a finite number')
    return float(v)


def _opt_bool(v, what):
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    raise ValueError(f'{what} is not true/false')


def _enumerate(base, depth, match, problems, label):
    """Files exactly `depth` folders below base whose name matches; every
    listing error is reported (glob would hide unreadable folders)."""
    level = [str(base)]
    for d in range(depth + 1):
        nxt = []
        for folder in level:
            try:
                with os.scandir(folder) as it:
                    entries = list(it)
            except FileNotFoundError:
                continue
            except OSError as e:
                problems.append(f'{label}: the folder {folder} could not be listed ({e.strerror}).')
                continue
            for e in entries:
                try:
                    if d < depth:
                        if e.is_dir(follow_symlinks=False):
                            nxt.append(e.path)
                    elif match(e.name) and e.is_file(follow_symlinks=False):
                        nxt.append(e.path)
                except OSError as err:
                    problems.append(f'{label}: {e.path} could not be checked ({err.strerror}).')
        level = nxt
    return sorted(level)


CWD_RE = re.compile(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _transcript_cwd(f, budget):
    """The first cwd recorded in a Claude Code transcript; reads past 64 KiB if needed."""
    with open(f, 'rb') as fh:
        head = fh.read(65536)
        m = CWD_RE.search(head)
        if m:
            return m.group(1), True
        tail = head[-4096:]
        while True:
            if budget.expired():
                raise TimeoutError('time limit')
            chunk = fh.read(1 << 20)
            if not chunk:
                return None, bool(head)
            buf = tail + chunk
            m = CWD_RE.search(buf)
            if m:
                return m.group(1), True
            tail = buf[-4096:]


def load_owners(cfg):
    """Return owner records: tool, cwd, title, archived, last (epoch s), id.

    A source that does not exist is recorded as absent. A source that exists
    but could not be listed or read, a record that is malformed, and a source
    only partly read are recorded in cfg.owner_problems; that blocks every
    copy from being called finished.
    """
    owners = []
    home = cfg.home
    probs = cfg.owner_problems

    # Claude desktop app session records (macOS and Linux locations).
    for base in (home / 'Library/Application Support/Claude/claude-code-sessions',
                 home / '.config/Claude/claude-code-sessions'):
        if not os.path.lexists(base):
            cfg.store('Claude desktop sessions', base, 'absent')
            continue
        files = _enumerate(base, 2, lambda n: n.startswith('local_') and n.endswith('.json'), probs,
                           'Claude desktop sessions')
        cfg.store('Claude desktop sessions', base, 'searched', f'{len(files)} record(s)')
        for f in files:
            if cfg.budget.expired():
                probs.append('Claude desktop session records were not all read before the time limit.')
                break
            try:
                d = json.loads(Path(f).read_text())
                if not isinstance(d, dict):
                    raise ValueError('record is not an object')
                last = _finite(d.get('lastActivityAt', 0), 'lastActivityAt') / 1000.0
                archived = _opt_bool(d.get('isArchived'), 'isArchived')
                title = d.get('title')
                if title is not None and not isinstance(title, str):
                    raise ValueError('title is not text')
                for key in ('worktreePath', 'cwd'):
                    v = d.get(key)
                    if v:
                        if not isinstance(v, str):
                            raise ValueError(f'{key} is not text')
                        owners.append({'tool': 'Claude', 'cwd': real(v), 'title': title or '',
                                       'archived': archived, 'last': last,
                                       'id': str(d.get('sessionId') or Path(f).stem)})
            except (OSError, ValueError, TypeError) as e:
                probs.append(f'A Claude desktop session record could not be used ({Path(f).name}: {e}).')

    # Claude CLI transcripts: cwd from the records, mtime = last activity.
    proj = cfg.claude_dir / 'projects'
    if os.path.lexists(proj):
        is_jsonl = lambda n: n.endswith('.jsonl')  # noqa: E731
        files = _enumerate(proj, 1, is_jsonl, probs, 'Claude Code transcripts')
        for d in _enumerate_dirs(proj, 2, 'subagents', probs, 'Claude Code transcripts'):
            files += _enumerate(d, 0, is_jsonl, probs, 'Claude Code transcripts')
        cfg.store('Claude Code transcripts', proj, 'searched', f'{len(files)} transcript(s)')
        for f in files:
            if cfg.budget.expired():
                probs.append('Claude Code transcripts were not all read before the time limit.')
                break
            try:
                raw, nonempty = _transcript_cwd(f, cfg.budget)
                if raw is None:
                    if nonempty:
                        probs.append(f'A Claude Code transcript names no working folder: {f}')
                    continue
                cwd = json.loads('"' + raw.decode('utf-8', 'replace') + '"')
                owners.append({'tool': 'Claude CLI', 'cwd': real(cwd), 'title': '', 'archived': False,
                               'last': os.path.getmtime(f), 'id': Path(f).stem})
            except TimeoutError:
                probs.append('Claude Code transcripts were not all read before the time limit.')
                break
            except (OSError, ValueError) as e:
                probs.append(f'A Claude Code transcript could not be used ({Path(f).name}: {e}).')
    else:
        cfg.store('Claude Code transcripts', proj, 'absent')

    # Codex: a consistent read-only snapshot through SQLite's backup API.
    dbs = []
    for s in _enumerate(cfg.codex_home, 0, lambda n: re.fullmatch(r'state_\d+\.sqlite', n) is not None, probs,
                        'Codex thread records') if os.path.lexists(cfg.codex_home) else []:
        dbs.append((int(re.fullmatch(r'state_(\d+)\.sqlite', Path(s).name).group(1)), s))
    if not dbs:
        cfg.store('Codex thread records', cfg.codex_home, 'absent')
    else:
        src = max(dbs)[1]
        cfg.store('Codex thread records', src, 'searched')
        try:
            rows = _read_codex_threads(src, cfg.budget)
        except (OSError, sqlite3.Error, TimeoutError) as e:
            rows = None
            probs.append(f'Codex thread records exist but could not be read ({type(e).__name__}: {e}).')
        for row in rows or []:
            try:
                tid, cwd, title, archived, updated = row
                if not cwd:
                    continue
                if not isinstance(cwd, str):
                    raise ValueError('cwd is not text')
                last = _finite(updated if updated is not None else 0, 'updated_at')
                if last > 1e12:
                    last /= 1000.0
                owners.append({'tool': 'Codex', 'cwd': real(cwd),
                               'title': str(title or '').split('\n')[0][:100],
                               'archived': _opt_bool(archived, 'archived'), 'last': last, 'id': str(tid)})
            except (ValueError, TypeError) as e:
                probs.append(f'A Codex thread record could not be used ({e}).')
    return owners


def _enumerate_dirs(base, depth, name, problems, label):
    """Folders called `name` exactly `depth` levels below base."""
    level = [str(base)]
    for d in range(depth + 1):
        nxt = []
        for folder in level:
            try:
                with os.scandir(folder) as it:
                    entries = list(it)
            except FileNotFoundError:
                continue
            except OSError as e:
                problems.append(f'{label}: the folder {folder} could not be listed ({e.strerror}).')
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False) and (d < depth or e.name == name):
                        nxt.append(e.path)
                except OSError as err:
                    problems.append(f'{label}: {e.path} could not be checked ({err.strerror}).')
        level = nxt
    return sorted(level)


def _read_codex_threads(src, budget=None):
    budget = budget or BUDGET
    wait = max(0.0, min(5.0, budget.left()))
    con = sqlite3.connect(f'file:{src}?mode=ro', uri=True, timeout=wait)
    mem = sqlite3.connect(':memory:')

    def progress(_status, _remaining, _total):
        if budget.expired():
            raise TimeoutError('time limit reached while reading Codex thread records')
    try:
        con.backup(mem, pages=256, progress=progress)  # one consistent snapshot, WAL included
        return mem.execute('select id, cwd, title, archived, updated_at from threads').fetchall()
    finally:
        con.close()
        mem.close()


def is_reviewer_title(title):
    t = (title or '').strip().lower()
    return not t or t.startswith('guardian review')


# --------------------------------------------------------------- processes

class ProcEvidence:
    """Which paths running programs use (working folder or an open file).

    state: ok, unavailable (no way to list), failed, or incomplete. Anything
    but ok blocks removal. Scope: this user's programs only; a program that
    touches a copy only briefly, between checks, cannot be seen.
    """

    def __init__(self, state, paths=None, detail='', accepted=(), unreadable=()):
        self.unreadable = list(unreadable)
        self.state = state
        self.paths = paths or {}
        self.detail = detail
        self.accepted = list(accepted)

    @property
    def ok(self):
        return self.state == 'ok'

    def inside(self, path):
        return sorted({p for d, ps in self.paths.items() if inside(d, path) for p in ps})


_readlink = os.readlink
# Programs whose details Linux hides from their own user (non-dumpable). They
# block removal unless named here or with --accept-unreadable. `(sd-pam)` is
# systemd's PAM session helper, present on most Linux desktops; it never works
# inside project folders.
DEFAULT_ACCEPT_UNREADABLE = ('(sd-pam)',)
ACCEPT_UNREADABLE = set(DEFAULT_ACCEPT_UNREADABLE)


def process_evidence(proc_root='/proc', platform_name=None):
    plat = platform_name or sys.platform
    paths = {}
    if plat.startswith('linux') and os.path.isdir(proc_root):
        uid = os.getuid()
        incomplete, accepted = [], []
        for pid in os.listdir(proc_root):
            if not pid.isdigit():
                continue
            base = os.path.join(proc_root, pid)
            try:
                owner = os.stat(base).st_uid
            except FileNotFoundError:
                continue  # exited
            except OSError:
                incomplete.append(f'?({pid})')
                continue
            if owner != uid:
                continue  # other users' programs are out of scope (documented)
            comm = '?'
            try:
                with open(os.path.join(base, 'comm')) as fh:
                    comm = fh.read().strip()
                names = [_readlink(os.path.join(base, 'cwd'))]
                fd_dir = os.path.join(base, 'fd')
                for fd in os.listdir(fd_dir):
                    try:
                        names.append(_readlink(os.path.join(fd_dir, fd)))
                    except FileNotFoundError:
                        continue
            except (FileNotFoundError, ProcessLookupError):
                continue  # exited while being read
            except OSError:
                # Same user but unreadable: cannot vouch for it, unless explicitly accepted.
                (accepted if comm in ACCEPT_UNREADABLE else incomplete).append(f'{comm}({pid})')
                continue
            for n in names:
                if n.startswith('/'):
                    paths.setdefault(real(n.removesuffix(' (deleted)')), set()).add(f'{comm}({pid})')
        if incomplete:
            names = sorted({x.split('(')[0] for x in incomplete})
            return ProcEvidence('incomplete', paths,
                                f"{len(incomplete)} of this user's programs could not be read: "
                                + ', '.join(incomplete[:6]) + '. If you know they are not working in these copies, '
                                + 'pass ' + ' '.join(f'--accept-unreadable {n!r}' for n in names[:4]), accepted,
                                incomplete)
        return ProcEvidence('ok', paths, '', accepted)
    if shutil.which('lsof') is None:
        return ProcEvidence('unavailable', {}, 'no process lister (lsof) found')
    r = run(['lsof', '-nP', '-w', '-u', str(os.getuid()), '-Fpcn'], timeout=60)
    if not r.ok or not r.out.strip():
        return ProcEvidence('failed', {}, f'lsof failed (exit {r.rc}): {r.err.strip()[:200]}')
    pid = comm = ''
    names = {}
    for line in r.out.splitlines():
        if line.startswith('p'):
            pid = line[1:]
        elif line.startswith('c'):
            comm = line[1:]
        elif line.startswith('n/'):
            names.setdefault(line[1:], set()).add(f'{comm}({pid})')
    for n, labels in names.items():
        paths.setdefault(real(n), set()).update(labels)
    return ProcEvidence('ok', paths)


# --------------------------------------------------------------- git facts

RISK_INSPECT = re.compile(r'^filter\..+\.(clean|smudge|process)$')
RISK_REMOTE = re.compile(r'^(core\.sshcommand|core\.gitproxy|core\.askpass|remote\..+\.(uploadpack|receivepack|vcs)'
                         r'|credential\..*helper)$')


def parse_config(out):
    """Parse `git config --list -z --show-scope` into (scope, key, value) triples."""
    parts = out.split('\0')
    items = []
    for i in range(0, len(parts) - 1, 2):
        scope, kv = parts[i], parts[i + 1]
        key, _, val = kv.partition('\n')
        items.append((scope, key.lower(), val))
    return items


def github_slug(url):
    m = re.search(r'github\.com[:/]+(.+?)(?:\.git)?/?$', (url or '').strip())
    return m.group(1) if m else None


def usable_remote(url, base, copy_paths):
    """A remote that can count as proof: a network remote, or a bare repository
    on this computer that is not inside any agent copy (which could be removed too)."""
    if not url:
        return False
    if not is_local_url(url):
        return True
    p = url[7:] if url.startswith('file://') else url
    p = real(p if os.path.isabs(p) else os.path.join(base, p))
    if any(inside(p, c) for c in copy_paths):
        return False
    return (os.path.isfile(os.path.join(p, 'HEAD')) and os.path.isdir(os.path.join(p, 'objects'))
            and not os.path.exists(os.path.join(p, '.git')))


def is_local_url(url):
    return bool(url) and (url.startswith('/') or url.startswith('file://') or url.startswith('.')
                          or not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://|^[^/]+@[^/]+:|^[^/:]+:', url))


def reflog_shas(logfile):
    """Commit ids one reflog names; [] when it does not exist, None when it
    exists but cannot be read as a regular file (a symlink, unreadable)."""
    out = []
    try:
        fd = os.open(logfile, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return out
    except OSError:
        return None
    try:
        with os.fdopen(fd, 'r', errors='replace') as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return None
            for line in fh:
                parts = line.split(' ', 2)
                if len(parts) >= 2:
                    for s in parts[:2]:
                        if SHA_RE.match(s) and set(s) != {'0'}:
                            out.append(s)
    except OSError:
        return None
    return out


def reflog_tips(logs, skip_remotes=False):
    """Commit ids named by every reflog under a git folder's logs/.

    Returns (shas, ok). ok is False when any part of the tree could not be
    read (an unreadable folder or file, or a symlink, which git does not
    write there): missing history evidence is uncertainty, never absence."""
    tips, errors = [], []

    def onerror(e):
        if not isinstance(e, FileNotFoundError):
            errors.append(e)

    for root, dirs, files in os.walk(logs, followlinks=False, onerror=onerror):
        if skip_remotes and os.path.relpath(root, logs).split(os.sep)[:2] == ['refs', 'remotes']:
            dirs[:] = []
            continue
        for d in dirs:
            if os.path.islink(os.path.join(root, d)):
                errors.append(d)
        for name in files:
            got = reflog_shas(os.path.join(root, name))
            if got is None:
                errors.append(name)
            else:
                tips += got
    return tips, not errors


# Single-value files git keeps in a git folder (per worktree) that can be the
# last pointer to a commit after its reflog is gone: ORIG_HEAD after a reset,
# state of an interrupted merge, rebase, cherry-pick, revert or bisect.
PSEUDOREFS = ('ORIG_HEAD', 'FETCH_HEAD', 'MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'REBASE_HEAD',
              'AUTO_MERGE', 'BISECT_HEAD', 'BISECT_EXPECTED_REV', 'BISECT_START',
              'rebase-merge/orig-head', 'rebase-merge/onto', 'rebase-merge/amend', 'rebase-merge/stopped-sha',
              'rebase-apply/orig-head', 'rebase-apply/onto', 'rebase-apply/abort-safety', 'sequencer/head',
              'sequencer/abort-safety')


def pseudoref_shas(gitdir):
    """Object ids held by this git folder's own pseudorefs. Returns (shas, ok);
    ok is False when one exists but cannot be read as a regular file."""
    out, ok = [], True
    for name in PSEUDOREFS:
        p = os.path.join(gitdir, *name.split('/'))
        try:
            os.lstat(p)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            ok = False
            continue
        raw = read_private_bytes(p)
        if raw is None:
            ok = False
            continue
        for ln in raw.decode('utf-8', 'replace').splitlines():
            tok = ln.split(None, 1)[0] if ln.strip() else ''
            if SHA_RE.match(tok) and set(tok) != {'0'}:
                out.append(tok)
    return out, ok


def object_types(path, names):
    """{name: (sha, type)} for each object that exists; None when git failed."""
    names = sorted(set(names))
    if not names:
        return {}
    r = git(path, 'cat-file', '--batch-check=%(objectname) %(objecttype)', input_text='\n'.join(names) + '\n')
    if not r.ok:
        return None
    out = {}
    for name, ln in zip(names, r.out.splitlines()):
        parts = ln.split()
        if len(parts) == 2 and parts[1] != 'missing':
            out[name] = (parts[0], parts[1])
    return out


def existing_objects(path, shas):
    """Split tips into commits (annotated tags peeled to their commit), annotated
    tag objects, and anything else (trees, blobs, tags of those).

    Returns (commits, tag_objects, other, ok). Nothing is dropped silently:
    a tag object and a non-commit object are kept, not treated as worthless."""
    types = object_types(path, shas)
    if types is None:
        return [], [], [], False
    commits, tags, other = set(), set(), set()
    for sha, typ in types.values():
        if typ == 'commit':
            commits.add(sha)
        elif typ == 'tag':
            tags.add(sha)
        else:
            other.add(sha)
    if tags:
        peeled = object_types(path, [t + '^{}' for t in tags])
        if peeled is None:
            return [], [], [], False
        for t in tags:
            got = peeled.get(t + '^{}')
            if got and got[1] == 'commit':
                commits.add(got[0])
            else:
                other.add(t)
    return sorted(commits), sorted(tags), sorted(other), True


def existing_commits(path, shas):
    commits, _tags, _other, ok = existing_objects(path, shas)
    return commits, ok


def count_not_reachable(path, tips, exclude_args=(), exclude_shas=()):
    """Commits reachable from tips but not from the exclusions; None when git failed."""
    if not tips:
        return 0
    lines = list(dict.fromkeys(tips)) + ['^' + s for s in dict.fromkeys(exclude_shas)]
    r = git(path, 'rev-list', '--count', '--stdin', *(['--not', *exclude_args] if exclude_args else []),
            input_text='\n'.join(lines) + '\n', timeout=60)
    if not r.ok or not r.out.strip().isdigit():
        return None
    return int(r.out.strip())


# Opt-in (--allow-caches): rebuildable cache folders, matched by folder name.
CACHE_DIR_NAMES = ('__pycache__', '.pytest_cache', '.ruff_cache', '.mypy_cache', '.next', 'node_modules')
VENV_DIR_NAMES = ('.venv', 'venv')  # only when the folder holds pyvenv.cfg
CACHEDIR_TAG_SIGNATURE = b'Signature: 8a477f597d28d172789f06886806bc55'
# Files that are never treated as rebuildable, even inside a cache folder,
# whether they are files or symlinks. One narrow, tool-defined exception: a
# virtual environment's bin/python* links, and only when they point at a
# regular executable file (removing a link never touches its target).
VENV_PYTHON_LINK_RE = re.compile(r'^python(\d+(\.\d+)?)?$')
NEVER_CACHE_RE = re.compile(r'^(\.env(\..*)?|.*\.(db|sqlite|sqlite3|pem|key|p12))$', re.IGNORECASE)


def cache_kind(path, rel):
    """The allowlist entry that makes path/rel a rebuildable cache folder, or None."""
    full = os.path.join(path, rel)
    if os.path.islink(full) or not os.path.isdir(full):
        return None
    name = os.path.basename(rel)
    if name in CACHE_DIR_NAMES:
        return name
    if name in VENV_DIR_NAMES and os.path.isfile(os.path.join(full, 'pyvenv.cfg')):
        return name
    try:
        with open(os.path.join(full, 'CACHEDIR.TAG'), 'rb') as fh:
            if fh.read(len(CACHEDIR_TAG_SIGNATURE)) == CACHEDIR_TAG_SIGNATURE:
                return 'CACHEDIR.TAG'
    except OSError:
        pass
    return None


def split_caches(path, ignored, tracked, budget, expand=None):
    """Split ignored entries into (still blocking, caches).

    An entry is exempt only when it sits entirely inside an allowlisted cache
    folder that holds no tracked file, no never-cache file (.env, databases,
    keys) and no symlink pointing outside the copy. Top-level files are never
    exempt.
    """
    # Compared case-insensitively: on a case-insensitive volume `Node_Modules/x`
    # in the index is the same file as `node_modules/x` on disk. Erring this way
    # can only keep more caches blocking.
    tracked = sorted(t.casefold() for t in tracked)

    def has_tracked(rel):
        rel = rel.casefold()
        i = bisect.bisect_left(tracked, rel + '/')
        return i < len(tracked) and tracked[i].startswith(rel + '/') or rel in tracked

    roots, verdict, kinds = {}, {}, {}
    blocking = []

    def kind_of(rel):
        if rel not in kinds:
            kinds[rel] = cache_kind(path, rel)
        return kinds[rel]

    queue, expanded = list(ignored), set()
    while queue:
        entry = queue.pop(0)
        parts = entry.rstrip('/').split('/')
        root = None
        for i in range(1, len(parts) + 1):
            rel = '/'.join(parts[:i])
            if rel in verdict or kind_of(rel):
                root = rel
                break
        if root is None:
            # A collapsed folder (e.g. `pkg/` holding only `pkg/__pycache__/`): look inside it once.
            if entry.endswith('/') and expand and entry not in expanded:
                expanded.add(entry)
                inner = expand(entry)
                if inner:
                    queue.extend(inner)
                    continue
            blocking.append(entry)
            continue
        if root not in verdict:
            verdict[root] = _check_cache(path, root, kind_of(root), has_tracked(root), budget)
        if verdict[root]['ok']:
            roots[root] = verdict[root]
        else:
            blocking.append(entry + ' (' + verdict[root]['why'] + ')')
    caches = [{'path': r, 'kind': v['kind'], 'size_mb': round(v['bytes'] / 1048576, 1)} for r, v in sorted(roots.items())]
    return blocking, caches


def _raise(err):
    raise err


def _check_cache(path, root, kind, tracked, budget):
    res = {'ok': False, 'kind': kind, 'bytes': 0, 'why': ''}
    if tracked:
        res['why'] = 'cache folder holds tracked files'
        return res
    top = real(path)
    base = os.path.join(path, root)
    try:
        for dirpath, dnames, fnames in os.walk(base, followlinks=False, onerror=_raise):
            if budget.expired():
                res['why'] = 'not fully checked before the time limit'
                return res
            for name in dnames + fnames:
                full = os.path.join(dirpath, name)
                if NEVER_CACHE_RE.match(name):  # checked first, for files and symlinks alike
                    res['why'] = f'{name} is not a cache file'
                    return res
                st = os.lstat(full)
                if stat.S_ISLNK(st.st_mode):
                    venv_python = kind in VENV_DIR_NAMES and os.path.basename(dirpath) == 'bin' \
                        and os.path.dirname(dirpath) == base and VENV_PYTHON_LINK_RE.match(name)
                    target = real(full)
                    if venv_python:
                        try:
                            tst = os.stat(full)
                        except OSError:
                            tst = None
                        if not tst or not stat.S_ISREG(tst.st_mode) or not tst.st_mode & 0o111:
                            res['why'] = f'symlink {name} does not point at a Python interpreter'
                            return res
                    elif not inside(target, top):
                        res['why'] = f'symlink {name} points outside the copy'
                        return res
                    elif NEVER_CACHE_RE.match(os.path.basename(target)):
                        res['why'] = f'symlink {name} points at {os.path.basename(target)}, not a cache file'
                        return res
                    if name in dnames:
                        dnames.remove(name)
                    continue
                if stat.S_ISDIR(st.st_mode):
                    continue
                if not stat.S_ISREG(st.st_mode):
                    res['why'] = f'{name} is a special file (socket, pipe or device)'
                    return res
                res['bytes'] += st.st_size
    except OSError as e:
        res['why'] = f'part of it could not be read ({e.strerror or e})'
        return res
    res['ok'] = True
    return res


def git_facts(path, cfg):
    """Read-only facts about one copy. Every failed query lands in f['unknown']."""
    f = {'ok': False, 'unknown': []}
    if not has_git(path):
        return f  # without its own git data, git would answer for an enclosing repository
    lines = []
    for opt in ('--show-toplevel', '--absolute-git-dir', '--git-common-dir'):
        r = git(path, 'rev-parse', '--path-format=absolute', opt)
        if not r.ok:
            if r.rc == -2:
                f['unknown'].append('time limit')
            return f
        lines.append(r.out[:-1] if r.out.endswith('\n') else r.out)  # paths may hold newlines
    if real(lines[0]) != path:
        return f
    f['ok'] = True
    unknown = f['unknown']
    gitdir, common = real(lines[1]), real(lines[2])
    f.update(gitdir=gitdir, common=common)
    cname = Path(common).name
    f['repo'] = Path(common).parent.name if cname == '.git' else re.sub(r'\.git$', '', cname)
    dotgit = os.path.join(path, '.git')
    f['worktree'] = os.path.isfile(dotgit)
    # A `.git` file that does not point into <common>/worktrees/ is a submodule
    # checkout or a --separate-git-dir repository: its git data lives elsewhere.
    f['foreign_gitdir'] = f['worktree'] and os.path.dirname(gitdir) != os.path.join(common, 'worktrees')
    f['standalone'] = not f['worktree'] and gitdir == real(dotgit)

    rc = git(path, 'config', '--list', '-z', '--show-scope')
    if not rc.ok:
        unknown.append('git config')
        cfg_items = []
    else:
        cfg_items = parse_config(rc.out)
    local = [(k, v) for s, k, v in cfg_items if s in ('local', 'worktree', 'command')]
    f['risk_inspect'] = sorted({k for k, _ in local if RISK_INSPECT.match(k)})
    f['risk_remote'] = sorted({k for k, _ in local if RISK_REMOTE.match(k)})
    f['remotes'] = {k[len('remote.'):-len('.url')]: v for _s, k, v in cfg_items
                    if k.startswith('remote.') and k.endswith('.url')}
    f['slug'] = github_slug(f['remotes'].get('origin'))
    f['gitdir_id'], f['common_id'] = identity(gitdir), identity(common)
    # Unsupported arrangements are refused before any object is inspected:
    # a partial clone could fetch missing objects (running remote helpers);
    # borrowed objects (alternates) live in another repository that the
    # checks here would not cover.
    unsupported = []
    if any(k == 'extensions.partialclone' or (k.startswith('remote.') and k.endswith('.promisor')
                                              and v.strip().lower() in ('true', 'yes', 'on', '1'))
           for _s, k, v in cfg_items) or os.path.exists(os.path.join(common, 'objects', 'info', 'promisor')):
        unsupported.append('it is a partial clone (objects are fetched on demand from a promisor remote)')
    alts = []
    for name in ('alternates', 'http-alternates'):
        raw = read_private_bytes(os.path.join(common, 'objects', 'info', name))
        if raw is None and os.path.lexists(os.path.join(common, 'objects', 'info', name)):
            unknown.append('object alternates')
        for ln in (raw or b'').decode('utf-8', 'surrogateescape').splitlines():
            ln = ln.strip()
            if ln and not ln.startswith('#'):
                alts.append(real(ln if os.path.isabs(ln) else os.path.join(common, 'objects', ln)))
    f['alternates'] = alts
    if alts and not f['worktree']:
        unsupported.append('its git objects are borrowed from another repository (alternates: '
                           + ', '.join(alts[:2]) + ')')
    f['unsupported'] = unsupported
    if unsupported:
        f['remotes'] = {}
        return f

    r = git(path, 'branch', '--show-current')
    f['branch'] = r.out.strip() if r.ok else ''
    if not r.ok:
        unknown.append('current branch')
    r = git(path, 'rev-parse', '-q', '--verify', 'HEAD^{commit}')
    f['head'] = r.out.strip() if r.ok else None
    f['head_short'] = (f['head'] or '')[:9]
    if not r.ok:
        r2 = git(path, 'symbolic-ref', '-q', 'HEAD')
        if not r2.ok:
            unknown.append('HEAD')  # neither a commit nor an unborn branch: damaged

    if f['risk_inspect']:
        # Reading file status could run these programs; do not inspect at all.
        f['changed'] = f['ignored'] = f['hidden'] = None
        f['changed_sample'] = f['ignored_sample'] = f['hidden_sample'] = []
        f['caches'] = []
    else:
        r = git(path, 'status', '--porcelain=v1', '-z', '--untracked-files=all', '--ignore-submodules=none',
                timeout=90)
        if r.ok:
            entries, parts, i = [], r.out.split('\0'), 0
            while i < len(parts):
                e = parts[i]
                if e:
                    entries.append(e[3:])
                    if e[0] in 'RC':
                        i += 1  # the rename source follows
                i += 1
            f['changed'], f['changed_sample'] = len(entries), entries[:6]
        else:
            unknown.append('file status')
            f['changed'], f['changed_sample'] = None, []
        r = git(path, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard', '--directory', timeout=90)
        ign = [x for x in r.out.split('\0') if x] if r.ok else None
        if not r.ok:
            unknown.append('ignored files')
        f['caches'] = []
        r = git(path, 'ls-files', '-z', '-s', '-v', timeout=90)
        tracked_names = []
        if r.ok:
            hidden, gitlinks = [], 0
            for e in r.out.split('\0'):
                if not e:
                    continue
                tag, rest = e[0], e[2:]
                meta, _, name = rest.partition('\t')
                tracked_names.append(name)
                if tag == 'S' or tag.islower():
                    hidden.append(name)
                if meta.startswith('160000'):
                    gitlinks += 1
            f['hidden'], f['hidden_sample'], f['gitlinks'] = len(hidden), hidden[:6], gitlinks
        else:
            unknown.append('index flags')
            f['hidden'], f['hidden_sample'], f['gitlinks'] = None, [], 0
        if ign is not None and cfg.allow_caches and r.ok:
            def expand(entry):
                r2 = git(path, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard', '--', entry,
                         timeout=90)
                files = [x for x in r2.out.split('\0') if x] if r2.ok else None
                if not files:
                    return None
                return sorted(set(files))
            ign, f['caches'] = split_caches(path, ign, tracked_names, cfg.budget, expand)
        f['ignored'], f['ignored_sample'] = (len(ign), ign[:6]) if ign is not None else (None, [])
    f['submodules'] = os.path.exists(os.path.join(path, '.gitmodules')) or bool(f.get('gitlinks'))
    r = git(path, 'ls-files', '-z', '--', ':(attr:filter=lfs)', timeout=60)
    f['lfs'] = bool(r.out.strip('\0')) if r.ok else None
    if not r.ok:
        unknown.append('LFS check')

    f['has_remote'] = bool(f['remotes'])
    f['unpushed'] = None
    if f['head']:
        n = count_not_reachable(path, [f['head']], ['--remotes'])
        if n is None:
            unknown.append('commits not on a remote')
        f['unpushed'] = n
    dref = None
    r = git(path, 'symbolic-ref', '-q', 'refs/remotes/origin/HEAD')
    if r.ok and r.out.strip():
        dref = r.out.strip()
    else:
        for b in DEFAULT_BRANCH_NAMES:
            if git(path, 'rev-parse', '-q', '--verify', f'refs/remotes/origin/{b}').ok:
                dref = f'refs/remotes/origin/{b}'
                break
    f['default_ref'] = dref
    f['in_default'] = bool(dref and f['head']) and git(path, 'merge-base', '--is-ancestor', 'HEAD', dref).ok
    f['branch_on_remote'] = bool(f['branch']) and git(
        path, 'rev-parse', '-q', '--verify', f"refs/remotes/origin/{f['branch']}").ok

    # History that exists only in this copy and would be lost with it.
    f['private_tips'], f['private_unpublished'] = [], 0
    if f['worktree'] and not f['foreign_gitdir']:
        tips, logs_ok = reflog_tips(os.path.join(gitdir, 'logs'))
        if not logs_ok:
            unknown.append('worktree reflogs')
        r = git(path, 'for-each-ref', '--format=%(objectname)', 'refs/worktree/', 'refs/bisect/',
                'refs/rewritten/')
        tips += r.out.split() if r.ok else []
        if not r.ok:
            unknown.append('worktree-private refs')
        tips += _private_pseudorefs(path, gitdir, unknown)
        if f['head']:
            tips.append(f['head'])
        tips, tag_objs, other, ok = existing_objects(path, tips)
        if not ok:
            unknown.append('worktree history')
        f['private_tips'] = tips
        # Tag objects, trees or blobs kept only by this worktree's own refs.
        f['private_objects'] = tag_objs + other
        # Branches and tags stay in the shared repository after a worktree is removed.
        n = count_not_reachable(path, tips, ['--remotes', '--branches', '--tags'])
        if n is None:
            unknown.append('worktree history')
        f['private_unpublished'] = n
    elif f['standalone']:
        r = git(path, 'for-each-ref', '--format=%(objectname) %(refname)')
        tips = []
        if r.ok:
            for ln in r.out.splitlines():
                sha, _, ref = ln.partition(' ')
                if not ref.startswith('refs/remotes/'):
                    tips.append(sha)
        else:
            unknown.append('local refs')
        logged, logs_ok = reflog_tips(os.path.join(gitdir, 'logs'), skip_remotes=True)
        tips += logged
        if not logs_ok:
            unknown.append('reflogs')
        tips += _private_pseudorefs(path, gitdir, unknown)
        tips, tag_objs, other, ok = existing_objects(path, tips + ([f['head']] if f['head'] else []))
        if not ok:
            unknown.append('local history')
        f['private_tips'] = tips
        f['tag_objects'] = tag_objs  # each must be advertised by the live remote before removal
        f['private_objects'] = other  # refs to trees or blobs: no remote check vouches for them
        n = count_not_reachable(path, tips, ['--remotes'])
        if n is None:
            unknown.append('local history')
        f['private_unpublished'] = n

    # A repository whose .git folder other worktrees depend on.
    f['dependents'] = []
    if f['standalone']:
        r = git(path, 'worktree', 'list', '--porcelain', '-z', timeout=20)
        if r.ok:
            f['dependents'] = [e['worktree'] for e in parse_worktree_list(r.out)[1:]]
        else:
            unknown.append('worktree list')

    stamps = []
    for x in [path] + [os.path.join(gitdir, n) for n in ('index', 'HEAD', 'logs/HEAD')]:
        try:
            stamps.append(os.path.getmtime(x))
        except OSError:
            pass
    f['last_touch'] = max(stamps) if stamps else None
    if cfg.budget.expired():
        unknown.append('time limit')
    return f


def _private_pseudorefs(path, gitdir, unknown):
    """Pseudoref values (ORIG_HEAD, FETCH_HEAD, ...) to add to the private
    history tips, so a commit kept only by ORIG_HEAD blocks removal like a
    reflog-only commit. A value that a ref also holds is left to that ref's
    own check (a plain `git fetch` writes every fetched branch and tag into
    FETCH_HEAD and into refs/remotes or refs/tags)."""
    shas, ok = pseudoref_shas(gitdir)
    if not ok:
        unknown.append('pseudorefs')
    shas = list(dict.fromkeys(shas))
    if not shas:
        return []
    r = git(path, 'for-each-ref', '--format=%(objectname)')
    if not r.ok:
        unknown.append('pseudorefs')
        return shas
    named = set(r.out.split())
    return [s for s in shas if s not in named]


def size_mb(path):
    r = run(['du', '-sk', path], timeout=90)
    try:
        return int(r.out.split()[0]) // 1024
    except (IndexError, ValueError):
        return None


# ------------------------------------------------------------ pull requests

PR_FIELDS = 'number,state,title,headRefName,headRefOid,isCrossRepository'
PR_LIMIT = 1000


def pr_list(slug):
    """All PRs for a repository. Returns (items or None, complete)."""
    r = run(['gh', 'pr', 'list', '--repo', slug, '--state', 'all', '--limit', str(PR_LIMIT),
             '--json', PR_FIELDS], timeout=90)
    if not r.ok:
        return None, False
    try:
        items = json.loads(r.out)
    except ValueError:
        return None, False
    return items, len(items) < PR_LIMIT


def pr_for_branch(slug, branch):
    """PRs for one branch (used right before removal)."""
    r = run(['gh', 'pr', 'list', '--repo', slug, '--state', 'all', '--head', branch, '--limit', '50',
             '--json', PR_FIELDS], timeout=60)
    if not r.ok:
        return None, False
    try:
        items = json.loads(r.out)
    except ValueError:
        return None, False
    return items, len(items) < 50


def attach_prs(row, items, complete, path):
    """Keep PRs from this repository whose head branch matches; note whether one covers HEAD."""
    f = row['git']
    if items is None:
        row['pr'] = {'state': 'unavailable', 'items': []}
        return
    same = [p for p in items if f.get('branch') and p.get('headRefName') == f['branch']
            and not p.get('isCrossRepository')]
    covered = None
    for p in same:
        if p.get('state') != 'MERGED' or not f.get('head'):
            continue
        oid = p.get('headRefOid') or ''
        if oid == f['head'] or (SHA_RE.match(oid) and git(path, 'merge-base', '--is-ancestor', f['head'], oid).ok):
            covered = p['number']
            break
    forks = sum(1 for p in items if f.get('branch') and p.get('headRefName') == f['branch']
                and p.get('isCrossRepository'))
    row['pr'] = {'state': 'ok' if complete or same else 'incomplete',
                 'items': [{'number': p['number'], 'state': p['state'], 'title': p.get('title', ''),
                            'head_oid': p.get('headRefOid')} for p in same],
                 'covers_head': covered, 'fork_matches': forks}


# ------------------------------------------------------------ classification

AGENT_KINDS = ('Claude Code', 'Codex', 'Cursor', 'Temp folder')
REDACTED_TEXT = {
    'timeout': 'Not fully checked before the time limit.',
    'proc-check': 'Running programs could not be fully checked.',
    'owners-incomplete': 'Chat records could not all be read.',
    'git-missing': 'Git data is missing or damaged.',
    'unknown': 'Some git checks failed.',
    'protected-main': 'This is a main checkout.',
    'dependents': 'Other working copies depend on its git data.',
    'foreign-gitdir': 'Its git data lives elsewhere (submodule or separate git folder).',
    'contains': 'Another repository or copy is inside it.',
    'helpers': 'Its settings run external programs, so it was not inspected.',
    'unsupported': 'Unsupported git setup (partial clone or borrowed objects).',
    'dirty': 'Unsaved or untracked files.',
    'ignored': 'Ignored files that exist only here.',
    'hidden': 'Files hidden from git status.',
    'submodules': 'Has submodules.',
    'lfs': 'Uses Git LFS.',
    'unpublished': 'Commits not on any remote.',
    'private-history': 'Local-only history (tags, refs or reflog).',
    'open-pr': 'Pull request still open.',
    'no-pr': 'Pushed branch with no pull request.',
    'closed-pr': 'Pull request closed without merging.',
    'pr-unknown': 'Pull requests could not be fully checked.',
    'no-remote': 'No usable remote to compare against.',
    'empty-repo': 'Repository has no commits.',
    'local-remote': 'Its only remote is another folder on this computer.',
}


def _b(code, text):
    return {'code': code, 'text': text}


def blockers_for(row, cfg):
    """Everything that stops this copy from being finished. Empty list means finished."""
    f = row['git']
    out = []
    if row.get('timed_out') or 'time limit' in f.get('unknown', []):
        out.append(_b('timeout', 'Not fully checked before the time limit; run the scan again.'))
    pe = row.get('proc_state', 'ok')
    if pe != 'ok':
        detail = row.get('proc_detail') or ''
        out.append(_b('proc-check', f'Running programs could not be checked ({pe}' + (f': {detail}' if detail else '') + ').'))
    if cfg.owner_problems:
        out.append(_b('owners-incomplete', 'Some chat records exist but could not be read, '
                                           'so an active chat could be missed.'))
    if not f.get('ok'):
        out.append(_b('git-missing', 'Git metadata is missing or damaged, so its contents cannot be checked.'))
        return out
    unknown = [u for u in f['unknown'] if u != 'time limit']
    if unknown:
        out.append(_b('unknown', 'These git checks failed, so nothing is assumed: ' + ', '.join(sorted(set(unknown))) + '.'))
    if row['path'] in cfg.source_repos:
        out.append(_b('protected-main', 'This is a repository found under your search folders (or a --root), '
                                        'the original, never a disposable copy.'))
    elif row['path'] in cfg.main_checkouts and row.get('kind') not in AGENT_KINDS:
        out.append(_b('protected-main', 'This is the main checkout of a repository, never a disposable copy.'))
    if f.get('unsupported'):
        out.append(_b('unsupported', 'Not supported, so it was not inspected further: '
                      + '; '.join(f['unsupported']) + '.'))
        return out
    if f.get('dependents'):
        out.append(_b('dependents', f"{len(f['dependents'])} other working cop(ies) use this repository's git data."))
    if f.get('foreign_gitdir'):
        out.append(_b('foreign-gitdir', 'Its git data lives outside it (a submodule or separate git folder).'))
    if row.get('contains'):
        out.append(_b('contains', f"It contains another repository or copy: {row['contains'][0]}."))
    if f.get('risk_inspect'):
        out.append(_b('helpers', 'Its own git settings define external filter programs ('
                      + ', '.join(f['risk_inspect']) + '); it was not inspected so they would not run.'))
    if f.get('changed'):
        out.append(_b('dirty', f"{f['changed']} unsaved or untracked file(s), e.g. "
                      + ', '.join(f['changed_sample'][:3]) + '.'))
    if f.get('ignored'):
        out.append(_b('ignored', f"{f['ignored']} ignored file(s) or folder(s) that exist only here, e.g. "
                      + ', '.join(f['ignored_sample'][:4]) + '. Removal would delete them.'))
    if f.get('hidden'):
        out.append(_b('hidden', f"{f['hidden']} tracked file(s) are marked assume-unchanged or skip-worktree, "
                      'so git status cannot vouch for them, e.g. ' + ', '.join(f['hidden_sample'][:3]) + '.'))
    if f.get('submodules'):
        out.append(_b('submodules', 'It has submodules, whose own unpublished work is not checked.'))
    if f.get('lfs'):
        out.append(_b('lfs', 'It uses Git LFS; large files are not proven to be on the LFS server.'))

    branch = f.get('branch') or '(detached)'
    pr = row.get('pr') or {'state': 'off', 'items': []}
    prs = pr['items']
    open_prs = [p for p in prs if p['state'] == 'OPEN']
    merged = [p for p in prs if p['state'] == 'MERGED']
    covered = pr.get('covers_head')
    if open_prs:
        out.append(_b('open-pr', f"Pull request #{open_prs[0]['number']} is still open."))
    if not f.get('head'):
        out.append(_b('empty-repo', 'The repository has no commits yet.'))
    else:
        usable = [n for n, u in f['remotes'].items() if usable_remote(u, row['path'], cfg.copy_paths)]
        local_only = f['has_remote'] and not usable
        published = f.get('in_default') or f.get('unpushed') == 0 or covered
        if not published:
            if not f['has_remote']:
                out.append(_b('no-remote', 'No remote to compare against, so nothing proves the work is saved elsewhere.'))
            elif f.get('unpushed'):
                extra = ''
                if merged:
                    extra = (f" Pull request #{merged[0]['number']} merged earlier, but this copy has commits made "
                             'after it.')
                out.append(_b('unpublished', f"{f['unpushed']} commit(s) on {branch} are not on any remote.{extra}"))
        elif local_only and not covered:
            out.append(_b('local-remote', 'Its remote is another folder on this computer, which may itself be '
                                          'a copy; that is not proof the work is saved.'))
        if f.get('private_unpublished'):
            where = 'tags, other refs or reflog' if f.get('standalone') else 'its own reflog or worktree refs'
            out.append(_b('private-history', f"{f['private_unpublished']} commit(s) are kept only by {where} "
                                              'in this copy and are not on any remote or branch.'))
        if f.get('private_objects'):
            out.append(_b('private-history', f"{len(f['private_objects'])} ref(s) here point at tag objects, trees "
                                              'or files rather than commits; no remote check vouches for them.'))
        if not f.get('in_default') and not covered:
            if f['slug'] and pr['state'] in ('unavailable', 'incomplete') and cfg.github:
                out.append(_b('pr-unknown', f'Pull requests for {branch} could not be fully checked.'))
            elif f['slug'] and pr['state'] == 'ok' and not prs and f.get('branch') \
                    and f['branch'] not in DEFAULT_BRANCH_NAMES and f.get('branch_on_remote'):
                out.append(_b('no-pr', f'Branch {branch} is on GitHub but has no pull request.'))
            elif prs and not open_prs and not merged:
                out.append(_b('closed-pr', f"Pull request #{prs[0]['number']} was closed without merging."))
    return out


def classify(row, cfg):
    """Set row['group'], row['why'], row['blockers']. Uncertain -> decide."""
    f = row['git']
    now = cfg.now
    procs = row['processes']
    live_owners = [o for o in row['owners'] if not o['archived']]
    newest_owner = max((o['last'] for o in live_owners), default=0)
    owner_days = (now - newest_owner) / DAY if newest_owner else None
    touch_days = (now - f['last_touch']) / DAY if f.get('last_touch') else None
    blockers = blockers_for(row, cfg)
    row['blockers'] = blockers
    open_prs = [p for p in (row.get('pr') or {}).get('items', []) if p['state'] == 'OPEN']

    if row.get('timed_out'):
        return 'decide', 'Not fully checked before the time limit; run the scan again.'
    if procs:
        names = ', '.join(sorted({p.split('(')[0] for p in procs})[:4])
        return 'inuse', f'{len(procs)} running program(s) work inside it ({names}).'
    if not f.get('ok'):
        return 'decide', ' '.join(b['text'] for b in blockers)
    dirty = bool(f.get('changed')) or f.get('changed') is None
    if owner_days is not None and owner_days <= 1 and (dirty or open_prs):
        what = (f"{f['changed']} unsaved file(s)" if f.get('changed') else 'unchecked files') if dirty \
            else f"open pull request #{open_prs[0]['number']}"
        return 'inuse', f'Its chat was active in the last day and it has {what}.'
    recent = []
    if owner_days is not None and owner_days <= cfg.recent_days:
        recent.append(f'its chat was active {_ago(owner_days)}')
    if touch_days is not None and touch_days <= cfg.recent_days:
        recent.append(f'git activity {_ago(touch_days)}')
    if recent:
        return 'recent', 'Active recently: ' + ' and '.join(recent) + '.'
    if blockers:
        return 'decide', ' '.join(b['text'] for b in blockers) + cache_note(f)
    pr = row.get('pr') or {}
    if pr.get('covers_head'):
        why = f"Work merged (pull request #{pr['covers_head']} includes this exact commit)."
    elif f.get('in_default'):
        why = 'Everything here is already in the main branch.'
    else:
        why = 'Work is saved on a remote branch.'
    return 'done', why + ' Nothing local-only found.' + cache_note(f) + \
        ' (Remote evidence is cached; it is checked live again before removal.)'


def cache_note(f):
    caches = f.get('caches') or []
    if not caches:
        return ''
    kinds = sorted({c['kind'] for c in caches})
    mb = sum(c['size_mb'] for c in caches)
    return (f' Rebuildable caches treated as removable (--allow-caches): {len(caches)} folder(s), '
            + ', '.join(kinds) + f', {mb:.0f} MB.')


def _ago(days):
    if days < 1:
        return f'{max(1, round(days * 24))} hour(s) ago'
    return f'{days:.1f} days ago'


def owner_label(owners):
    if not owners:
        return ''
    named = [o for o in owners if not is_reviewer_title(o['title'])]
    pick = max(named or owners, key=lambda o: o['last'])
    title = pick['title'] or ('background helper' if pick['tool'] == 'Claude CLI' else 'untitled')
    return f"{pick['tool']}: {title}" + (' (archived)' if pick['archived'] else '')


def inspect_one(path, kind, cfg, owners, procev, others=(), with_size=True):
    row = {'id': copy_id(path), 'path': path, 'name': Path(path).name, 'kind': kind,
           'proc_state': procev.state, 'proc_detail': procev.detail}
    if cfg.budget.expired():
        row.update(timed_out=True, git={'ok': False, 'unknown': ['time limit']}, owners=[], processes=[],
                   size_mb=None, identity=identity(path))
        return row
    row['identity'] = identity(path)
    row['git'] = git_facts(path, cfg)
    row['project'] = row['git'].get('repo') or Path(path).parent.name
    row['owners'] = [dict(o) for o in owners if inside(o['cwd'], path)]
    row['processes'] = procev.inside(path)
    own = {path, row['git'].get('common'), row['git'].get('gitdir')}
    row['contains'] = sorted(o for o in others if o not in own and inside(o, path))
    row['size_mb'] = size_mb(path) if with_size and not cfg.budget.expired() else None
    return row


def finish_row(r, cfg):
    f = r['git']
    r['group'], r['why'] = classify(r, cfg)
    r['chat'] = owner_label(r['owners'])
    r['last_touch_days'] = round((cfg.now - f['last_touch']) / DAY, 1) if f.get('last_touch') else None
    r.setdefault('project', Path(r['path']).parent.name)
    for o in r['owners']:
        o['idle_days'] = round((cfg.now - o['last']) / DAY, 1)
    return r


def inspect_copies(paths, cfg, owners, procev, commons=()):
    """Collect facts and classify each path. paths: {real_path: kind}."""
    others = sorted(set(paths) | {real(c) for c in commons} | cfg.main_checkouts)
    cfg.copy_paths = set(paths)

    with ThreadPoolExecutor(8) as ex:
        rows = list(ex.map(lambda it: inspect_one(it[0], it[1], cfg, owners, procev, others),
                           sorted(paths.items())))
    add_alternate_dependents(rows, commons)
    pr_cache = {}
    if cfg.github:
        for slug in sorted({r['git'].get('slug') for r in rows if r['git'].get('slug')}):
            if cfg.budget.expired():
                cfg.warnings.append('Pull-request lookups stopped at the time limit.')
                break
            pr_cache[slug] = pr_list(slug)
            if pr_cache[slug][0] is None:
                cfg.warnings.append(f'Pull requests for {slug} could not be listed.')
            elif not pr_cache[slug][1]:
                cfg.warnings.append(f'Pull requests for {slug} hit the {PR_LIMIT} limit; older ones were not seen.')
    for r in rows:
        slug = r['git'].get('slug')
        if slug and cfg.github:
            items, complete = pr_cache.get(slug, (None, False))
            attach_prs(r, items, complete, r['path'])
        else:
            r['pr'] = {'state': 'off', 'items': []}
        finish_row(r, cfg)
    return rows


def alternates_of(common):
    """Object folders a repository borrows from (objects/info/alternates).

    Returns (folders, ok). ok is False when the file (or a folder above it)
    exists but cannot be read as a regular file, a symlink included: which
    repository it borrows from is then unknown, not "none"."""
    out = []
    p = os.path.join(common, 'objects', 'info', 'alternates')
    try:
        os.lstat(p)
    except (FileNotFoundError, NotADirectoryError):
        return out, True
    except OSError:
        return out, False
    raw = read_private_bytes(p)
    if raw is None:
        return out, False
    for ln in raw.decode('utf-8', 'surrogateescape').splitlines():
        ln = ln.strip()
        if ln and not ln.startswith('#'):
            out.append(real(ln if os.path.isabs(ln) else os.path.join(common, 'objects', ln)))
    return out, True


def add_alternate_dependents(rows, commons=()):
    """A copy whose objects another discovered repository borrows cannot go.

    Only repositories this scan saw are checked; a borrower elsewhere on the
    disk cannot be seen (documented)."""
    borrowers = {real(c) for c in commons} | {r['git'].get('common') for r in rows if r['git'].get('common')}
    lent, unreadable = {}, []
    for c in sorted(b for b in borrowers if b):
        alts, ok = alternates_of(c)
        if not ok:
            unreadable.append(c)
        for alt in alts:
            lent.setdefault(alt, set()).add(c)
    for r in rows:
        f = r['git']
        if not f.get('ok') or not f.get('standalone'):
            continue
        # A discovered repository whose borrowing cannot be read may borrow
        # from this one: that is uncertainty, so it blocks (its own row
        # already reports its own unreadable alternates).
        if any(not inside(c, r['path']) for c in unreadable):
            f.setdefault('unknown', []).append('object alternates of another repository')
        users = sorted({c for alt, cs in lent.items() if inside(alt, r['path']) for c in cs
                        if not inside(c, r['path'])})
        if users:
            f['dependents'] = sorted(set(f.get('dependents') or []) | set(users))


# --------------------------------------------------------- live verification

def live_remote_check(row, copy_paths):
    """Prove with `git ls-remote` (read-only) that nothing here is missing from a remote.

    Returns (ok, detail). Only branch, tag and pull-request head tips a server
    advertises right now, and that exist locally, count as published. A linked
    worktree must have its HEAD and every commit its own reflog and refs keep
    there (or on a local branch or tag, which outlive the worktree); a
    standalone clone must have every local ref and reflog tip there, and every
    annotated tag object advertised.
    """
    f = row['git']
    path = row['path']
    if f.get('risk_remote'):
        return False, ('its own git settings define programs used to contact a remote ('
                       + ', '.join(f['risk_remote']) + '), so the remote was not contacted')
    if f.get('unsupported'):
        return False, 'unsupported git setup; the remote was not contacted'
    advertised, tried = set(), []
    for name, url in sorted(f.get('remotes', {}).items()):
        if not usable_remote(url, path, copy_paths):
            continue
        r = git(path, 'ls-remote', name, 'refs/heads/*', 'refs/tags/*', 'refs/pull/*/head', timeout=45)
        if not r.ok:
            return False, f'remote {name} could not be reached ({r.err.strip()[:120] or r.rc})'
        tried.append(name)
        for ln in r.out.splitlines():
            sha = ln.split('\t')[0]
            if SHA_RE.match(sha):
                advertised.add(sha)
    if not tried:
        return False, 'no usable remote to verify against'
    present, ok = existing_commits(path, advertised)
    if not ok:
        return False, 'could not compare with the remote'
    head = [f['head']] if f.get('head') else []
    if f.get('worktree'):
        # HEAD itself must be on the remote; the rest of its private history may
        # instead sit on a local branch or tag, which stays after removal.
        n = count_not_reachable(path, head, exclude_shas=present)
        n2 = count_not_reachable(path, list(f.get('private_tips') or []), ['--branches', '--tags'],
                                 exclude_shas=present)
        n = None if n is None or n2 is None else n + n2
    else:
        n = count_not_reachable(path, list(f.get('private_tips') or []) + head, exclude_shas=present)
    if n is None:
        return False, 'could not compare with the remote'
    if n:
        return False, f'{n} commit(s) here are not on the remote right now'
    missing_tags = [t for t in f.get('tag_objects') or [] if t not in advertised]
    if missing_tags:
        return False, f'{len(missing_tags)} annotated tag(s) here are not on the remote right now'
    return True, 'verified against ' + ', '.join(tried)


# -------------------------------------------------------------- disk context

def disk_context(cfg):
    d = {}
    try:
        u = shutil.disk_usage(str(cfg.home))
        d['free_gb'] = round(u.free / 1e9, 1)
        d['total_gb'] = round(u.total / 1e9, 1)
    except OSError:
        pass
    if sys.platform == 'darwin' and shutil.which('tmutil'):
        r = run(['tmutil', 'listlocalsnapshots', '/'], timeout=30)
        if r.ok:
            d['local_snapshots'] = sum(1 for x in r.out.splitlines() if 'com.apple' in x)
    return d


# -------------------------------------------------------------------- report

STYLE = r"""<style>
:root{
 --paper:#f4f6f8;--sheet:#ffffff;--ink:#111a24;--muted:#2b3644;--rule:#cfd6df;--head:#e9edf2;
 --use:#a3141c;--use-bg:#fbe7e8;--recent:#8a4a00;--recent-bg:#fdefd9;--decide:#1d4c96;--decide-bg:#e4ecf9;--done:#16663a;--done-bg:#e0f2e7;
 --body:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--paper:#0f141b;--sheet:#171e28;--ink:#f1f4f8;--muted:#d6dde6;--rule:#334051;--head:#1e2835;--use:#ff8f94;--use-bg:#3d1b1f;--recent:#f6bd6e;--recent-bg:#382a10;--decide:#9ec0ff;--decide-bg:#1a2740;--done:#7fe0a8;--done-bg:#14301f;color-scheme:dark}}
:root[data-theme="dark"]{--paper:#0f141b;--sheet:#171e28;--ink:#f1f4f8;--muted:#d6dde6;--rule:#334051;--head:#1e2835;--use:#ff8f94;--use-bg:#3d1b1f;--recent:#f6bd6e;--recent-bg:#382a10;--decide:#9ec0ff;--decide-bg:#1a2740;--done:#7fe0a8;--done-bg:#14301f;color-scheme:dark}
body{background:var(--paper);color:var(--ink);font-family:var(--body);font-size:16px;line-height:1.55;-webkit-font-smoothing:antialiased;margin:0}
.wrap{max-width:1120px;margin:0 auto;padding-block:28px 64px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));display:flex;flex-direction:column;gap:28px}
header{display:flex;flex-direction:column;gap:6px}
.eyebrow{font-size:14px;font-weight:500;color:var(--muted)}
h1{font-weight:600;font-size:clamp(28px,5vw,38px);line-height:1.15;margin:0;text-wrap:balance}
h2{font-weight:600;font-size:22px;margin:0;text-wrap:balance}
h3{font-weight:600;font-size:18px;margin:0}
p{margin:0;max-width:70ch}
.lead{font-size:18px}
.sheet{background:var(--sheet);border:1px solid var(--rule);border-radius:6px}
.private{border-left:4px solid var(--use);padding:14px 18px;font-size:15px}
.tablewrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th{font-size:14px;font-weight:600;color:var(--ink);text-align:left;background:var(--head);padding:10px 14px;border-bottom:1px solid var(--rule);white-space:nowrap}
td{padding:12px 14px;border-bottom:1px solid var(--rule);vertical-align:top}
tr:last-child td{border-bottom:0}
.num{text-align:right;white-space:nowrap;font-size:15px;font-weight:500}
.tag{display:inline-block;font-size:14px;font-weight:600;padding:3px 10px;border-radius:4px}
.g-inuse{--c:var(--use);--cb:var(--use-bg)} .g-recent{--c:var(--recent);--cb:var(--recent-bg)} .g-decide{--c:var(--decide);--cb:var(--decide-bg)} .g-done{--c:var(--done);--cb:var(--done-bg)}
.tag{color:var(--c);background:var(--cb)}
.summary td:first-child{border-left:4px solid var(--c)}
.summary a{color:inherit;text-decoration:none;font-weight:600}
.summary a:hover,.summary a:focus-visible{text-decoration:underline}
section{display:flex;flex-direction:column;gap:12px;scroll-margin-top:16px}
.sec-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 14px;border-left:4px solid var(--c);padding-left:12px}
.sec-head .meta{font-size:15px;font-weight:500;color:var(--muted)}
.name{font-size:15px;font-weight:600;word-break:break-word}
.sub{color:var(--muted);font-size:14.5px;margin-top:3px;word-break:break-word}
.why{min-width:280px}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.controls label{font-size:15px;font-weight:500;color:var(--ink)}
select,input{font:inherit;font-size:15px;color:var(--ink);background:var(--sheet);border:1px solid var(--rule);border-radius:4px;padding:8px 12px}
select:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--decide);outline-offset:2px}
.notes{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,320px),1fr));gap:20px}
.notes div{display:flex;flex-direction:column;gap:6px}
.foot{font-size:14.5px;color:var(--muted)}
ul{margin:0;padding-left:20px}
li{font-size:15px}
code{font-family:var(--mono);font-size:14px}
</style>"""

GROUP_TEXT = [
    ('inuse', 'In use right now', 'Leave alone. Work is happening in these.'),
    ('recent', 'Active in the last few days', 'Leave for now; recheck in a few days.'),
    ('decide', 'Your call: holds work found nowhere else', 'Keep until someone decides: finish, save or drop.'),
    ('done', 'Finished: nothing local-only found', 'Work is on a remote; checked live again before removal.'),
]

SCRIPT = r"""
const G=__GROUPS__;
const gb=mb=>mb==null?'—':mb>=1024?(mb/1024).toFixed(1)+' GB':mb+' MB';
const ago=d=>d==null?'—':d<1?'today':d<2?'1 day ago':Math.round(d)+' days ago';
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const projSel=document.getElementById('proj'),q=document.getElementById('q');
[...new Set(ROWS.map(r=>r.project))].sort().forEach(p=>projSel.insertAdjacentHTML('beforeend',`<option>${esc(p)}</option>`));
document.getElementById('sum').innerHTML=G.map(g=>{const rs=ROWS.filter(r=>r.g===g.k);return `<tr class="g-${g.k}"><td><a href="#${g.k}">${g.t}</a></td><td class="num">${rs.length}</td><td class="num">${gb(rs.reduce((s,r)=>s+(r.mb||0),0))}</td><td>${g.a}</td></tr>`}).join('');
function draw(){
 const p=projSel.value,s=q.value.trim().toLowerCase();
 document.getElementById('groups').innerHTML=G.map(g=>{
  let rs=ROWS.filter(r=>r.g===g.k&&(!p||r.project===p)&&(!s||(r.name+r.chat+r.why+r.project+r.path).toLowerCase().includes(s))).sort((a,b)=>(b.mb||0)-(a.mb||0));
  const body=rs.length?rs.map(r=>`<tr><td><div class="name">${esc(r.name)}</div><div class="sub">${esc(r.project)} · ${esc(r.tool)}${r.id?` · id <code>${esc(r.id)}</code>`:''}</div></td><td class="why">${esc(r.why)}${r.chat!=null?`<div class="sub">${esc(r.chat||'No chat record found')}</div>`:''}${r.path?`<div class="sub"><code>${esc(r.path)}</code></div>`:''}</td><td class="num">${gb(r.mb)}</td><td class="num">${ago(r.touch)}</td></tr>`).join(''):`<tr><td colspan="4" class="sub">No copies match this filter.</td></tr>`;
  return `<section id="${g.k}" class="g-${g.k}"><div class="sec-head"><h2>${g.t}</h2><span class="meta">${rs.length} · ${gb(rs.reduce((t,r)=>t+(r.mb||0),0))}</span><span class="tag">${g.a}</span></div>
  <div class="sheet tablewrap"><table><thead><tr><th>Copy</th><th>Why it's here · chat that made it</th><th class="num">Size</th><th class="num">Last touched</th></tr></thead><tbody>${body}</tbody></table></div></section>`}).join('');
}
projSel.onchange=draw;q.oninput=draw;draw();
"""


def script_json(obj):
    """JSON safe inside <script>: every '<' (and U+2028/9) escaped."""
    return json.dumps(obj).replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')


def render_html(report, redact=False):
    e = html.escape
    if redact:
        projects = {}
        rows = []
        for n, r in enumerate(report['copies'], 1):
            proj = projects.setdefault(r.get('project', ''), f'project {len(projects) + 1}')
            codes = [b['code'] for b in r.get('blockers', [])]
            if r['group'] == 'done':
                why = 'Finished: nothing local-only found.'
            elif r['group'] == 'inuse':
                why = 'In use.'
            elif r['group'] == 'recent':
                why = 'Active recently.'
            else:
                why = ' '.join(REDACTED_TEXT.get(c, 'Needs review.') for c in dict.fromkeys(codes)) or 'Needs review.'
            rows.append({'g': r['group'], 'id': '', 'name': f'copy {n}', 'project': proj, 'tool': r['kind'],
                         'mb': r.get('size_mb'), 'touch': r.get('last_touch_days'), 'why': why, 'chat': None, 'path': ''})
    else:
        rows = [{'g': r['group'], 'id': r['id'], 'name': r['name'], 'project': r.get('project', ''),
                 'tool': r['kind'], 'mb': r.get('size_mb'), 'touch': r.get('last_touch_days'),
                 'why': r['why'], 'chat': r.get('chat', ''), 'path': r['path']} for r in report['copies']]
    groups = [{'k': k, 't': t, 'a': a} for k, t, a in GROUP_TEXT]
    disk = report['disk']
    left = report['leftovers']
    disk_bits = []
    if 'free_gb' in disk:
        disk_bits.append(f"{disk['free_gb']} GB free of {disk['total_gb']} GB.")
    if 'local_snapshots' in disk:
        disk_bits.append(f"{disk['local_snapshots']} Time Machine local snapshot(s) exist. They often hide tens of "
                         'GB; macOS frees them on its own when space runs low. Clearing them is a separate decision.')
    left_text = f"{left['empty']} empty leftover folder(s) take no real space."
    if left['non_git'] and not redact:
        left_text += f" {len(left['non_git'])} other non-git folder(s) sit beside the agent copies: " + \
            ', '.join(e(Path(p).name) for p in left['non_git'][:5]) + '.'
    if redact:
        warn = f"<li>{len(report['warnings'])} scan warning(s) are hidden in this shareable copy.</li>" \
            if report['warnings'] else ''
        stores = ''.join(f"<li>{e(s['name'])}: {e(s['state'])}</li>" for s in report.get('stores', []))
        banner = ('<div class="sheet private"><b>Shareable copy.</b> Folder names, paths, chat titles, branch and '
                  'file names, ids and warnings were removed from this page and its data.</div>')
    else:
        warn = ''.join(f'<li>{e(w)}</li>' for w in report['warnings'])
        stores = ''.join(f"<li>{e(s['name'])}: {e(s['state'])} <code>{e(s['path'])}</code>"
                         f"{(' · ' + e(s['detail'])) if s.get('detail') else ''}</li>" for s in report.get('stores', []))
        banner = ('<div class="sheet private"><b>Private to this computer.</b> This page lists folder paths, chat '
                  'titles, branch and file names from this machine, including rows hidden by the filters. Do not '
                  'post, upload or send it. For sharing, make the shareable copy (<code>scan --redact</code>), which '
                  'removes them.</div>')
    total = len(rows)
    cinfo = report.get('caches') or {}
    cache_html = ''
    if cinfo.get('allowed'):
        by_kind = {}
        for r in report['copies']:
            for c in r['git'].get('caches') or []:
                k = by_kind.setdefault(c['kind'], [0, 0.0])
                k[0] += 1
                k[1] += c['size_mb']
        items = ''.join(f'<li><code>{e(k)}</code>: {n} folder(s), {mb:.0f} MB</li>' for k, (n, mb) in sorted(by_kind.items()))
        if not redact:
            items += ''.join(f"<li><code>{e(os.path.join(r['path'], c['path']))}</code> · {e(c['kind'])} · "
                             f"{c['size_mb']} MB</li>" for r in report['copies'] for c in r['git'].get('caches') or [])
        cache_html = ('<div class="sheet" style="padding:20px"><h3>Caches treated as rebuildable (--allow-caches)</h3>'
                      f"<p>{cinfo.get('folders', 0)} folder(s), {cinfo.get('size_mb', 0):.0f} MB in all. They do not "
                      'block removal. Every folder is listed below.</p>'
                      f'<ul>{items}</ul></div>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Agent Copy Sort</title>
{STYLE}
</head><body>
<div class="wrap">
<header>
 <div class="eyebrow">Agent working copies · scanned {e(report['generated_at'])}</div>
 <h1>Which agent copies are still being worked on</h1>
 <p class="lead">Every Claude Code, Codex and Cursor task makes its own copy of a project. This scan checked {total} copies: is a program working inside it, is its chat still active, does it hold files or commits that exist nowhere else, and has its work already landed on a remote. {e(report.get('changed_note') or 'No working copy was changed.')}</p>
</header>
{banner}
<div class="sheet tablewrap"><table class="summary">
<thead><tr><th>Group</th><th class="num">Copies</th><th class="num">Space</th><th>What to do</th></tr></thead>
<tbody id="sum"></tbody></table></div>
<div class="controls">
 <label for="proj">Project</label><select id="proj"><option value="">All projects</option></select>
 <label for="q">Search</label><input id="q" type="search" placeholder="name, chat or reason">
</div>
<div id="groups" style="display:flex;flex-direction:column;gap:32px"></div>
<div class="sheet" style="padding:20px"><div class="notes">
 <div><h3>How a copy was judged</h3><p>A copy is <b>in use</b> if a program has its working folder or an open file inside it, or its open chat was active in the last day and it has unsaved files or an open pull request. <b>Active</b> means its chat or its git history moved in the last {report['settings']['recent_days']:g} days. It is <b>finished</b> only when nothing in it exists only here (unsaved, untracked or ignored files, local-only commits, tags or reflog entries) and its work is on a remote. Anything unclear, and any check that failed, goes to <b>your call</b>. Removal checks each copy again, including the live remote.</p></div>
 <div><h3>Not counted here</h3><p>{left_text} {' '.join(disk_bits)}</p></div>
 <div><h3>What these numbers mean</h3><p>Sizes come from a folder-size count. Copies of the same project can share storage, so removing one may free less than its listed size.</p></div>
</div></div>
{cache_html}
<div class="sheet" style="padding:20px"><h3>Where it looked</h3><ul>{stores}</ul></div>
{('<div class="sheet" style="padding:20px"><h3>Scan warnings</h3><ul>' + warn + '</ul></div>') if warn else ''}
<p class="foot">Source: a scan (no working copy is changed) of Claude Code copies (<code>.claude/worktrees</code>), Codex and Cursor copies, every worktree of each repository seen, and clones in the temp folders, matched to Claude and Codex chat records, running programs and pull requests.</p>
</div>
<script>
const ROWS={script_json(rows)};
{SCRIPT.replace('__GROUPS__', script_json(groups))}
</script>
</body></html>
"""


# --------------------------------------------------------------------- scan

def do_scan(cfg):
    owners = load_owners(cfg)
    procev = process_evidence()
    if not procev.ok:
        cfg.warnings.append(f'Running programs could not be fully checked ({procev.state}: {procev.detail}); '
                            'no copy can be finished.')
    if cfg.github_requested and not cfg.github:
        cfg.warnings.append('GitHub CLI (gh) not available; pull requests were not checked.')
    for p in cfg.owner_problems:
        cfg.warnings.append(p)
    owner_cwds = sorted({o['cwd'] for o in owners})
    found, leftovers, commons = discover(cfg, owner_cwds)
    rows = inspect_copies(found, cfg, owners, procev, commons)
    return {
        'tool': TOOL, 'plan_version': PLAN_VERSION, 'plan_id': secrets.token_hex(8),
        'generated_at': time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(cfg.now)),
        'settings': cfg.settings(),
        'stores': cfg.stores,
        'process_check': {'state': procev.state, 'detail': procev.detail, 'accepted_unreadable': procev.accepted,
                          'accept_names': sorted(ACCEPT_UNREADABLE)},
        'counts': {g: sum(1 for r in rows if r['group'] == g) for g in GROUPS},
        'sizes_mb': {g: sum(r.get('size_mb') or 0 for r in rows if r['group'] == g) for g in GROUPS},
        'caches': {'allowed': cfg.allow_caches,
                   'folders': sum(len(r['git'].get('caches') or []) for r in rows),
                   'size_mb': round(sum(c['size_mb'] for r in rows for c in r['git'].get('caches') or []), 1)},
        'disk': disk_context(cfg),
        'leftovers': leftovers,
        'commons': sorted(commons),
        'changed_note': 'No working copy was changed.',
        'warnings': cfg.warnings,
        'copies': rows,
    }


def prune_outputs(out, now, keep_days=OUTPUT_KEEP_DAYS):
    """Delete this tool's own scan plans and pages older than keep_days."""
    pat = re.compile(r'^(scan|report)-\d{8}-\d{6}-[0-9a-f]{6}(-shareable)?\.(json|html)$')
    for p in _safe_iterdir(out):
        try:
            st = os.lstat(p)
            if pat.match(p.name) and stat.S_ISREG(st.st_mode) and now - st.st_mtime > keep_days * DAY:
                os.unlink(p)
        except OSError:
            continue


def make_config(args, settings=None, home=None):
    s = settings or {}
    return Config(home=home if home is not None else s.get('home'), roots=s.get('roots') or (),
                  temp_roots=s.get('temp_roots') or None, codex_home=s.get('codex_home'),
                  claude_dir=s.get('claude_dir'), cursor_home=s.get('cursor_home'),
                  recent_days=s.get('recent_days', 4), github=s.get('github') and not args.no_github,
                  time_limit=args.time_limit, allow_caches=args.allow_caches)


def cmd_scan(args):
    ACCEPT_UNREADABLE.update(args.accept_unreadable)
    cfg = Config(home=args.home, roots=args.root or (), temp_roots=args.temp_root, codex_home=args.codex_home,
                 claude_dir=args.claude_dir, cursor_home=args.cursor_home, recent_days=args.recent_days,
                 github=not args.no_github, time_limit=args.time_limit, allow_caches=args.allow_caches)
    try:
        out = ensure_private_dir(args.out or cfg.state_dir)
    except OutputError as e:
        print(f'Refusing: {e}', file=sys.stderr)
        return 2
    report = do_scan(cfg)
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(cfg.now)) + '-' + report['plan_id'][:6]
    plan = write_private(out / f'scan-{stamp}.json', json.dumps(report, indent=1))
    page = write_private(out / f'report-{stamp}.html', render_html(report))
    share = write_private(out / f'report-{stamp}-shareable.html', render_html(report, redact=True)) if args.redact else None
    prune_outputs(out, cfg.now)
    c, s = report['counts'], report['sizes_mb']
    print(f"{len(report['copies'])} copies: " + ', '.join(f'{g} {c[g]} ({s[g] / 1024:.1f} GB)' for g in GROUPS))
    print(f"empty leftovers {report['leftovers']['empty']}; disk {report['disk']}")
    if cfg.allow_caches:
        print(f"rebuildable caches treated as removable: {report['caches']['folders']} folder(s), "
              f"{report['caches']['size_mb'] / 1024:.1f} GB")
    for w in report['warnings']:
        print('warning:', w)
    print(f'plan: {plan}')
    print(f'page (private, open locally): {page}')
    if share:
        print(f'shareable page (paths, titles, ids removed): {share}')
    return 0


# ------------------------------------------------------------------- remove

def trash_commands(path):
    """Commands that move one folder to the Trash, path passed as an argument, never in script text."""
    if sys.platform == 'darwin':
        cmds = []
        if os.path.exists('/usr/bin/trash'):
            cmds.append(['/usr/bin/trash', path])
        cmds.append(['osascript', '-e', 'on run argv', '-e',
                     'tell application "Finder" to delete (POSIX file (item 1 of argv))', '-e', 'end run', path])
        return cmds
    if shutil.which('gio'):
        return [['gio', 'trash', path]]
    return []


def trash(path, trash_dir=None):
    if trash_dir:  # tests only: a folder standing in for the system Trash
        dest = os.path.join(trash_dir, f'{Path(path).name}-{secrets.token_hex(3)}')
        os.rename(path, dest)
        return True
    for cmd in trash_commands(path):
        if run(cmd, timeout=120).ok and not os.path.exists(path):
            return True
    return False


def _before_target(index, item):
    """Test hook: called right before each target is re-checked."""


def _after_slow_step(step, row):
    """Test hook: called after each slow step (the live remote check)."""


FINGERPRINT_KEYS = ('head', 'branch', 'changed', 'changed_sample', 'ignored', 'ignored_sample', 'hidden',
                    'hidden_sample', 'unpushed', 'private_tips', 'private_objects', 'tag_objects',
                    'private_unpublished', 'dependents', 'unknown', 'unsupported', 'alternates', 'lfs',
                    'submodules', 'risk_inspect', 'risk_remote', 'gitdir', 'common', 'foreign_gitdir')


def fingerprint(f):
    """The git facts that decide removal; any change between checks refuses it."""
    fp = {k: f.get(k) for k in FINGERPRINT_KEYS}
    fp['caches'] = sorted(c['path'] for c in f.get('caches') or [])
    for k in ('gitdir_id', 'common_id'):
        v = f.get(k) or {}
        fp[k] = (v.get('dev'), v.get('ino'))
    return fp


def _same_git_identity(f, old):
    for k in ('gitdir_id', 'common_id'):
        a, b = f.get(k) or {}, old.get(k) or {}
        if (a.get('dev'), a.get('ino')) != (b.get('dev'), b.get('ino')):
            return False
    return True


def recheck_target(entry, plan, cfg):
    """Fresh, per-target evidence. Returns (row, refusal or None, live detail)."""
    path = entry['path']
    if copy_id(path) != entry.get('id'):
        return None, 'its id does not match its path in the plan', ''
    if not os.path.isabs(path) or real(path) != path:
        return None, 'its path is not canonical (a symlink is involved)', ''
    now_id = identity(path)
    if now_id is None:
        return None, 'skipped', ''
    if not same_identity(now_id, entry.get('identity') or {}):
        return None, 'the folder at this path was replaced after the scan', ''
    owners = load_owners(cfg)
    procev = process_evidence()
    plan_paths = [c['path'] for c in plan['copies']]
    cfg.copy_paths = set(plan_paths)
    others = sorted(set(plan_paths) | cfg.main_checkouts)
    row = inspect_one(path, entry.get('kind', ''), cfg, owners, procev, others, with_size=False)
    f = row['git']
    old = entry.get('git') or {}
    for k in ('gitdir', 'common', 'head'):
        if f.get(k) != old.get(k):
            return row, f'its git {k} changed after the scan', ''
    if f.get('ok') and not _same_git_identity(f, old):
        return row, 'its git folder was replaced after the scan', ''
    add_alternate_dependents([row], plan.get('commons') or [])
    if cfg.github and f.get('slug') and f.get('branch'):
        items, complete = pr_for_branch(f['slug'], f['branch'])
        attach_prs(row, items, complete, path)
    else:
        row['pr'] = {'state': 'off', 'items': []}
    finish_row(row, cfg)
    live_ok, live = live_remote_check(row, plan_paths)
    _after_slow_step('live', row)
    if row['group'] != 'done':
        return row, row['why'], live
    if not live_ok:
        return row, f'the live remote check did not prove the work is published: {live}', live
    return row, None, live


def final_recheck(entry, row, plan, cfg):
    """Last check right before deletion, after the slow steps: same folder, same
    git folders, nothing in use or newly active, and git facts unchanged."""
    path = row['path']
    if not same_identity(identity(path), entry.get('identity') or {}):
        return 'the folder at this path was replaced while it was being checked'
    owners = load_owners(cfg)
    procev = process_evidence()
    plan_paths = [c['path'] for c in plan['copies']]
    others = sorted(set(plan_paths) | cfg.main_checkouts)
    row2 = inspect_one(path, row.get('kind', ''), cfg, owners, procev, others, with_size=False)
    add_alternate_dependents([row2], plan.get('commons') or [])
    row2['pr'] = row.get('pr')
    finish_row(row2, cfg)
    if not procev.ok or row2['processes']:
        return 'a program is using it, or running programs could not be checked, right before removal'
    if fingerprint(row2['git']) != fingerprint(row['git']):
        return 'it changed while it was being checked (stop the program writing to it and scan again)'
    if row2['group'] != 'done':
        return row2['why']
    return None


def remove_one(entry, row, plan, cfg, trash_dir):
    """Remove one re-checked copy. Returns item fields."""
    f = row['git']
    path = row['path']
    why = final_recheck(entry, row, plan, cfg)
    if why:
        return dict(result='refused', reason=f'final check: {why}')
    if f.get('worktree'):
        main = Path(f['common'])
        main = str(main.parent) if main.name == '.git' else str(main)
        # Plain `git worktree remove` (never --force) also refuses anything
        # modified or untracked, and a locked worktree.
        r = run(GIT_BASE + ['-C', main, 'worktree', 'remove', path], timeout=120)
        return dict(result='removed' if r.ok else 'failed', how='git worktree remove (branch kept)',
                    **({} if r.ok else {'reason': r.err.strip()[:200]}))
    ok = trash(path, trash_dir)
    return dict(result='moved to Trash' if ok else 'failed', how='Trash')


def cmd_remove(args):
    try:
        raw = Path(os.path.abspath(os.path.expanduser(args.plan)))
        plan_path = Path(real(raw.parent)) / raw.name  # the folder resolved once; the file itself never followed
        out = ensure_private_dir(plan_path.parent, create=False)
        plan = read_private_json(plan_path)
    except OutputError as e:
        print(f'Refusing: {e}', file=sys.stderr)
        return 2
    if not isinstance(plan, dict) or plan.get('tool') != TOOL or plan.get('plan_version') != PLAN_VERSION \
            or not isinstance(plan.get('copies'), list) \
            or not all(isinstance(c, dict) and isinstance(c.get('path'), str) for c in plan['copies']):
        print('Refusing: that file is not a scan plan from this version of the tool; run scan again.', file=sys.stderr)
        return 2
    ids = [c.get('id') for c in plan['copies']]
    if len(ids) != len(set(ids)):
        print('Refusing: the plan has duplicate ids.', file=sys.stderr)
        return 2
    by_id = {c['id']: c for c in plan['copies']}
    missing = [i for i in args.ids if i not in by_id]
    if missing:
        print('Unknown id(s) in this plan: ' + ', '.join(missing), file=sys.stderr)
        return 2
    s = plan.get('settings') or {}
    ACCEPT_UNREADABLE.update(args.accept_unreadable)
    cfg = make_config(args, s)
    if args.allow_caches and not s.get('allow_caches'):
        print('Refusing: --allow-caches was not used for this scan plan; run scan --allow-caches again, '
              'review the new plan, and remove from that one.', file=sys.stderr)
        return 2
    # Main checkouts known from the plan's repositories stay protected.
    for c in plan['copies']:
        common = (c.get('git') or {}).get('common')
        if common and os.path.isdir(common):
            r = git(common, 'worktree', 'list', '--porcelain', '-z', timeout=20)
            if r.ok:
                entries = parse_worktree_list(r.out)
                if entries:
                    cfg.main_checkouts.add(real(entries[0]['worktree']))
    receipt = {'at': time.strftime('%Y-%m-%d %H:%M:%S %Z'), 'plan': str(plan_path), 'plan_id': plan.get('plan_id'),
               'confirmed': bool(args.confirm), 'allow_caches': bool(args.allow_caches),
               'accept_unreadable': sorted(ACCEPT_UNREADABLE), 'items': []}
    rc_all = 0
    for n, i in enumerate(dict.fromkeys(args.ids)):
        entry = by_id[i]
        path = entry['path']
        _before_target(n, entry)
        item = {'id': i, 'path': path}
        if cfg.budget.expired():
            item.update(result='refused', reason='time limit reached before this copy was checked')
            rc_all = 1
        else:
            row, refusal, live = recheck_target(entry, plan, cfg)
            if refusal == 'skipped':
                item.update(result='skipped', reason='Copy no longer exists.')
            elif refusal:
                item.update(result='refused', group=(row or {}).get('group'), reason=refusal)
                rc_all = 1
            elif not args.confirm:
                how = 'git worktree remove' if row['git'].get('worktree') else 'move to Trash'
                item.update(result='would remove', how=how, live=live)
            else:
                try:
                    item.update(remove_one(entry, row, plan, cfg, args.trash_dir), live=live)
                except (OutputError, OSError) as e:
                    item.update(result='refused', reason=f'nothing removed: {e}')
                if item['result'] not in ('removed', 'moved to Trash', 'skipped'):
                    rc_all = 1
        receipt['items'].append(item)
        extra = ''.join(f" ({item[k]})" for k in ('reason',) if item.get(k))
        print(f"{item['result']}: {path}{extra}")
    try:
        rp = write_private(out / f"remove-receipt-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.json",
                           json.dumps(receipt, indent=1))
        print(f'receipt: {rp}')
    except (OutputError, OSError) as e:
        print(f'warning: receipt not written ({e})', file=sys.stderr)
    return rc_all


# ---------------------------------------------------------------------- cli

def finite_nonneg(v):
    try:
        x = float(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f'{v!r} is not a number')
    if not math.isfinite(x) or x < 0:
        raise argparse.ArgumentTypeError(f'{v!r} must be a finite number, zero or more')
    return x


def finite_positive(v):
    x = finite_nonneg(v)
    if x <= 0:
        raise argparse.ArgumentTypeError(f'{v!r} must be more than zero')
    return x


QUIESCENT_HELP = ('Removal assumes nothing writes into a copy while it runs; stop the agent or program working '
                  'there first. A write that lands between the last check and the deletion can be lost.')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    scan_help = ('sort copies; writes a private plan and page. No working copy is changed (it only cleans up '
                 'its own plan and page files older than 14 days)')
    p = sub.add_parser('scan', help=scan_help, description=scan_help)
    p.add_argument('--out', help='private output folder (default: ~/.local/state/agent-copy-sort)')
    p.add_argument('--root', action='append', help='extra folder holding repositories (repeatable); repositories '
                                                   'found there are originals and never removable')
    p.add_argument('--temp-root', action='append', help='temp folder to search for clones (repeatable)')
    p.add_argument('--recent-days', default=4, type=finite_nonneg)
    p.add_argument('--time-limit', default=300, type=finite_positive,
                   help='best-effort time budget in seconds (bounds every command; a single large file '
                        'copy or hash can overrun it)')
    p.add_argument('--no-github', action='store_true', help='skip pull-request lookups')
    p.add_argument('--redact', action='store_true', help='also write a shareable page without paths, titles or ids')
    p.add_argument('--allow-caches', action='store_true',
                   help='opt-in: treat the named rebuildable cache folders as safe to remove (see SKILL.md)')
    p.add_argument('--accept-unreadable', action='append', default=[], metavar='NAME',
                   help='Linux: a program name whose hidden details you accept as not working in these copies')
    p.add_argument('--home', help=argparse.SUPPRESS)
    p.add_argument('--codex-home', help=argparse.SUPPRESS)
    p.add_argument('--claude-dir', help=argparse.SUPPRESS)
    p.add_argument('--cursor-home', help=argparse.SUPPRESS)
    p = sub.add_parser('remove', help='remove approved copies from one scan plan. ' + QUIESCENT_HELP,
                       description=QUIESCENT_HELP)
    p.add_argument('--plan', required=True, help='the scan plan file (scan-*.json) the ids come from')
    p.add_argument('--ids', nargs='+', required=True, help='copy ids from that plan')
    p.add_argument('--confirm', action='store_true', help='actually act; without it, a dry run')
    p.add_argument('--time-limit', default=600, type=finite_positive, help='best-effort time budget in seconds')
    p.add_argument('--no-github', action='store_true')
    p.add_argument('--allow-caches', action='store_true',
                   help='opt-in: rebuildable caches do not block removal; the plan must be scanned with it too')
    p.add_argument('--accept-unreadable', action='append', default=[], metavar='NAME',
                   help='Linux: a program name whose hidden details you accept as not working in these copies')
    p.add_argument('--trash-dir', help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if shutil.which('git') is None:
        print('git is required.', file=sys.stderr)
        return 2
    return {'scan': cmd_scan, 'remove': cmd_remove}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
