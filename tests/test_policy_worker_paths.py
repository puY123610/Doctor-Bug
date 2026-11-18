"""Worker-surface path bounds: outside the work copy is deny, plus a read-only whitelist.

The worker has no confirmation channel. Reads outside the work copy are denied
except interpreter stdlib and site-packages derived from ``sys.path``. That
whitelist never overrides Tier 1. This is stricter than an ask-to-confirm flow.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import unicodedata
from pathlib import Path

from core.policy import PolicyContext, PolicyEngine, is_secret_path


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _stdlib_file(workspace: Path) -> Path:
    candidate = Path(os.__file__).resolve()
    root = candidate.parent
    if not root.is_dir() or _within(root, workspace):
        raise AssertionError(f"stdlib root {root} is not outside the workspace")
    on_sys_path = any(
        entry and Path(entry).resolve() == root for entry in sys.path
    )
    if not on_sys_path:
        raise AssertionError(f"stdlib root {root} is not a sys.path entry")
    return candidate


def _site_packages_file(workspace: Path) -> Path:
    for entry in sys.path:
        if not entry or "site-packages" not in entry:
            continue
        root = Path(entry).resolve()
        if not root.is_dir() or _within(root, workspace):
            continue
        for child in root.iterdir():
            if child.is_file() and child.suffix == ".py":
                return child
            init = child / "__init__.py"
            if child.is_dir() and init.is_file():
                return init
    raise AssertionError("no site-packages file outside the workspace")


class WorkerPathPolicyTests(unittest.TestCase):
    """File-read bounds for role=worker, and the shared Tier 1 credential rule."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.workspace = root / "workspace"
        self.original = self.workspace / "demo_repo"
        self.copy = self.workspace / "copies" / "demo_repo"
        self.vault = root / "vault"
        for directory in (self.original, self.copy, self.vault):
            directory.mkdir(parents=True)
        (self.original / "README.md").write_text("original\n", encoding="utf-8")
        (self.copy / "README.md").write_text("copy\n", encoding="utf-8")
        (self.copy / "app.py").write_text("print(1)\n", encoding="utf-8")
        (self.vault / "note.md").write_text("vault note\n", encoding="utf-8")
        (self.copy / ".env").write_text("TOKEN=fake\n", encoding="utf-8")
        (self.copy / ".env.example").write_text(
            "TOKEN=example\n", encoding="utf-8"
        )
        (self.copy / ".npmrc").write_text(
            "registry=https://example.test\n", encoding="utf-8"
        )
        (self.copy / ".netrc").write_text(
            "machine example.test\n", encoding="utf-8"
        )
        nested = self.copy / "pkg"
        nested.mkdir()
        (nested / ".env").write_text("TOKEN=nested\n", encoding="utf-8")
        self.engine = PolicyEngine()

    def _context(self, role: str) -> PolicyContext:
        return PolicyContext(
            mode="ask",
            surface="runtime",
            workspace=self.copy,
            vault_paths=(self.vault,),
            role=role,
            is_ai=True,
            actor=role,
        )

    def _decide(
        self,
        role: str,
        action: str,
        path: str | Path,
    ):
        return self.engine.decide(
            role, action, os.fspath(path), self._context(role)
        )

    def test_worker_read_stdlib_allowed(self) -> None:
        target = _stdlib_file(self.copy)
        decision = self._decide("worker", "read_file", target)
        self.assertEqual(decision.verdict, "allow")

    def test_worker_read_site_packages_allowed(self) -> None:
        target = _site_packages_file(self.copy)
        decision = self._decide("worker", "read_file", target)
        self.assertEqual(decision.verdict, "allow")

    def test_worker_write_site_packages_denied(self) -> None:
        target = _site_packages_file(self.copy)
        decision = self._decide("worker", "write_file", target)
        self.assertEqual(decision.verdict, "deny")

    def test_worker_read_ssh_denied(self) -> None:
        target = Path.home() / ".ssh" / "PERM1C_no_read"
        decision = self._decide("worker", "read_file", target)
        self.assertEqual(decision.verdict, "deny")
        self.assertEqual(decision.tier, 1)
        folded = Path.home() / ".SSH" / "PERM1C_no_read"
        folded_decision = self._decide("worker", "read_file", folded)
        self.assertEqual(folded_decision.verdict, "deny")
        self.assertEqual(folded_decision.tier, 1)

    def test_worker_read_drbug_config_denied(self) -> None:
        target = Path.home() / ".config" / "drbug" / "PERM1C_no_read"
        decision = self._decide("worker", "read_file", target)
        self.assertEqual(decision.verdict, "deny")
        self.assertEqual(decision.tier, 1)

    def test_worker_read_env_anywhere_denied(self) -> None:
        outside = self.workspace.parent / "elsewhere"
        outside.mkdir()
        env_file = outside / ".env"
        env_file.write_text("TOKEN=outside\n", encoding="utf-8")
        for target in (
            self.copy / ".env",
            self.copy / "pkg" / ".env",
            env_file,
        ):
            with self.subTest(target=str(target)):
                decision = self._decide("worker", "read_file", target)
                self.assertEqual(decision.verdict, "deny")
                self.assertEqual(decision.tier, 1)

    def test_worker_read_env_example_in_copy_allowed(self) -> None:
        decision = self._decide(
            "worker", "read_file", self.copy / ".env.example"
        )
        self.assertEqual(decision.verdict, "allow")
        self.assertFalse(is_secret_path(self.copy / ".env.example"))
        self.assertTrue(is_secret_path(self.copy / ".env"))

    def test_credential_example_suffixes_are_not_secret(self) -> None:
        for name in (".env.sample", ".env.template", ".npmrc.example"):
            path = self.copy / name
            path.write_text("placeholder\n", encoding="utf-8")
            with self.subTest(name=name):
                self.assertFalse(is_secret_path(path))
                decision = self._decide("worker", "read_file", path)
                self.assertEqual(decision.verdict, "allow")
        local = self.copy / ".env.local"
        local.write_text("TOKEN=local\n", encoding="utf-8")
        self.assertTrue(is_secret_path(local))
        denied = self._decide("worker", "read_file", local)
        self.assertEqual(denied.verdict, "deny")
        self.assertEqual(denied.tier, 1)

    def test_worker_read_npmrc_and_netrc_in_copy_denied(self) -> None:
        for name in (".npmrc", ".netrc", ".envrc", ".pypirc"):
            path = self.copy / name
            if not path.exists():
                path.write_text("placeholder\n", encoding="utf-8")
            with self.subTest(name=name):
                decision = self._decide("worker", "read_file", path)
                self.assertEqual(decision.verdict, "deny")
                self.assertEqual(decision.tier, 1)
                self.assertTrue(is_secret_path(path))

    def test_worker_read_vault_denied(self) -> None:
        decision = self._decide(
            "worker", "read_file", self.vault / "note.md"
        )
        self.assertEqual(decision.verdict, "deny")
        self.assertEqual(decision.tier, 1)

    def test_worker_read_original_repo_denied(self) -> None:
        decision = self._decide(
            "worker", "read_file", self.original / "README.md"
        )
        self.assertEqual(decision.verdict, "deny")

    def test_worker_read_normal_file_in_copy_allowed(self) -> None:
        decision = self._decide("worker", "read_file", self.copy / "app.py")
        self.assertEqual(decision.verdict, "allow")

    def test_worker_read_env_case_variants_denied(self) -> None:
        case_dir = self.copy / "case"
        case_dir.mkdir()
        env_upper = case_dir / ".ENV"
        npmrc_mixed = case_dir / ".NpmRc"
        env_upper.write_text("TOKEN=upper\n", encoding="utf-8")
        npmrc_mixed.write_text(
            "registry=https://example.test\n", encoding="utf-8"
        )
        for target in (env_upper, npmrc_mixed):
            with self.subTest(target=target.name):
                decision = self._decide("worker", "read_file", target)
                self.assertEqual(decision.verdict, "deny")
                self.assertEqual(decision.tier, 1)

    def test_worker_read_nfd_env_denied(self) -> None:
        nfd_name = unicodedata.normalize("NFD", ".env")
        # A combining character that NFC composes away from the ASCII
        # basename would not be ".env"; use NFD of the basename itself and
        # a non-ASCII directory so the path bytes differ from NFC when the
        # platform preserves the normalization form.
        nfd_dir = self.copy / unicodedata.normalize("NFD", "caf\u00e9")
        nfd_dir.mkdir()
        target = nfd_dir / nfd_name
        target.write_text("TOKEN=nfd\n", encoding="utf-8")
        decision = self._decide("worker", "read_file", target)
        self.assertEqual(decision.verdict, "deny")
        self.assertEqual(decision.tier, 1)
        as_nfc = Path(unicodedata.normalize("NFC", str(target)))
        nfc_decision = self._decide("worker", "read_file", as_nfc)
        self.assertEqual(nfc_decision.verdict, "deny")
        self.assertEqual(nfc_decision.tier, 1)

    def test_worker_read_symlink_to_ssh_denied(self) -> None:
        link = self.copy / "looks_fine.txt"
        link.symlink_to(Path.home() / ".ssh" / "PERM1C_no_read")
        decision = self._decide("worker", "read_file", link)
        self.assertEqual(decision.verdict, "deny")
        self.assertEqual(decision.tier, 1)

    def test_worker_read_dotdot_escape_denied(self) -> None:
        decision = self._decide(
            "worker",
            "read_file",
            "../../demo_repo/README.md",
        )
        self.assertEqual(decision.verdict, "deny")

    def test_worker_read_renamed_to_env_denied(self) -> None:
        folder = self.copy / "rename_case"
        folder.mkdir()
        source = folder / "notes.txt"
        source.write_text("hello\n", encoding="utf-8")
        before = self._decide("worker", "read_file", source)
        self.assertEqual(before.verdict, "allow")
        renamed = source.rename(folder / ".env")
        after = self._decide("worker", "read_file", renamed)
        self.assertEqual(after.verdict, "deny")
        self.assertEqual(after.tier, 1)

    def test_secretary_read_project_env_tier1_deny(self) -> None:
        decision = self._decide(
            "secretary", "read_file", self.copy / ".env"
        )
        self.assertEqual(decision.verdict, "deny")
        self.assertEqual(decision.tier, 1)

    def test_secretary_read_env_example_not_tier1_deny(self) -> None:
        decision = self._decide(
            "secretary", "read_file", self.copy / ".env.example"
        )
        self.assertFalse(
            decision.verdict == "deny" and decision.tier == 1,
            msg=repr(decision),
        )

    def test_whitelist_does_not_override_tier1(self) -> None:
        fake_site = self.workspace.parent / "fake_site"
        fake_site.mkdir()
        secret = fake_site / ".env"
        secret.write_text("TOKEN=fake-site\n", encoding="utf-8")
        sys.path.insert(0, str(fake_site))
        self.addCleanup(lambda: sys.path.remove(str(fake_site)))
        decision = self._decide("worker", "read_file", secret)
        self.assertEqual(decision.verdict, "deny")
        self.assertEqual(decision.tier, 1)

    def test_whitelist_does_not_cover_workspace_even_if_on_sys_path(self) -> None:
        sys.path.insert(0, str(self.original))
        self.addCleanup(lambda: sys.path.remove(str(self.original)))
        decision = self._decide(
            "worker", "read_file", self.original / "README.md"
        )
        self.assertEqual(decision.verdict, "deny")


