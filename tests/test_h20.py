"""H20：会话存档格式与读写层。"""

from __future__ import annotations

import errno
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import session_store as ss
from core.agentscope_actors import build_state
from core.model_state import ModelTriple
from core.tui import HistoryRecord
from core.usage import UsageAccumulator, UsageRecord


def _rec(kind, text, tone="plain", actual=None, seq=None, ts=None):
    r = HistoryRecord(kind, text, tone, actual=actual)
    r.seq = seq
    r.ts = ts
    return r


PATCH = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


def _usage():
    return [
        UsageRecord("c1", 1.0, "openai", "gpt-x", "high", 100, 20, None, None,
                    "provider", 1.0, 2.0, 0.00014),
        UsageRecord("c2", 2.0, "openai", "gpt-x", None, None, 7, None, None,
                    "estimate", None, None, None),
    ]


def _tree(root: Path) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns, p.read_bytes())
    return out


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="h20-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ws = self.tmp / "proj"
        self.ws.mkdir()
        (self.ws / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.store = ss.SessionStore(self.tmp / "sessions")

    def session(self, sid="s-1", title="修登录 bug"):
        history = [
            _rec("user", "帮我看看 tests/test_auth.py", seq=1, ts=10.5),
            _rec("assistant", "看了，问题在 auth.py\n第二行", "plain",
                 actual=("openai", "gpt-x", None), seq=2, ts=11.25),
            _rec("diff", PATCH, seq=3, ts=12.0),
            _rec("system", "运行结束", "muted", seq=4, ts=13.0),
        ]
        return ss.SessionData(
            session_id=sid,
            title=title,
            workspace=str(self.ws),
            agent_state=build_state(self.ws),
            history=history,
            model_override=ModelTriple("openai", "gpt-x", "high"),
            usage=_usage(),
        )

    def runs(self):
        return [
            ss.ToolRun(f"call-{i}", "shell",
                       ss.summarize_args({"cmd": "pytest -q"}, self.ws),
                       state=state, started_at="t0",
                       ended_at=None if state in ("pending", "running") else "t1",
                       result_summary=None if state == "pending" else "ok")
            for i, state in enumerate(ss.TOOL_STATES)
        ]


class FormatTest(_Base):
    def test_root_for_external_project(self) -> None:
        self.assertEqual(ss.sessions_root_for(self.ws), self.ws / ".drbug" / "sessions")

    def test_manifest_fields_and_version(self) -> None:
        self.store.save(self.session(), self.runs())
        doc = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(doc["format_version"], 1)
        self.assertEqual(doc["kind"], "manifest")
        (entry,) = doc["payload"]["sessions"]
        for key in ("id", "title", "workspace", "created_at", "updated_at",
                    "message_count", "last_summary"):
            self.assertIn(key, entry)
        self.assertEqual(entry["id"], "s-1")
        self.assertEqual(entry["title"], "修登录 bug")
        self.assertEqual(entry["workspace"], str(self.ws))
        self.assertEqual(entry["message_count"], 4)
        self.assertEqual(entry["last_summary"], "运行结束")
        session_doc = json.loads(
            (self.store.root / "s-1" / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(session_doc["format_version"], 1)
        self.assertEqual(session_doc["sha256"], ss.payload_sha256(session_doc["payload"]))

    def test_history_roundtrip_all_fields(self) -> None:
        original = self.session()
        self.store.save(original, self.runs())
        loaded = self.store.load("s-1").session
        self.assertEqual(len(loaded.history), len(original.history))
        for got, want in zip(loaded.history, original.history):
            self.assertIsInstance(got, HistoryRecord)
            self.assertEqual(tuple(got), tuple(want))
            self.assertEqual(got.actual, want.actual)
            self.assertEqual(got.seq, want.seq)
            self.assertEqual(got.ts, want.ts)
        self.assertEqual(loaded.history[1].actual, ("openai", "gpt-x", None))
        self.assertEqual(loaded.history[2][0], "diff")
        self.assertEqual(loaded.history[2][1], PATCH)

    def test_agent_state_override_and_meta_roundtrip(self) -> None:
        original = self.session()
        self.store.save(original)
        loaded = self.store.load("s-1").session
        self.assertEqual(loaded.agent_state, original.agent_state)
        self.assertEqual(loaded.model_override, ModelTriple("openai", "gpt-x", "high"))
        self.assertEqual(loaded.title, "修登录 bug")
        self.assertEqual(loaded.workspace, str(self.ws))
        self.assertTrue(loaded.created_at and loaded.updated_at)

    def test_no_override_stays_none(self) -> None:
        s = self.session()
        s.model_override = None
        self.store.save(s)
        self.assertIsNone(self.store.load("s-1").session.model_override)

    def test_usage_snapshot_keeps_unknown_as_none(self) -> None:
        self.store.save(self.session())
        usage = self.store.load("s-1").session.usage
        self.assertEqual(usage, _usage())
        est = usage[1]
        self.assertEqual(est.source, "estimate")
        self.assertIsNone(est.input_tokens)
        self.assertIsNone(est.cost)
        acc = UsageAccumulator()
        for r in usage:
            acc.add(r)
        totals = acc.totals()
        self.assertEqual(totals["input_tokens"], {"sum": 100, "missing": 1})
        self.assertEqual(totals["cost"]["unknown"], 1)
        self.assertEqual(totals["estimated_calls"], 1)
        raw = json.loads((self.store.root / "s-1" / "session.json").read_text("utf-8"))
        self.assertIsNone(raw["payload"]["usage"][1]["input_tokens"])

    def test_thinking_text_not_archived(self) -> None:
        self.store.save(self.session())
        raw = json.loads((self.store.root / "s-1" / "session.json").read_text("utf-8"))
        rec_keys = set(raw["payload"]["history"][0])
        self.assertEqual(rec_keys, {"kind", "text", "tone", "actual", "seq", "ts"})
        self.assertNotIn("thinking", json.dumps(raw["payload"]["history"]))

    def test_save_updates_existing_entry(self) -> None:
        s = self.session()
        self.store.save(s)
        created = s.created_at
        s.history.append(_rec("user", "再看一下", seq=5, ts=20.0))
        self.store.save(s)
        listed = self.store.list_sessions().entries
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].message_count, 5)
        self.assertEqual(listed[0].created_at, created)

    def test_invalid_session_id_rejected(self) -> None:
        for bad in ("../x", "", "a/b", ".hidden"):
            with self.assertRaises(ValueError):
                self.store.session_dir(bad)


class AtomicWriteTest(_Base):
    def test_interrupted_write_keeps_old_file(self) -> None:
        self.store.save(self.session(title="旧标题"))
        path = self.store.root / "s-1" / "session.json"
        before = path.read_bytes()
        real_write = os.write
        calls = {"n": 0}

        def flaky(fd, data):
            calls["n"] += 1
            # 只写一半就“断电”
            real_write(fd, bytes(data[: max(1, len(data) // 2)]))
            raise KeyboardInterrupt

        with mock.patch("core.session_store.os.write", side_effect=flaky):
            with self.assertRaises(KeyboardInterrupt):
                self.store.save(self.session(title="新标题"))
        self.assertTrue(calls["n"] >= 1)
        # tool_runs 先写，被打断的是它；session.json 原样
        self.assertEqual(path.read_bytes(), before)
        leftovers = [p for p in self.store.root.rglob("*.tmp")]
        self.assertEqual(leftovers, [])
        self.assertEqual(self.store.load("s-1").session.title, "旧标题")

    def test_disk_full_raises_not_swallowed(self) -> None:
        self.store.save(self.session(title="旧标题"))
        manifest_before = self.store.manifest_path.read_bytes()
        session_before = (self.store.root / "s-1" / "session.json").read_bytes()

        def full(fd, data):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch("core.session_store.os.write", side_effect=full):
            with self.assertRaises(OSError) as ctx:
                self.store.save(self.session(title="新标题"))
        self.assertEqual(ctx.exception.errno, errno.ENOSPC)
        self.assertEqual(self.store.manifest_path.read_bytes(), manifest_before)
        self.assertEqual(
            (self.store.root / "s-1" / "session.json").read_bytes(), session_before)
        self.assertEqual(list(self.store.root.rglob("*.tmp")), [])

    def test_replace_failure_leaves_no_tmp(self) -> None:
        target = self.tmp / "f.json"
        target.write_text("old", encoding="utf-8")
        with mock.patch("core.session_store.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                ss.atomic_write_text(target, "new")
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])


class ChecksumAndCorruptionTest(_Base):
    def _three(self):
        for sid in ("s-1", "s-2", "s-3"):
            self.store.save(self.session(sid=sid, title=f"会话 {sid}"))

    def test_one_byte_flip_fails_checksum_and_is_skipped(self) -> None:
        self._three()
        path = self.store.root / "s-2" / "session.json"
        text = path.read_text(encoding="utf-8")
        flipped = text.replace("会话 s-2", "会话 s-9", 1)
        self.assertNotEqual(flipped, text)
        path.write_text(flipped, encoding="utf-8")
        result = self.store.list_sessions()
        self.assertEqual(sorted(e.session_id for e in result.entries), ["s-1", "s-3"])
        self.assertEqual(result.corrupt, 1)
        self.assertEqual(result.manifest_status, "ok")
        with self.assertRaises(ss.CorruptArchiveError):
            self.store.load("s-2")

    def test_garbage_session_json_skipped(self) -> None:
        self._three()
        (self.store.root / "s-1" / "session.json").write_text("{半截", encoding="utf-8")
        (self.store.root / "s-3" / "session.json").unlink()
        result = self.store.list_sessions()
        self.assertEqual([e.session_id for e in result.entries], ["s-2"])
        self.assertEqual(result.corrupt, 2)

    def test_corrupt_manifest_still_lists(self) -> None:
        self._three()
        self.store.manifest_path.write_text("not json", encoding="utf-8")
        result = self.store.list_sessions()
        self.assertEqual(result.manifest_status, "corrupt")
        self.assertEqual(sorted(e.session_id for e in result.entries),
                         ["s-1", "s-2", "s-3"])

    def test_tampered_manifest_checksum_falls_back(self) -> None:
        self._three()
        doc = json.loads(self.store.manifest_path.read_text(encoding="utf-8"))
        doc["payload"]["sessions"][0]["title"] = "被改过"
        self.store.manifest_path.write_text(json.dumps(doc), encoding="utf-8")
        result = self.store.list_sessions()
        self.assertEqual(result.manifest_status, "corrupt")
        self.assertEqual(len(result.entries), 3)
        self.assertNotIn("被改过", [e.title for e in result.entries])

    def test_missing_manifest_rebuild_index_matches(self) -> None:
        self._three()
        before = self.store.list_sessions().entries
        manifest_before = json.loads(self.store.manifest_path.read_text("utf-8"))
        self.store.manifest_path.unlink()
        listed = self.store.list_sessions()
        self.assertEqual(listed.manifest_status, "missing")
        self.assertFalse(self.store.manifest_path.exists())  # 列表不写盘
        rebuilt = self.store.rebuild_index()
        self.assertTrue(self.store.manifest_path.exists())
        self.assertEqual(
            sorted(rebuilt.entries, key=lambda e: e.session_id),
            sorted(before, key=lambda e: e.session_id),
        )
        manifest_after = json.loads(self.store.manifest_path.read_text("utf-8"))
        key = lambda d: d["id"]
        self.assertEqual(
            sorted(manifest_after["payload"]["sessions"], key=key),
            sorted(manifest_before["payload"]["sessions"], key=key),
        )
        self.assertEqual(self.store.list_sessions().manifest_status, "ok")

    def test_rebuild_skips_and_counts_bad_sessions(self) -> None:
        self._three()
        (self.store.root / "s-2" / "session.json").write_text("{}", encoding="utf-8")
        result = self.store.rebuild_index()
        self.assertEqual(result.corrupt, 1)
        self.assertEqual(sorted(e.session_id for e in result.entries), ["s-1", "s-3"])


class VersionTest(_Base):
    def _bump(self, path: Path, version) -> None:
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["format_version"] = version
        path.write_text(json.dumps(doc), encoding="utf-8")

    def test_unknown_session_version_rejected_and_counted(self) -> None:
        self.store.save(self.session(sid="s-1"))
        self.store.save(self.session(sid="s-2"))
        self._bump(self.store.root / "s-1" / "session.json", 2)
        result = self.store.list_sessions()
        self.assertEqual([e.session_id for e in result.entries], ["s-2"])
        self.assertEqual(result.rejected_version, 1)
        self.assertEqual(result.corrupt, 0)
        with self.assertRaises(ss.UnsupportedVersionError):
            self.store.load("s-1")

    def test_unknown_manifest_version_falls_back_to_scan(self) -> None:
        self.store.save(self.session(sid="s-1"))
        self._bump(self.store.manifest_path, 99)
        result = self.store.list_sessions()
        self.assertEqual(result.manifest_status, "rejected_version")
        self.assertEqual([e.session_id for e in result.entries], ["s-1"])

    def test_migrate_placeholder_only_v1(self) -> None:
        self.assertEqual(ss.migrate({"format_version": 1})["format_version"], 1)
        for v in (0, 2, "1", None):
            with self.assertRaises(ss.UnsupportedVersionError):
                ss.migrate({"format_version": v})


class ToolRunStateTest(_Base):
    def test_six_states_roundtrip(self) -> None:
        self.assertEqual(
            ss.TOOL_STATES,
            ("pending", "running", "succeeded", "failed", "cancelled", "unknown"),
        )
        self.store.save(self.session(), self.runs())
        runs = self.store.load("s-1").tool_runs
        self.assertEqual([r.state for r in runs], list(ss.TOOL_STATES))
        self.assertEqual([r.to_dict() for r in runs], [r.to_dict() for r in self.runs()])

    def test_unknown_state_refused(self) -> None:
        with self.assertRaises(ValueError):
            ss.ToolRun("c", "shell", "{}", state="done")

    def test_running_and_unknown_never_rerun_on_load(self) -> None:
        self.store.save(self.session(), self.runs())
        from agentscope.tool import Toolkit
        from core import shell_tool

        with mock.patch.object(shell_tool, "_execute") as execute, \
             mock.patch.object(shell_tool, "run_shell") as run_shell, \
             mock.patch.object(shell_tool.ShellTool, "call") as tool_call, \
             mock.patch.object(Toolkit, "call_tool") as toolkit_call, \
             mock.patch("subprocess.Popen") as popen, \
             mock.patch("subprocess.run") as sub_run:
            loaded = self.store.load("s-1")
        for m in (execute, run_shell, tool_call, toolkit_call, popen, sub_run):
            m.assert_not_called()
        states = {r.call_id: r.state for r in loaded.tool_runs}
        self.assertEqual(states["call-1"], "running")
        self.assertEqual(states["call-5"], "unknown")
        attention = [r.state for r in loaded.tool_runs if r.needs_attention]
        self.assertEqual(attention, ["running", "unknown"])

    def test_args_summary_redacts_secrets_and_paths(self) -> None:
        summary = ss.summarize_args(
            {
                "cmd": f"cat {self.ws}/a.py /etc/passwd",
                "api_key": "sk-abcdef123456",
                "env": {"OPENAI_TOKEN": "plain-value-123"},
                "note": "token=abcd1234efgh",
            },
            self.ws,
        )
        self.assertNotIn("sk-abcdef123456", summary)
        self.assertNotIn("plain-value-123", summary)
        self.assertNotIn("abcd1234efgh", summary)
        self.assertNotIn(str(self.ws), summary)
        self.assertNotIn("/etc/passwd", summary)
        self.assertIn("a.py", summary)
        self.assertIn("…/passwd", summary)
        self.assertLessEqual(len(summary), ss.ARGS_SUMMARY_MAX)
        result = ss.summarize_result(f"wrote {self.ws}/a.py with sk-zzzzzzzz99")
        self.assertNotIn(str(self.ws), result)
        self.assertNotIn("sk-zzzzzzzz99", result)

    def test_missing_tool_runs_file_means_none(self) -> None:
        self.store.save(self.session(), self.runs())
        (self.store.root / "s-1" / "tool_runs.json").unlink()
        self.assertEqual(self.store.load("s-1").tool_runs, [])

    def test_corrupt_tool_runs_raises(self) -> None:
        self.store.save(self.session(), self.runs())
        (self.store.root / "s-1" / "tool_runs.json").write_text("x", encoding="utf-8")
        with self.assertRaises(ss.CorruptArchiveError):
            self.store.load("s-1")


class SafetyTest(_Base):
    def test_load_and_list_write_nothing(self) -> None:
        self.store.save(self.session(), self.runs())
        ws_before = _tree(self.ws)
        store_before = _tree(self.store.root)
        self.store.load("s-1")
        self.store.list_sessions()
        self.assertEqual(_tree(self.ws), ws_before)
        self.assertEqual(_tree(self.store.root), store_before)

    def test_load_never_opens_for_write(self) -> None:
        self.store.save(self.session(), self.runs())
        with mock.patch("core.session_store.atomic_write_text") as writer, \
             mock.patch("core.session_store.os.open") as raw_open, \
             mock.patch("core.session_store.os.replace") as replace:
            self.store.load("s-1")
            self.store.list_sessions()
        writer.assert_not_called()
        raw_open.assert_not_called()
        replace.assert_not_called()

    def test_archive_has_no_secret_values(self) -> None:
        s = self.session()
        runs = [ss.ToolRun("c", "shell",
                           ss.summarize_args({"password": "hunter2-secret"}, self.ws),
                           state="succeeded")]
        self.store.save(s, runs)
        blob = "".join(p.read_text("utf-8") for p in self.store.root.rglob("*.json"))
        self.assertNotIn("hunter2-secret", blob)


if __name__ == "__main__":
    unittest.main()
