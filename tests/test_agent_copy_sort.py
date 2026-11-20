"""Tests for scripts/agent_copy_sort.py.

Every repository, remote, owner record and output folder lives in a
throwaway temp folder with its own HOME; the real home directory is never
scanned and nothing outside the temp folder is removed. Destructive paths
(worktree removal, clone-to-Trash via a stand-in Trash folder) run on Linux
and macOS alike.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SCRIPT = Path(__file__).parents[1] / 'scripts/agent_copy_sort.py'
_spec = importlib.util.spec_from_file_location('agent_copy_sort', SCRIPT)
acs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acs)

GIT_ENV = {**{k: v for k, v in os.environ.items() if not k.startswith('GIT_')},
           'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}


def git(cwd, *args, check=True):
    return subprocess.run(['git', '-c', 'init.defaultBranch=main', '-c', 'protocol.file.allow=always', *args],  # ubs:ignore
                          cwd=cwd, env=GIT_ENV, check=check, capture_output=True, text=True)


def head(path):
    return git(path, 'rev-parse', 'HEAD').stdout.strip()


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = base = Path(os.path.realpath(self.temp.name))
        self.home = base / 'home'
        self.tmproot = base / 'tmp'
        self.out = base / 'out'
        self.trash = base / 'trash'
        self.trash.mkdir()
        self.tmproot.mkdir(parents=True)
        self.remote = base / 'remote.git'
        git(base, 'init', '--bare', str(self.remote))
        self.repo = self.home / 'Desktop' / 'proj'
        self.repo.mkdir(parents=True)
        git(self.repo, 'init')
        (self.repo / 'a.txt').write_text('a')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-m', 'first')
        git(self.repo, 'remote', 'add', 'origin', str(self.remote))
        git(self.repo, 'push', '-u', 'origin', 'main')
        git(self.repo, 'remote', 'set-head', 'origin', 'main')
        # The script strips GIT_* variables, so isolation comes from HOME.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith('GIT_') and k not in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'XDG_STATE_HOME')}
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / '.config'))
        safe = {**self.env, 'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'}
        patcher = mock.patch.object(acs, 'SAFE_ENV', safe)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Linux hides some of this user's own programs (non-dumpable, e.g. CI runner
        # helpers). The tool refuses unless they are named; name only those that are
        # unreadable on this machine right now, never the test's own programs.
        ev = acs.process_evidence()
        self.accept = sorted({x.rsplit('(', 1)[0] for x in ev.unreadable})
        accept_patch = mock.patch.object(acs, 'ACCEPT_UNREADABLE', set(acs.DEFAULT_ACCEPT_UNREADABLE) | set(self.accept))
        accept_patch.start()
        self.addCleanup(accept_patch.stop)

    # ---------------------------------------------------------------- helpers
    def worktree(self, name, detach=None, age=True):
        path = self.repo / '.claude' / 'worktrees' / name
        if detach:
            git(self.repo, 'worktree', 'add', '--detach', str(path), detach)
        else:
            git(self.repo, 'worktree', 'add', '-b', name, str(path))
        if age:
            self.age(path)
        return path

    def age(self, path, days=10):
        stamp = time.time() - days * 86400
        gitdir = subprocess.run(['git', '-C', str(path), 'rev-parse', '--absolute-git-dir'],  # ubs:ignore -- argv list
                                capture_output=True, text=True, check=True, env=GIT_ENV).stdout.strip()
        for p in [path, Path(gitdir) / 'index', Path(gitdir) / 'HEAD', Path(gitdir) / 'logs' / 'HEAD']:
            if p.exists():
                os.utime(p, (stamp, stamp))

    def commit(self, path, name='b.txt', text='b', msg='local'):
        (path / name).write_text(text)
        git(path, 'add', name)
        git(path, 'commit', '-m', msg)

    def owner(self, cwd, days_ago, archived=False, title='Test chat', sid='s1'):
        d = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b'
        d.mkdir(parents=True, exist_ok=True)
        rec = {'sessionId': sid, 'cwd': str(cwd), 'title': title, 'isArchived': archived,
               'lastActivityAt': int((time.time() - days_ago * 86400) * 1000)}
        (d / f'local_{sid}.json').write_text(json.dumps(rec))

    def clone(self, parent, name='proj', age=True):
        parent.mkdir(parents=True, exist_ok=True)
        git(parent, 'clone', str(self.remote), name)
        path = parent / name
        git(path, 'remote', 'set-head', 'origin', 'main')
        if age:
            self.age(path)
        return path

    def cli(self, *args, out=None, extra=()):
        cmd = [sys.executable, str(SCRIPT), *args]
        if args[0] in ('scan', 'remove'):
            for name in self.accept:
                cmd += ['--accept-unreadable', name]
        if args[0] == 'scan':
            cmd += ['--home', str(self.home), '--temp-root', str(self.tmproot), '--out', str(out or self.out),
                    '--no-github', '--time-limit', '120', *extra]
        elif args[0] == 'remove':
            cmd += ['--no-github', '--trash-dir', str(self.trash), *extra]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=self.env)  # ubs:ignore - argument list, no shell, test-built args

    def latest_plan(self, out=None):
        plans = sorted((out or self.out).glob('scan-*.json'), key=os.path.getmtime)
        return plans[-1]

    def scan(self, *extra, out=None):
        res = self.cli('scan', out=out, extra=extra)
        self.assertEqual(res.returncode, 0, res.stderr + res.stdout)
        plan = self.latest_plan(out)
        report = json.loads(plan.read_text())
        self.plan = plan
        self.report = report
        return {Path(r['path']).name: r for r in report['copies']}

    def remove(self, *ids, confirm=True, plan=None, extra=()):
        args = ['remove', '--plan', str(plan or self.plan), '--ids', *ids]
        if confirm:
            args.append('--confirm')
        return self.cli(*args, extra=extra)

    def remove_inprocess(self, *ids, plan=None, extra=()):
        argv = ['remove', '--plan', str(plan or self.plan), '--ids', *ids, '--confirm', '--no-github',
                '--trash-dir', str(self.trash), *extra]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = acs.main(argv)
        return rc, buf.getvalue()

    def codes(self, row):
        return {b['code'] for b in row.get('blockers', [])}

    def run_tool(self, *args, env=None):
        cmd = [sys.executable, str(SCRIPT), *args]
        return subprocess.run(cmd, capture_output=True, text=True, env=env or self.env, timeout=300)  # ubs:ignore -- argv list

class SortingTest(Base):
    def test_dirty_copy_is_your_call(self):
        wt = self.worktree('dirty')
        (wt / 'new.txt').write_text('unsaved')
        self.age(wt)
        row = self.scan()['dirty']
        self.assertEqual(row['group'], 'decide', row['why'])
        self.assertIn('unsaved', row['why'])

    def test_clean_copy_on_remote_is_finished(self):
        self.worktree('clean')
        row = self.scan()['clean']
        self.assertEqual(row['group'], 'done', row['why'])

    def test_unpushed_commit_is_your_call(self):
        wt = self.worktree('local-only')
        self.commit(wt)
        self.age(wt)
        row = self.scan()['local-only']
        self.assertEqual(row['group'], 'decide', row['why'])
        self.assertIn('not on any remote', row['why'])

    def test_process_inside_is_in_use(self):
        wt = self.worktree('busy')
        proc = subprocess.Popen(['sleep', '60'], cwd=wt)  # ubs:ignore - fixed argument list, no shell
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        row = self.scan()['busy']
        self.assertEqual(row['group'], 'inuse', row['why'])

    def test_open_file_from_outside_is_in_use(self):
        wt = self.worktree('held')
        proc = subprocess.Popen([sys.executable, '-c', 'import sys,time; f=open(sys.argv[1]); time.sleep(60)',  # ubs:ignore - fixed argument list, no shell
                                 str(wt / 'a.txt')], cwd=self.base)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        time.sleep(0.5)  # let the interpreter open the file; not a correctness barrier
        row = self.scan()['held']
        self.assertEqual(row['group'], 'inuse', row['why'])

    def test_recent_owner_is_recent(self):
        wt = self.worktree('chatty')
        self.owner(wt, days_ago=2)
        row = self.scan()['chatty']
        self.assertEqual(row['group'], 'recent', row['why'])
        self.assertIn('Test chat', row['chat'])

    def test_archived_old_owner_does_not_block_finished(self):
        wt = self.worktree('old-chat')
        self.owner(wt, days_ago=2, archived=True)
        self.assertEqual(self.scan()['old-chat']['group'], 'done')

    def test_copy_folder_without_git_data_is_your_call(self):
        broken = self.repo / '.claude' / 'worktrees' / 'broken'
        (broken / '.claude').mkdir(parents=True)
        row = self.scan()['broken']
        self.assertEqual(row['group'], 'decide', row['why'])
        self.assertIn('missing or damaged', row['why'])

    def test_empty_git_marker_file_is_not_a_copy(self):
        cache = self.tmproot / 'tool-cache' / 'sdists'
        cache.mkdir(parents=True)
        (cache / '.git').write_text('')
        self.assertNotIn('sdists', self.scan())

    def test_ignored_file_is_your_call_and_named(self):
        (self.repo / '.git' / 'info' / 'exclude').write_text('valuable.db\n')
        wt = self.worktree('ignored')
        (wt / 'valuable.db').write_bytes(b'precious')
        self.age(wt)
        row = self.scan()['ignored']
        self.assertEqual(row['group'], 'decide', row['why'])
        self.assertIn('ignored', self.codes(row))
        self.assertIn('valuable.db', row['why'])
        res = self.remove(row['id'])
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertEqual((wt / 'valuable.db').read_bytes(), b'precious')

    def test_hidden_index_flags_are_your_call(self):
        for flag in ('--assume-unchanged', '--skip-worktree'):
            name = 'hidden' + flag.replace('-', '')
            wt = self.worktree(name)
            (wt / 'a.txt').write_text('changed but hidden')
            git(wt, 'update-index', flag, 'a.txt')
            self.assertEqual(git(wt, 'status', '--porcelain').stdout, '')  # status cannot see it
            self.age(wt)
        rows = self.scan()
        for name in ('hiddenassumeunchanged', 'hiddenskipworktree'):
            self.assertEqual(rows[name]['group'], 'decide', rows[name]['why'])
            self.assertIn('hidden', self.codes(rows[name]))
        res = self.remove(rows['hiddenskipworktree']['id'])
        self.assertEqual(res.returncode, 1)
        self.assertEqual((self.repo / '.claude/worktrees/hiddenskipworktree/a.txt').read_text(), 'changed but hidden')

    def test_submodule_is_your_call(self):
        git(self.repo, 'update-index', '--add', '--cacheinfo', f'160000,{head(self.repo)},sub')
        git(self.repo, 'commit', '-m', 'gitlink')
        git(self.repo, 'push', 'origin', 'main')
        self.worktree('withsub')
        row = self.scan()['withsub']
        self.assertEqual(row['group'], 'decide', row['why'])
        self.assertIn('submodules', self.codes(row))

    def test_clone_with_tag_only_commit_is_your_call(self):
        clone = self.clone(self.tmproot / 'job', age=False)
        self.commit(clone)
        git(clone, 'tag', 'keep-me')
        git(clone, 'reset', '--hard', 'origin/main')
        git(clone, 'reflog', 'expire', '--expire=now', '--all')
        self.age(clone)
        row = self.scan()['proj']
        self.assertEqual(row['group'], 'decide', row['why'])
        self.assertIn('private-history', self.codes(row))

    def test_clone_reflog_only_commit_is_your_call(self):
        clone = self.clone(self.tmproot / 'job', age=False)
        self.commit(clone)
        git(clone, 'reset', '--hard', 'origin/main')
        self.age(clone)
        row = self.scan()['proj']
        self.assertEqual(row['group'], 'decide', row['why'])
        self.assertIn('private-history', self.codes(row))


class DiscoveryTest(Base):
    def test_temp_clone_found_and_clean_clone_finished_and_trashed(self):
        clone = self.clone(self.tmproot / 'job')
        row = self.scan()['proj']
        self.assertEqual(row['kind'], 'Temp folder')
        self.assertEqual(row['group'], 'done', row['why'])
        res = self.remove(row['id'])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse(clone.exists())
        self.assertEqual(len(list(self.trash.iterdir())), 1)

    def test_claude_named_temp_clone_found_but_claude_scratch_skipped(self):
        self.clone(self.tmproot / 'claude-job', 'jobclone')
        self.clone(self.tmproot / 'claude-1234', 'scratchclone')
        rows = self.scan()
        self.assertIn('jobclone', rows)
        self.assertNotIn('scratchclone', rows)

    def test_symlink_to_main_checkout_is_not_followed(self):
        wtdir = self.repo / '.claude' / 'worktrees'
        wtdir.mkdir(parents=True)
        os.symlink(self.repo, wtdir / 'evil')
        os.symlink(self.repo, self.tmproot / 'evil2')
        rows = self.scan()
        self.assertNotIn('evil', rows)
        self.assertNotIn('evil2', rows)
        self.assertFalse([r for r in rows.values() if r['path'] == str(self.repo)])
        self.assertTrue(self.report['leftovers']['skipped_links'])

    def test_repo_with_external_worktree_is_never_removable(self):
        seed = self.clone(self.tmproot / 'job', 'seed', age=False)
        elsewhere = self.base / 'elsewhere' / 'wt'
        git(seed, 'worktree', 'add', '--detach', str(elsewhere))
        self.commit(elsewhere)
        self.age(elsewhere)
        self.age(seed)
        rows = self.scan()
        self.assertIn('dependents', self.codes(rows['seed']), rows['seed']['why'])
        self.assertEqual(rows['seed']['group'], 'decide')
        self.assertEqual(rows['wt']['group'], 'decide', rows['wt']['why'])
        res = self.remove(rows['seed']['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue((seed / '.git').is_dir())

    def test_repo_under_explicit_root_seeds_worktree_elsewhere(self):
        root = self.base / 'projects'
        other = root / 'r'
        other.mkdir(parents=True)
        git(other, 'clone', str(self.remote), '.')
        far = self.base / 'far' / 'w1'
        git(other, 'worktree', 'add', '-b', 'w1', str(far))
        self.age(far)
        rows = self.scan('--root', str(root))
        self.assertIn('w1', rows)
        self.assertNotIn('r', rows)  # the main checkout is never a copy

    def test_worktree_path_with_newline_is_parsed(self):
        odd = self.base / 'far' / 'odd\nname'
        git(self.repo, 'worktree', 'add', '-b', 'oddname', str(odd))
        self.age(odd)
        rows = self.scan()
        self.assertIn('odd\nname', rows)
        self.assertEqual(rows['odd\nname']['group'], 'done', rows['odd\nname']['why'])

    def test_case_variant_owner_path_matches_on_case_insensitive_volume(self):
        probe = self.base / 'CaseProbe'
        probe.mkdir()
        if not (self.base / 'caseprobe').exists():
            self.skipTest('case-sensitive filesystem')
        wt = self.worktree('casey')
        self.owner(str(wt).replace('/casey', '/CASEY'), days_ago=2)
        self.assertEqual(self.scan()['casey']['group'], 'recent')

    def test_cursor_worktrees_are_found(self):
        cur = self.home / '.cursor' / 'worktrees' / 'task-1'
        cur.mkdir(parents=True)
        far = cur / 'proj'
        git(self.repo, 'worktree', 'add', '-b', 'cursor-task', str(far))
        self.age(far)
        rows = self.scan()
        self.assertEqual(rows['proj']['kind'], 'Cursor')
        stores = {s['name'] for s in self.report['stores']}
        self.assertIn('Cursor worktrees', stores)
        self.assertIn('Codex thread records', stores)


class OwnerRecordsTest(Base):
    def make_codex(self, rows, name='state_5.sqlite'):
        codex = self.home / '.codex'
        codex.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(codex / name)
        con.execute('pragma journal_mode=wal')
        con.execute('create table threads (id text, cwd text, title text, archived int, updated_at)')
        con.executemany('insert into threads values (?,?,?,?,?)', rows)
        con.commit()
        return con  # kept open so the WAL file stays in place

    def test_codex_wal_records_and_malformed_rows(self):
        wt = self.worktree('codexed')
        con = self.make_codex([('t1', str(wt), 'Codex task\nmore', 0, int(time.time() - 86400 * 2)),
                               ('t2', str(wt), 'bad', 0, 'not-a-number')])
        self.addCleanup(con.close)
        row = self.scan()['codexed']
        self.assertEqual(row['group'], 'recent', row['why'])
        self.assertIn('Codex: Codex task', row['chat'])
        # A malformed record is uncertainty, not a warning to skip past.
        self.assertIn('owners-incomplete', self.codes(row))
        self.assertTrue(any('Codex thread record could not be used' in w for w in self.report['warnings']))

    def test_unreadable_codex_db_blocks_finished(self):
        self.worktree('clean2')
        codex = self.home / '.codex'
        codex.mkdir(parents=True)
        (codex / 'state_5.sqlite').write_bytes(b'this is not a database' * 100)
        row = self.scan()['clean2']
        self.assertEqual(row['group'], 'decide', row['why'])
        self.assertIn('owners-incomplete', self.codes(row))

    def test_malformed_desktop_record_is_uncertainty(self):
        wt = self.worktree('desk')
        self.owner(wt, days_ago=2)
        d = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b'
        (d / 'local_bad.json').write_text('[1, 2]')
        (d / 'local_bad2.json').write_text(json.dumps({'cwd': str(wt), 'lastActivityAt': 'yesterday'}))
        row = self.scan()['desk']
        self.assertEqual(row['group'], 'recent', row['why'])
        self.assertIn('owners-incomplete', self.codes(row))
        self.assertTrue(any('Claude desktop session record could not be used' in w for w in self.report['warnings']))

    def test_only_owner_record_malformed_blocks_finished(self):
        wt = self.worktree('lonely')
        d = self.home / 'Library/Application Support/Claude/claude-code-sessions/a/b'
        d.mkdir(parents=True)
        (d / 'local_bad.json').write_text(json.dumps({'cwd': str(wt), 'lastActivityAt': 1, 'isArchived': 'no'}))
        row = self.scan()['lonely']
        self.assertEqual(row['group'], 'decide', row['why'])
        self.assertIn('owners-incomplete', self.codes(row))
        res = self.remove(row['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(wt.exists())

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root can read any folder')
    def test_unlistable_owner_folder_blocks_finished(self):
        wt = self.worktree('unlisted')
        d = self.home / 'Library/Application Support/Claude/claude-code-sessions/a'
        d.mkdir(parents=True)
        os.chmod(d, 0)
        self.addCleanup(os.chmod, d, 0o700)
        row = self.scan()['unlisted']
        self.assertEqual(row['group'], 'decide', row['why'])
        self.assertIn('owners-incomplete', self.codes(row))
        self.assertTrue(wt.exists())

    def test_transcript_without_cwd_in_first_64k_is_read_further(self):
        wt = self.worktree('longcli')
        t = self.home / '.claude' / 'projects' / 'p' / 'long.jsonl'
        t.parent.mkdir(parents=True)
        t.write_text(json.dumps({'type': 'summary', 'text': 'x' * 70000}) + '\n'
                     + json.dumps({'type': 'user', 'cwd': str(wt)}) + '\n')
        stamp = time.time() - 2 * 86400
        os.utime(t, (stamp, stamp))
        nocwd = t.with_name('nocwd.jsonl')
        nocwd.write_text(json.dumps({'type': 'summary'}) + '\n')
        row = self.scan()['longcli']
        self.assertEqual(row['group'], 'recent', row['why'])
        self.assertIn('owners-incomplete', self.codes(row))

    def test_busy_codex_database_stops_at_the_time_limit(self):
        codex = self.home / '.codex'
        codex.mkdir(parents=True)
        db = codex / 'state_5.sqlite'
        con = sqlite3.connect(db, isolation_level=None)
        con.execute('create table threads (id text, cwd text, title text, archived int, updated_at)')
        con.execute('begin exclusive')
        self.addCleanup(con.close)
        cfg = acs.Config(home=self.home, github=False, time_limit=1)
        start = time.monotonic()
        acs.load_owners(cfg)
        self.assertLess(time.monotonic() - start, 5)
        self.assertTrue(any('Codex thread records' in p for p in cfg.owner_problems), cfg.owner_problems)

    def test_claude_cli_transcript_is_an_owner(self):
        wt = self.worktree('cli')
        t = self.home / '.claude' / 'projects' / 'p' / 'abc.jsonl'
        t.parent.mkdir(parents=True)
        t.write_text(json.dumps({'type': 'user', 'cwd': str(wt)}) + '\n')
        stamp = time.time() - 2 * 86400
        os.utime(t, (stamp, stamp))
        row = self.scan()['cli']
        self.assertEqual(row['group'], 'recent', row['why'])

    def test_codex_home_environment_is_respected(self):
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(self.base / 'cx')}):
            cfg = acs.Config(home=None, github=False)
        self.assertEqual(str(cfg.codex_home), str(self.base / 'cx'))


class RemovalSafetyTest(Base):
    def test_remove_refuses_copy_that_became_dirty(self):
        wt = self.worktree('changes-later')
        row = self.scan()['changes-later']
        self.assertEqual(row['group'], 'done', row['why'])
        (wt / 'late.txt').write_text('new work')
        res = self.remove(row['id'])
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn('refused', res.stdout)
        self.assertTrue((wt / 'late.txt').exists())

    def test_remove_finished_worktree_keeps_branch(self):
        wt = self.worktree('finished')
        row = self.scan()['finished']
        dry = self.remove(row['id'], confirm=False)
        self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertTrue(wt.exists(), 'a run without --confirm must not remove anything')
        res = self.remove(row['id'])
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse(wt.exists())
        self.assertIn('finished', git(self.repo, 'branch', '--list', 'finished').stdout)
        receipts = list(self.out.glob('remove-receipt-*.json'))
        self.assertTrue(receipts)
        self.assertEqual(stat.S_IMODE(os.stat(receipts[0]).st_mode), 0o600)

    def test_process_check_failure_blocks_removal(self):
        self.worktree('pc')
        row = self.scan()['pc']
        self.assertEqual(row['group'], 'done')
        for state in ('failed', 'unavailable', 'incomplete'):
            with mock.patch.object(acs, 'process_evidence', lambda *a, **k: acs.ProcEvidence(state, {}, 'mock')):
                rc, out = self.remove_inprocess(row['id'])
            self.assertEqual(rc, 1, out)
            self.assertIn('could not be checked', out)
        self.assertTrue((self.repo / '.claude/worktrees/pc').exists())

    def test_lsof_failure_is_not_an_empty_result(self):
        with mock.patch.object(acs.shutil, 'which', return_value='/usr/sbin/lsof'), \
                mock.patch.object(acs, 'run', return_value=acs.Res(-1, '', 'timed out')):
            ev = acs.process_evidence(platform_name='darwin')
        self.assertEqual(ev.state, 'failed')
        with mock.patch.object(acs.shutil, 'which', return_value='/usr/sbin/lsof'), \
                mock.patch.object(acs, 'run', return_value=acs.Res(1, '', 'lsof: error')):
            self.assertEqual(acs.process_evidence(platform_name='darwin').state, 'failed')

    def test_unreadable_same_user_proc_entry_is_incomplete(self):
        proc = self.base / 'proc'
        (proc / '4242' / 'fd').mkdir(parents=True)
        (proc / '4242' / 'comm').write_text('agent\n')
        with mock.patch.object(acs, '_readlink', side_effect=PermissionError('denied')):
            self.assertEqual(acs.process_evidence(str(proc), 'linux').state, 'incomplete')
        with mock.patch.object(acs, '_readlink', side_effect=FileNotFoundError('exited')):
            self.assertEqual(acs.process_evidence(str(proc), 'linux').state, 'ok')

    def test_accepted_unreadable_name_is_explicit(self):
        proc = self.base / 'proc'
        for pid, comm in (('4242', 'helper'), ('4243', 'agent')):
            (proc / pid / 'fd').mkdir(parents=True)
            (proc / pid / 'comm').write_text(comm + '\n')
        with mock.patch.object(acs, '_readlink', side_effect=PermissionError('denied')), \
                mock.patch.object(acs, 'ACCEPT_UNREADABLE', {'helper'}):
            ev = acs.process_evidence(str(proc), 'linux')
        self.assertEqual(ev.state, 'incomplete')
        self.assertIn('agent(4243)', ev.detail)
        self.assertNotIn('helper(4242)', ev.detail)
        self.assertEqual(ev.accepted, ['helper(4242)'])
        with mock.patch.object(acs, '_readlink', side_effect=PermissionError('denied')), \
                mock.patch.object(acs, 'ACCEPT_UNREADABLE', {'helper', 'agent'}):
            self.assertEqual(acs.process_evidence(str(proc), 'linux').state, 'ok')

    def test_each_target_is_rechecked_right_before_removal(self):
        first = self.worktree('first')
        second = self.worktree('second')
        rows = self.scan()
        procs = []

        def barrier(index, entry):
            if index == 1:  # after the first copy is handled, before the second is re-checked
                procs.append(subprocess.Popen(['sleep', '60'], cwd=second))  # ubs:ignore -- fixed argument list, shell=False

        with mock.patch.object(acs, '_before_target', barrier):
            rc, out = self.remove_inprocess(rows['first']['id'], rows['second']['id'])
        for p in procs:
            p.kill()
            p.wait()
        self.assertEqual(rc, 1, out)
        self.assertFalse(first.exists(), out)
        self.assertTrue(second.exists(), out)
        self.assertIn('running program', out)

    def test_replaced_folder_is_refused(self):
        wt = self.worktree('swap')
        row = self.scan()['swap']
        self.assertEqual(row['group'], 'done')
        os.rename(wt, str(wt) + '-moved')
        git(wt.parent, 'clone', str(self.remote), 'swap')
        res = self.remove(row['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn('replaced', res.stdout)
        self.assertTrue((wt / '.git').is_dir())

    def test_moved_detached_head_is_refused(self):
        self.commit(self.repo, 'c.txt', 'c', 'second')
        git(self.repo, 'push', 'origin', 'main')
        wt = self.worktree('det', detach='HEAD~1')
        row = self.scan()['det']
        self.assertEqual(row['group'], 'done', row['why'])
        git(wt, 'checkout', '--detach', 'main')
        res = self.remove(row['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn('head changed', res.stdout, res.stderr)
        self.assertTrue(wt.exists())

    def test_stale_cached_remote_ref_is_refused_by_live_check(self):
        wt = self.worktree('gone')
        self.commit(wt)
        git(wt, 'push', '-u', 'origin', 'gone')
        self.age(wt)
        row = self.scan()['gone']
        self.assertEqual(row['group'], 'done', row['why'])
        git(self.remote, 'update-ref', '-d', 'refs/heads/gone')  # the server forgets it; the cache does not
        res = self.remove(row['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn('not on the remote right now', res.stdout)
        self.assertTrue(wt.exists())

    def test_edited_plan_cannot_redirect_or_promote(self):
        clean = self.worktree('okay')
        dirty = self.worktree('notokay')
        (dirty / 'work.txt').write_text('real work')
        self.age(dirty)
        rows = self.scan()
        plan = json.loads(self.plan.read_text())
        for c in plan['copies']:
            if c['path'] == str(clean):
                c['path'] = str(dirty)  # approved id now points at another copy
            if Path(c['path']).name == 'notokay':
                c['group'] = 'done'  # a reviewer "moved it to finished" by editing JSON
        os.chmod(self.plan, 0o600)
        self.plan.write_text(json.dumps(plan))
        res = self.remove(rows['okay']['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn('id does not match', res.stdout)
        res = self.remove(rows['notokay']['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue((dirty / 'work.txt').exists())
        self.assertTrue(clean.exists())

    def test_second_scan_keeps_first_plan(self):
        wt = self.worktree('twice')
        row = self.scan()['twice']
        first = self.plan
        time.sleep(1.1)
        self.scan()
        self.assertNotEqual(first, self.plan)
        self.assertEqual(json.loads(first.read_text())['plan_id'], row and json.loads(first.read_text())['plan_id'])
        res = self.remove(row['id'], plan=first)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse(wt.exists())
        self.assertEqual(stat.S_IMODE(os.stat(first).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o700)

    def test_unsafe_output_folders_are_refused(self):
        real_dir = self.base / 'realout'
        real_dir.mkdir()
        os.symlink(real_dir, self.base / 'linkout')
        res = self.cli('scan', out=self.base / 'linkout')
        self.assertEqual(res.returncode, 2, res.stderr)
        shared = self.base / 'shared'
        shared.mkdir()
        os.chmod(shared, 0o777)
        res = self.cli('scan', out=shared)
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertFalse(list(shared.iterdir()))

    def test_git_query_failure_is_unknown_not_zero(self):
        wt = self.worktree('flaky')
        real_git = acs.git

        def failing(path, *args, **kw):
            if args and args[0] == 'rev-list':
                return acs.Res(128, '', 'fatal: bad object')
            return real_git(path, *args, **kw)

        cfg = acs.Config(home=self.home, temp_roots=[self.tmproot], github=False)
        with mock.patch.object(acs, 'git', failing):
            f = acs.git_facts(str(wt), cfg)
        self.assertIn('commits not on a remote', f['unknown'])
        row = {'path': str(wt), 'git': f, 'owners': [], 'processes': [], 'pr': {'state': 'off', 'items': []},
               'proc_state': 'ok'}
        group, why = acs.classify(row, cfg)
        self.assertEqual(group, 'decide', why)
        self.assertIn('failed', why)

    def test_fsmonitor_helper_is_not_run(self):
        wt = self.worktree('fsm')
        marker = self.base / 'fsmonitor-ran'
        hook = self.base / 'hook.sh'
        hook.write_text(f'#!/bin/sh\ntouch {marker}\nexit 1\n')
        os.chmod(hook, 0o755)
        git(self.repo, 'config', 'core.fsmonitor', str(hook))
        self.age(wt)
        self.scan()
        self.assertFalse(marker.exists(), 'scan ran the repository fsmonitor helper')
        git(wt, 'status', check=False)  # control: plain git status does run it
        self.assertTrue(marker.exists(), 'control failed: this git never runs fsmonitor helpers')

    def test_local_filter_config_is_not_inspected(self):
        wt = self.worktree('filt')
        git(self.repo, 'config', 'filter.x.clean', 'cat')
        row = self.scan()['filt']
        self.assertEqual(row['group'], 'decide', row['why'])
        self.assertIn('helpers', self.codes(row))
        self.assertTrue(wt.exists())

    def test_time_budget_stops_work(self):
        clock = [0.0]
        b = acs.Budget(10, clock=lambda: clock[0])
        marker = self.base / 'should-not-exist'
        clock[0] = 11
        res = acs.run(['touch', str(marker)], budget=b)
        self.assertEqual(res.rc, -2)
        self.assertFalse(marker.exists())
        self.assertEqual(b.cap(30), -1)
        wt = self.worktree('late')
        cfg = acs.Config(home=self.home, temp_roots=[self.tmproot], github=False, time_limit=0)
        rows = acs.inspect_copies({str(wt): 'Claude Code'}, cfg, [], acs.ProcEvidence('ok'))
        self.assertEqual(rows[0]['group'], 'decide')


class PullRequestEvidenceTest(Base):
    def row(self, prs, complete=True, head='b' * 40):
        f = {'ok': True, 'unknown': [], 'branch': 'feat', 'head': head, 'remotes': {'origin': 'git@github.com:o/r.git'},
             'slug': 'o/r', 'has_remote': True, 'unpushed': 2, 'in_default': False, 'branch_on_remote': True,
             'private_unpublished': 0, 'standalone': False, 'worktree': True, 'changed': 0, 'ignored': 0,
             'hidden': 0, 'last_touch': time.time() - 30 * 86400, 'risk_inspect': [], 'risk_remote': []}
        r = {'path': str(self.base / 'nowhere'), 'git': f, 'owners': [], 'processes': [], 'proc_state': 'ok'}
        acs.attach_prs(r, prs, complete, r['path'])
        return r

    def cfg(self):
        c = acs.Config(home=self.home, temp_roots=[self.tmproot], github=False)
        c.github = True
        return c

    def test_merged_pr_never_overrides_later_commits(self):
        r = self.row([{'headRefName': 'feat', 'state': 'MERGED', 'number': 7, 'title': 't',
                       'headRefOid': 'a' * 40, 'isCrossRepository': False}])
        group, why = acs.classify(r, self.cfg())
        self.assertEqual(group, 'decide', why)
        self.assertIn('merged earlier', why)

    def test_merged_pr_with_exact_head_is_proof(self):
        r = self.row([{'headRefName': 'feat', 'state': 'MERGED', 'number': 7, 'title': 't',
                       'headRefOid': 'b' * 40, 'isCrossRepository': False}])
        self.assertEqual(acs.classify(r, self.cfg())[0], 'done')

    def test_fork_pr_with_same_branch_name_is_not_proof(self):
        r = self.row([{'headRefName': 'feat', 'state': 'MERGED', 'number': 9, 'title': 't',
                       'headRefOid': 'b' * 40, 'isCrossRepository': True}])
        self.assertEqual(acs.classify(r, self.cfg())[0], 'decide')

    def test_truncated_pr_list_is_incomplete(self):
        r = self.row([], complete=False)
        r['git']['unpushed'] = 0
        group, why = acs.classify(r, self.cfg())
        self.assertEqual(r['pr']['state'], 'incomplete')
        self.assertEqual(group, 'decide', why)
        self.assertIn('pr-unknown', self.codes(r))


class ReportPrivacyTest(Base):
    def test_page_is_private_offline_and_script_safe(self):
        wt = self.worktree('page')
        self.owner(wt, days_ago=9, title='x <!--<script> y </script>')
        self.scan()
        page = next(self.out.glob('report-*.html')).read_text()
        self.assertNotIn('http://', page)
        self.assertNotIn('https://', page)
        data = page.split('const ROWS=', 1)[1].split(';\n', 1)[0]
        self.assertNotIn('<', data)
        self.assertIn('\\u003c!--\\u003cscript>', data)
        self.assertIn('Private to this computer', page)
        self.assertEqual(stat.S_IMODE(os.stat(next(self.out.glob('report-*.html'))).st_mode), 0o600)

    def test_shareable_page_contains_no_private_strings(self):
        wt = self.worktree('secretname-xyz')
        (wt / 'secretfile-abc.txt').write_text('x')
        self.age(wt)
        self.owner(wt, days_ago=9, title='SECRET-TITLE-123')
        rows = self.scan('--redact')
        page = next(self.out.glob('report-*-shareable.html')).read_bytes()
        for seed in ('secretname-xyz', 'secretfile-abc', 'SECRET-TITLE-123', rows['secretname-xyz']['id'],
                     str(self.base)):
            self.assertNotIn(seed.encode(), page, seed)
        self.assertIn(b'Shareable copy', page)

    def test_trash_fallback_passes_path_as_argument(self):
        path = '/tmp/odd "name\\ é'
        with mock.patch.object(acs.sys, 'platform', 'darwin'), mock.patch.object(acs.os.path, 'exists', return_value=False):
            cmds = acs.trash_commands(path)
        osa = cmds[-1]
        self.assertEqual(osa[0], 'osascript')
        self.assertEqual(osa[-1], path)
        self.assertFalse(any(path in part for part in osa[:-1]))


TAG = 'Signature: 8a477f597d28d172789f06886806bc55\n# a cache\n'


class CacheAllowlistTest(Base):
    def setUp(self):
        super().setUp()
        (self.repo / '.git' / 'info' / 'exclude').write_text(
            '__pycache__/\n.pytest_cache/\n.ruff_cache/\n.mypy_cache/\n.next/\nnode_modules/\n.venv/\nvenv/\n'
            'build-cache/\nvaluable.db\n.env\n')

    def cached_worktree(self, name, cache, files, age=True):
        wt = self.worktree(name, age=False)
        for rel, text in files.items():
            p = wt / cache / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        if age:
            self.age(wt)
        return wt

    KINDS = {
        'k-pycache': ('pkg/__pycache__', {'m.cpython-311.pyc': 'x'}),
        'k-pytest': ('.pytest_cache', {'v/cache/lastfailed': '{}'}),
        'k-ruff': ('.ruff_cache', {'0.6.9/1': 'x'}),
        'k-mypy': ('.mypy_cache', {'3.11/m.json': '{}'}),
        'k-next': ('.next', {'build/a.js': 'x'}),
        'k-node': ('node_modules', {'left-pad/index.js': 'x'}),
        'k-venv': ('.venv', {'pyvenv.cfg': 'home = /usr', 'lib/site.py': 'x'}),
        'k-venv2': ('venv', {'pyvenv.cfg': 'home = /usr'}),
        'k-tag': ('build-cache', {'CACHEDIR.TAG': TAG, 'blob': 'x'}),
    }

    def test_each_allowlisted_kind_is_removable_only_with_the_flag(self):
        for name, (cache, files) in self.KINDS.items():
            self.cached_worktree(name, cache, files)
        plain = self.scan()
        plain_plan = self.plan
        for name in self.KINDS:
            self.assertEqual(plain[name]['group'], 'decide', name)
            self.assertIn('ignored', self.codes(plain[name]))
        time.sleep(1.1)
        rows = self.scan('--allow-caches')
        for name in self.KINDS:
            self.assertEqual(rows[name]['group'], 'done', (name, rows[name]['why']))
            self.assertIn('Rebuildable caches', rows[name]['why'])
        self.assertTrue(self.report['settings']['allow_caches'])
        self.assertEqual(self.report['caches']['folders'], len(self.KINDS))
        page = self.plan.with_name(self.plan.name.replace('scan-', 'report-').replace('.json', '.html')).read_text()
        self.assertIn('Caches treated as rebuildable', page)
        self.assertIn('node_modules', page)
        # A plan scanned without the flag cannot be used with it.
        res = self.remove(plain['k-node']['id'], plan=plain_plan, extra=('--allow-caches',))
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn('scan --allow-caches again', res.stderr)
        self.assertTrue((self.repo / '.claude/worktrees/k-node').exists())
        # Without the flag at removal, caches still block.
        res = self.remove(rows['k-node']['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        res = self.remove(rows['k-node']['id'], extra=('--allow-caches',))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse((self.repo / '.claude/worktrees/k-node').exists())
        receipt = json.loads(sorted(self.out.glob('remove-receipt-*.json'), key=os.path.getmtime)[-1].read_text())
        self.assertTrue(receipt['allow_caches'])

    def test_non_cache_files_beside_or_inside_caches_still_block(self):
        self.cached_worktree('in-env', 'node_modules', {'pkg/index.js': 'x', '.env': 'TOKEN=1'})
        self.cached_worktree('in-db', '.pytest_cache', {'valuable.db': 'precious'})
        wt = self.cached_worktree('beside', 'node_modules', {'pkg/index.js': 'x'}, age=False)
        (wt / 'valuable.db').write_text('precious')
        (wt / '.env').write_text('TOKEN=1')
        self.age(wt)
        self.cached_worktree('fake-venv', '.venv', {'lib/data.txt': 'x'})
        rows = self.scan('--allow-caches')
        for name in ('in-env', 'in-db', 'beside', 'fake-venv'):
            self.assertEqual(rows[name]['group'], 'decide', (name, rows[name]['why']))
            self.assertIn('ignored', self.codes(rows[name]))
        self.assertIn('.env', rows['in-env']['why'])
        self.assertIn('valuable.db', rows['beside']['why'])
        res = self.remove(rows['in-db']['id'], extra=('--allow-caches',))
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertEqual((self.repo / '.claude/worktrees/in-db/.pytest_cache/valuable.db').read_text(), 'precious')

    def test_symlink_out_of_the_copy_and_tracked_cache_block(self):
        wt = self.cached_worktree('linky', 'node_modules', {'pkg/index.js': 'x'}, age=False)
        os.symlink(self.base, wt / 'node_modules' / 'escape')
        self.age(wt)
        inner = self.cached_worktree('inner-link', 'node_modules', {'pkg/index.js': 'x'}, age=False)
        os.symlink(inner / 'a.txt', inner / 'node_modules' / 'ok-link')
        self.age(inner)
        tracked = self.worktree('tracked', age=False)
        (tracked / 'node_modules').mkdir()
        (tracked / 'node_modules' / 'vendored.js').write_text('x')
        git(tracked, 'add', '-f', 'node_modules/vendored.js')
        git(tracked, 'commit', '-m', 'vendor')
        git(tracked, 'push', '-u', 'origin', 'tracked')
        (tracked / 'node_modules' / 'extra.js').write_text('y')
        self.age(tracked)
        venv = self.cached_worktree('venv-link', '.venv', {'pyvenv.cfg': 'home = /usr'}, age=False)
        (venv / '.venv' / 'bin').mkdir()
        os.symlink(sys.executable, venv / '.venv' / 'bin' / 'python3')
        self.age(venv)
        badvenv = self.cached_worktree('venv-badlink', '.venv', {'pyvenv.cfg': 'home = /usr'}, age=False)
        (badvenv / '.venv' / 'lib').mkdir()
        os.symlink(self.base, badvenv / '.venv' / 'lib' / 'python3')
        self.age(badvenv)
        rows = self.scan('--allow-caches')
        self.assertEqual(rows['venv-link']['group'], 'done', rows['venv-link']['why'])
        self.assertEqual(rows['venv-badlink']['group'], 'decide', rows['venv-badlink']['why'])
        self.assertEqual(rows['linky']['group'], 'decide', rows['linky']['why'])
        self.assertIn('points outside', rows['linky']['why'])
        self.assertEqual(rows['inner-link']['group'], 'done', rows['inner-link']['why'])
        self.assertEqual(rows['tracked']['group'], 'decide', rows['tracked']['why'])
        self.assertIn('tracked', rows['tracked']['why'])

class CacheBypassTest(Base):

    def setUp(self):
        super().setUp()
        (self.repo / '.git' / 'info' / 'exclude').write_text('node_modules/\n.mypy_cache/\n')

    def cache_wt(self, name, cache, files):
        wt = self.worktree(name, age=False)
        for rel, text in files.items():
            p = wt / cache / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return wt

    def test_env_symlink_to_inner_file_blocks(self):
        wt = self.cache_wt('envlink', 'node_modules', {'credentials': 'TOKEN=secret', 'pkg/i.js': 'x'})
        os.symlink('credentials', wt / 'node_modules' / '.env')
        self.age(wt)
        row = self.scan('--allow-caches')['envlink']
        self.assertEqual(row['group'], 'decide', row['why'])
        self.assertIn('.env', row['why'])

    def test_unproven_cache_db_blocks(self):
        wt = self.cache_wt('mypydb', '.mypy_cache', {'3.11/cache.db': 'whatever', '3.11/m.json': '{}'})
        self.age(wt)
        row = self.scan('--allow-caches')['mypydb']
        self.assertEqual(row['group'], 'decide', row['why'])

    def test_venv_interpreter_link_must_be_an_executable_file(self):
        (self.repo / '.git' / 'info' / 'exclude').write_text('.venv/\n')
        wt = self.cache_wt('venvdir', '.venv', {'pyvenv.cfg': 'home = /usr'})
        (wt / '.venv' / 'bin').mkdir()
        os.symlink(self.base, wt / '.venv' / 'bin' / 'python3')
        self.age(wt)
        row = self.scan('--allow-caches')['venvdir']
        self.assertEqual(row['group'], 'decide', row['why'])

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root can read any folder')
    def test_unreadable_cache_blocks(self):
        wt = self.cache_wt('lockedcache', 'node_modules', {'pkg/i.js': 'x', 'sealed/x.js': 'x'})
        os.chmod(wt / 'node_modules' / 'sealed', 0)
        self.addCleanup(os.chmod, wt / 'node_modules' / 'sealed', 0o700)
        self.age(wt)
        row = self.scan('--allow-caches')['lockedcache']
        self.assertEqual(row['group'], 'decide', row['why'])

    def test_tracked_path_containment_ignores_letter_case(self):
        wt = self.cache_wt('casey', 'node_modules', {'pkg/i.js': 'x'})
        blocking, caches = acs.split_caches(str(wt), ['node_modules/'], ['Node_Modules/vendored.js'], acs.Budget(30))
        self.assertEqual(caches, [])
        self.assertTrue(blocking and 'tracked' in blocking[0], blocking)


class RaceTest(Base):

    def test_file_written_after_live_check_refuses_removal(self):
        wt = self.worktree('racy')
        (self.repo / '.git' / 'info' / 'exclude').write_text('late.env\n')
        row = self.scan()['racy']
        self.assertEqual(row['group'], 'done', row['why'])

        def write_late(step, _row):
            if step == 'live':
                (wt / 'late.env').write_text('TOKEN=new')
        with mock.patch.object(acs, '_after_slow_step', write_late):
            rc, out = self.remove_inprocess(row['id'])
        self.assertEqual(rc, 1, out)
        self.assertIn('final check', out)
        self.assertEqual((wt / 'late.env').read_text(), 'TOKEN=new')

class RefsAndDependenciesTest(Base):

    def test_annotated_tag_only_commit_blocks(self):
        clone = self.clone(self.tmproot / 'jt', 'tagged', age=False)
        self.commit(clone, 't.txt', 'kept only by a tag', 'tagged work')
        git(clone, 'tag', '-a', 'v-local', '-m', 'local only')
        git(clone, 'reset', '--hard', 'origin/main')
        git(clone, 'reflog', 'expire', '--expire=now', '--all')
        self.age(clone)
        row = self.scan()['tagged']
        self.assertEqual(row['group'], 'decide', row['why'])
        self.assertIn('private-history', self.codes(row))

    def test_alternates_lender_and_borrower_are_protected(self):
        lender = self.clone(self.tmproot / 'ja', 'lender')
        (self.tmproot / 'jb').mkdir()
        git(self.tmproot / 'jb', 'clone', '--shared', str(lender), 'borrower')
        self.age(self.tmproot / 'jb' / 'borrower')
        rows = self.scan()
        self.assertIn('dependents', self.codes(rows['lender']), rows['lender']['why'])
        self.assertIn('unsupported', self.codes(rows['borrower']), rows['borrower']['why'])
        res = self.remove(rows['lender']['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue((lender / '.git').is_dir())

    def _borrower_with_alternates(self, mode):
        lender = self.clone(self.tmproot / 'ja', 'lender')
        (self.tmproot / 'jb').mkdir()
        git(self.tmproot / 'jb', 'clone', '--shared', str(lender), 'borrower')
        borrower = self.tmproot / 'jb' / 'borrower'
        self.age(borrower)
        alt = borrower / '.git' / 'objects' / 'info' / 'alternates'
        if mode == 'symlink':
            target = self.base / 'alternates-target'
            target.write_text(alt.read_text())
            alt.unlink()
            alt.symlink_to(target)
        else:
            alt.chmod(0)
            self.addCleanup(alt.chmod, 0o644)
        rows = self.scan()
        self.assertIn('unknown', self.codes(rows['lender']), rows['lender']['why'])
        self.assertNotEqual(rows['lender']['group'], 'done')
        res = self.remove(rows['lender']['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue((lender / '.git').is_dir())

    def test_lender_blocks_when_discovered_borrower_alternates_is_a_symlink(self):
        self._borrower_with_alternates('symlink')

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root reads mode-000 files')
    def test_lender_blocks_when_discovered_borrower_alternates_is_unreadable(self):
        self._borrower_with_alternates('unreadable')

    def test_worktree_commit_kept_only_by_orig_head_blocks(self):
        wt = self.worktree('orighead', detach='HEAD', age=False)
        base = head(wt)
        self.commit(wt, 'o.txt', 'kept only by ORIG_HEAD', 'O')
        lost = head(wt)
        git(wt, 'reset', '--hard', base)
        gitdir = Path(git(wt, 'rev-parse', '--absolute-git-dir').stdout.strip())
        self.assertEqual((gitdir / 'ORIG_HEAD').read_text().strip(), lost)
        (gitdir / 'logs' / 'HEAD').write_text('')  # its reflog has expired
        self.age(wt)
        row = self.scan()['orighead']
        self.assertIn('private-history', self.codes(row), row['why'])
        res = self.remove(row['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(wt.exists())

    def test_clone_commit_kept_only_by_orig_head_blocks(self):
        clone = self.clone(self.tmproot / 'jo', 'origclone', age=False)
        self.commit(clone, 'o.txt', 'kept only by ORIG_HEAD', 'O')
        git(clone, 'reset', '--hard', 'origin/main')
        for p in (clone / '.git' / 'logs').rglob('*'):
            if p.is_file():
                p.write_text('')
        self.age(clone)
        row = self.scan()['origclone']
        self.assertIn('private-history', self.codes(row), row['why'])
        res = self.remove(row['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(clone.exists())

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root reads mode-000 folders')
    def test_unreadable_reflog_folder_blocks(self):
        clone = self.clone(self.tmproot / 'jr', 'unread', age=False)
        self.commit(clone, 'r.txt', 'kept only by a branch reflog', 'R')
        git(clone, 'reset', '--hard', 'origin/main')
        (clone / '.git' / 'logs' / 'HEAD').write_text('')
        (clone / '.git' / 'ORIG_HEAD').unlink()
        self.age(clone)
        hidden = clone / '.git' / 'logs' / 'refs' / 'heads'
        hidden.chmod(0)
        self.addCleanup(hidden.chmod, 0o755)
        row = self.scan()['unread']
        self.assertIn('unknown', self.codes(row), row['why'])
        res = self.remove(row['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(clone.exists())

    def test_explicit_root_source_in_temp_folder_is_protected(self):
        src = self.clone(self.tmproot / 'src', 'proj')
        rows = self.scan('--root', str(self.tmproot / 'src'))
        self.assertIn('protected-main', self.codes(rows['proj']), rows['proj']['why'])
        res = self.remove(rows['proj']['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(src.exists())

    def test_private_reflog_covered_only_by_stale_remote_ref_is_checked_live(self):
        wt = self.worktree('reflogged', detach='HEAD', age=False)
        base = head(wt)
        self.commit(wt, 'r.txt', 'only in the reflog', 'R')
        git(wt, 'push', 'origin', 'HEAD:refs/heads/side')
        git(wt, 'fetch', 'origin')
        git(wt, 'checkout', '--detach', base)
        git(self.remote, 'update-ref', '-d', 'refs/heads/side')
        self.age(wt)
        row = self.scan()['reflogged']
        self.assertEqual(row['group'], 'done', row['why'])  # cached evidence says fine
        res = self.remove(row['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn('not on the remote', res.stdout)
        self.assertTrue(wt.exists())


class GitHelperTest(Base):

    def fake_bin(self, name, marker):
        bindir = self.base / 'bin'
        bindir.mkdir(exist_ok=True)
        p = bindir / name
        p.write_text(f'#!/bin/sh\necho ran >> "{marker}"\nexit 1\n')
        p.chmod(0o755)
        env = dict(self.env, PATH=f"{bindir}{os.pathsep}{self.env['PATH']}")
        return env

    def test_unknown_transport_helper_is_never_run(self):
        marker = self.base / 'helper-ran'
        env = self.fake_bin('git-remote-acstest', marker)
        wt = self.worktree('helper')
        git(self.repo, 'remote', 'add', 'aaa', 'acstest::anything')
        row = self.scan()['helper']
        res = subprocess.run([sys.executable, str(SCRIPT), 'remove', '--plan', str(self.plan), '--ids', row['id'],  # ubs:ignore
                              '--confirm', '--no-github', '--trash-dir', str(self.trash),
                              *sum((['--accept-unreadable', n] for n in self.accept), [])],
                             capture_output=True, text=True, env=env, timeout=300)  # ubs:ignore -- argv list
        self.assertFalse(marker.exists(), res.stdout + res.stderr)
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(wt.exists())

    def test_partial_clone_is_refused_without_fetching(self):
        git(self.remote, 'config', 'uploadpack.allowfilter', 'true')
        (self.tmproot / 'jp').mkdir()
        git(self.tmproot / 'jp', 'clone', '--filter=blob:none', '--no-checkout', f'file://{self.remote}', 'partial')
        clone = self.tmproot / 'jp' / 'partial'
        marker = self.base / 'ssh-ran'
        env = self.fake_bin('ssh', marker)
        git(clone, 'remote', 'set-url', 'origin', 'ssh://example.invalid/x.git')
        self.age(clone)
        res = subprocess.run([sys.executable, str(SCRIPT), 'scan', '--home', str(self.home), '--temp-root',  # ubs:ignore
                              str(self.tmproot), '--out', str(self.out), '--no-github', '--time-limit', '120',
                              *sum((['--accept-unreadable', n] for n in self.accept), [])],
                             capture_output=True, text=True, env=env, timeout=300)  # ubs:ignore -- argv list
        self.assertEqual(res.returncode, 0, res.stderr)
        rows = {Path(r['path']).name: r for r in json.loads(self.latest_plan().read_text())['copies']}
        self.assertIn('unsupported', self.codes(rows['partial']), rows['partial']['why'])
        self.assertFalse(marker.exists())


class SafeIOTest(Base):

    def test_symlinked_plan_is_refused(self):
        wt = self.worktree('plan-link')
        row = self.scan()['plan-link']
        link = self.out / 'linked-plan.json'
        os.symlink(self.plan, link)
        res = self.remove(row['id'], plan=link)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertTrue(wt.exists())

    def test_replaced_git_folder_at_same_path_is_refused(self):
        clone = self.clone(self.tmproot / 'jg', 'regit')
        row = self.scan()['regit']
        os.rename(clone / '.git', self.base / 'old-git')
        subprocess.run(['cp', '-R', str(self.base / 'old-git'), str(clone / '.git')], check=True)  # ubs:ignore -- argv list, no shell
        res = self.remove(row['id'])
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn('replaced', res.stdout)
        self.assertTrue(clone.exists())

class NumberValidationTest(Base):

    def test_non_finite_and_negative_numbers_are_rejected(self):
        self.worktree('nums')
        self.scan()
        for flag, value in (('--time-limit', 'nan'), ('--time-limit', 'inf'), ('--time-limit', '-1'),
                            ('--time-limit', '0')):
            res = self.cli('remove', '--plan', str(self.plan), '--ids', 'x', flag, value)
            self.assertEqual(res.returncode, 2, (flag, value, res.stderr))
        for flag, value in (('--time-limit', 'nan'), ('--recent-days', '-2'), ('--recent-days', 'inf')):
            res = self.cli('scan', extra=(flag, value))
            self.assertEqual(res.returncode, 2, (flag, value, res.stderr))


class WordingTest(Base):
    """Help and page say what really happens."""

    def test_help_states_quiescence_and_best_effort_budget(self):
        res = self.run_tool('remove', '--help')
        text = ' '.join(res.stdout.split())
        self.assertIn('nothing writes into a copy', text)
        res = self.run_tool('scan', '--help')
        text = ' '.join(res.stdout.split())
        self.assertIn('No working copy is changed', text)
        self.assertIn('best-effort', text)
        self.assertNotIn('real overall budget', SCRIPT.read_text())


if __name__ == '__main__':
    unittest.main()