if __name__ == "__main__":
    unittest.main()

class WorkerGateStructureTests(unittest.TestCase):
    """常驻结构检查：工人工具要么过门禁、要么在豁免表里。"""

    EXEMPTIONS = {"Glob": "后端限定工作区、只列名不读内容"}

    def _tools(self):
        from core.agentscope_actors import build_toolkit

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        toolkit = build_toolkit(workspace=Path(tmp.name))
        tools = []
        groups = getattr(toolkit, "tool_groups", None)
        if groups:
            for group in groups:
                tools.extend(group.tools)
        else:
            tools.extend(toolkit.tools)
        return tools

    def test_every_worker_tool_is_gated_or_exempt(self) -> None:
        from core import agentscope_actors

        for tool in self._tools():
            name = getattr(tool, "name", None) or type(tool).__name__
            with self.subTest(tool=name):
                self.assertTrue(
                    name in agentscope_actors._POLICY_ACTIONS
                    or name in self.EXEMPTIONS,
                    f"工具 {name} 既未过门禁也不在豁免表里",
                )

    def test_gated_tools_actually_deny_secret_probe(self) -> None:
        from core import agentscope_actors

        probe = {"file_path": str(Path.home() / ".ssh" / "x")}
        for tool in self._tools():
            name = getattr(tool, "name", None) or type(tool).__name__
            if name not in agentscope_actors._POLICY_ACTIONS:
                continue
            with self.subTest(tool=name):
                self.assertIsNotNone(
                    agentscope_actors._policy_denial(tool, probe)
                )

    def test_worker_ask_lands_as_deny(self) -> None:
        from core import agentscope_actors
        from core.policy import Decision, PolicyEngine

        class _AskEngine(PolicyEngine):
            def decide(self, actor, action, target, ctx=None):
                return Decision(
                    "ask", 2, "stub ask", normalized_target=target
                )

        read_tool = None
        for tool in self._tools():
            name = getattr(tool, "name", None) or type(tool).__name__
            if name == "Read":
                read_tool = tool
        self.assertIsNotNone(read_tool)
        read_tool._policy_engine = _AskEngine(
            workspace=read_tool._policy_workspace
        )
        denied = agentscope_actors._policy_denial(
            read_tool, {"file_path": "app.py"}
        )
        self.assertIsNotNone(denied)
        self.assertIn("deny", str(denied.behavior).lower())
