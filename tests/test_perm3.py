# -*- coding: utf-8 -*-
"""PERM3: frozen check contract, anti-fake-green guards, behavior."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptor.acceptor import FrozenAcceptor
from acceptor.frozen import MANIFEST_NAME, freeze, freeze_behavior


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_ok.py").write_text(
        "from pkg import VALUE\n\n\ndef test_value():\n"
        "    assert VALUE == 1\n",
        encoding="utf-8",
    )
    return repo


def _hand_vault(root: Path, repo: Path, script_text: str) -> Path:
    """A vault whose check command runs a canned-output script."""

    vault = root / "vault"
    vault.mkdir(parents=True)
    test_file = vault / "tests" / "test_ok.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        (repo / "tests" / "test_ok.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    script = vault / "fake_suite.py"
    script.write_text(script_text, encoding="utf-8")
    # manifest 里的文件在判卷时要与工作区同名文件对哈希，
    # 所以工作区里也放一份同内容（它不是判卷面文件，
    # 只是让篡改核对能对上）。
    (repo / "fake_suite.py").write_text(script_text, encoding="utf-8")
    manifest = {
        "files": {
            "tests/test_ok.py": _sha(test_file),
            "fake_suite.py": _sha(script),
        },
        "package": "pkg",
        "source": str(repo),
        "check_command": ["{python}", "fake_suite.py"],
        "parser": "pytest",
        "kind": "tests",
    }
    (vault / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return vault


class FrozenContractTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="perm3-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = _repo(self.root)

    def test_freeze_writes_contract_keys(self) -> None:
        vault = self.root / "vault"
        manifest = freeze(self.repo, vault, package="pkg")
        self.assertEqual(
            manifest["check_command"],
            ["{python}", "-m", "pytest", "-q"],
        )
        self.assertEqual(manifest["parser"], "pytest")
        self.assertEqual(manifest["kind"], "tests")

    def test_freeze_rejects_unknown_parser(self) -> None:
        with self.assertRaises(ValueError):
            freeze(
                self.repo, self.root / "vault",
                package="pkg", parser="nope",
            )

    def test_old_manifest_without_contract_still_works(self) -> None:
        vault = self.root / "vault"
        freeze(self.repo, vault, package="pkg")
        manifest_path = vault / MANIFEST_NAME
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
        for key in ("check_command", "parser", "kind"):
            manifest.pop(key, None)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        verdict = FrozenAcceptor(vault).check(self.repo)
        self.assertEqual(verdict.status, "PASS")

    def test_unknown_parser_in_manifest_rejected(self) -> None:
        vault = self.root / "vault"
        freeze(self.repo, vault, package="pkg")
        manifest_path = vault / MANIFEST_NAME
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8")
        )
        manifest["parser"] = "nope"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            FrozenAcceptor(vault)


class AntiFakeGreenTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="perm3-fg-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = _repo(self.root)

    def test_empty_run_is_not_a_pass(self) -> None:
        vault = _hand_vault(
            self.root, self.repo,
            "print('no tests ran in 0.01s')\n",
        )
        verdict = FrozenAcceptor(vault).check(self.repo)
        self.assertEqual(verdict.status, "FAIL")
        self.assertIn("未真正执行", verdict.detail)

    def test_failed_lines_beat_zero_failed_count(self) -> None:
        vault = _hand_vault(
            self.root, self.repo,
            "print('FAILED tests/test_ok.py::test_value - boom')\n"
            "print('1 passed, 0 failed')\n",
        )
        verdict = FrozenAcceptor(vault).check(self.repo)
        self.assertEqual(verdict.status, "FAIL")
        self.assertIn(
            "tests/test_ok.py::test_value", verdict.failed_tests
        )

    def test_real_pass_still_passes(self) -> None:
        vault = _hand_vault(
            self.root, self.repo, "print('3 passed in 0.02s')\n"
        )
        verdict = FrozenAcceptor(vault).check(self.repo)
        self.assertEqual(verdict.status, "PASS")
        self.assertEqual(verdict.passed_count, 3)


class BehaviorCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="perm3-bh-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = _repo(self.root)

    def _freeze(self, body: str) -> Path:
        script = self.repo / "check_behavior.py"
        script.write_text(body, encoding="utf-8")
        vault = self.root / "vault"
        freeze_behavior(
            self.repo, vault, script="check_behavior.py",
            package="pkg",
        )
        return vault

    def test_behavior_pass_and_fail(self) -> None:
        ok_vault = self._freeze(
            "from pkg import VALUE\n"
            "raise SystemExit(0 if VALUE == 1 else 1)\n"
        )
        verdict = FrozenAcceptor(ok_vault).check(self.repo)
        self.assertEqual(verdict.status, "PASS")

        bad_repo = _repo(self.root / "second")
        (bad_repo / "pkg" / "__init__.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        script = bad_repo / "check_behavior.py"
        script.write_text(
            "from pkg import VALUE\n"
            "raise SystemExit(0 if VALUE == 1 else 1)\n",
            encoding="utf-8",
        )
        bad_vault = self.root / "vault2"
        freeze_behavior(
            bad_repo, bad_vault, script="check_behavior.py",
            package="pkg",
        )
        verdict2 = FrozenAcceptor(bad_vault).check(bad_repo)
        self.assertEqual(verdict2.status, "FAIL")

    def test_workspace_tests_do_not_tamper_behavior_vault(self) -> None:
        vault = self._freeze("raise SystemExit(0)\n")
        verdict = FrozenAcceptor(vault).check(self.repo)
        self.assertEqual(verdict.status, "PASS")
        self.assertEqual(verdict.tampered_files, [])

    def test_behavior_vault_tamper_detected(self) -> None:
        vault = self._freeze("raise SystemExit(0)\n")
        (vault / "check_behavior.py").write_text(
            "raise SystemExit(1)\n", encoding="utf-8"
        )
        with self.assertRaises(RuntimeError):
            FrozenAcceptor(vault)


class NonUtf8OutputTests(unittest.TestCase):
    """判卷命令吐非法 UTF-8 字节时不得崩：替换解码、照常出裁决。

    E2E 案例 3 实撞：gitea 的截断测试打印原始字节，
    check() 曾以 UnicodeDecodeError 死亡。
    """

    def test_invalid_utf8_output_still_verdicts(self) -> None:
        payload = b"".join([
            bytes([0xFF, 0xFE]),
            b" garbage\n",
            b"FAILED tests/test_ok.py::test_value - boom\n",
            b"==== 1 failed, 1 passed in 0.01s ====\n",
        ])
        script = (
            "import sys\n"
            "sys.stdout.buffer.write(" + repr(payload) + ")\n"
            "raise SystemExit(1)\n"
        )

        with tempfile.TemporaryDirectory(prefix="perm3-utf8-") as tmp:
            root = Path(tmp)
            repo = _repo(root)
            vault = _hand_vault(root, repo, script)
            verdict = FrozenAcceptor(vault).check(repo)
            self.assertEqual(verdict.status, "FAIL")
            self.assertEqual(
                verdict.failed_tests, ["tests/test_ok.py::test_value"]
            )


class RunDirCompletenessTests(unittest.TestCase):
    """F4 回归：包外辅助文件必须能进判卷运行目录。

    仓库根有 helper.py（非判卷面文件），测试经根 conftest
    引入它。旧实现只拷包目录时 helper 缺席、修复版也 FAIL；
    现在父版 FAIL（断言）、修复版 PASS。
    """

    def _tree(self, root: Path, value: int) -> Path:
        repo = root / "repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "__init__.py").write_text(
            f"VALUE = {value}\n", encoding="utf-8"
        )
        (repo / "helper.py").write_text(
            "from pkg import VALUE\n\n\n"
            "def compute():\n    return VALUE * 10\n",
            encoding="utf-8",
        )
        (repo / "conftest.py").write_text("", encoding="utf-8")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_ok.py").write_text(
            "from helper import compute\n\n\n"
            "def test_compute():\n    assert compute() == 20\n",
            encoding="utf-8",
        )
        return repo

    def test_out_of_package_helper_reaches_run_dir(self) -> None:
        with tempfile.TemporaryDirectory(prefix="f4-") as tmp:
            root = Path(tmp)
            fixed = self._tree(root / "fix", 2)
            parent = self._tree(root / "parent", 1)
            vault = root / "vault"
            freeze(fixed, vault, package="pkg")
            acceptor = FrozenAcceptor(vault)
            bad = acceptor.check(parent)
            self.assertEqual(bad.status, "FAIL")
            self.assertEqual(
                bad.failed_tests, ["tests/test_ok.py::test_compute"]
            )
            good = acceptor.check(fixed)
            self.assertEqual(good.status, "PASS")


class RunDirSymlinkTests(unittest.TestCase):
    """符号链接布局（如 pnpm 的 node_modules）进运行目录须保链。

    解引用复制会把 .pnpm 的同级依赖关系拆散，vitest 在运行
    目录里找不到自身依赖（E2E 案例 3 实撞）。保链后链接原样
    保留、透过链接可读；判卷文件仍由 vault 覆盖工作区版本。
    """

    def test_symlink_preserved_and_vault_overlay_wins(self) -> None:
        with tempfile.TemporaryDirectory(prefix="f4-link-") as tmp:
            root = Path(tmp)
            fixed = root / "fix"
            (fixed / "pkg").mkdir(parents=True)
            (fixed / "pkg" / "__init__.py").write_text(
                "VALUE = 2\n", encoding="utf-8"
            )
            (fixed / "real_nm").mkdir()
            (fixed / "real_nm" / "data.txt").write_text(
                "linked-ok", encoding="utf-8"
            )
            (fixed / "nm").symlink_to("real_nm")
            (fixed / "tests").mkdir()
            (fixed / "tests" / "test_ok.py").write_text(
                "from pathlib import Path\n\n\n"
                "def test_link():\n"
                "    data = (Path(__file__).resolve().parents[1]"
                " / 'nm' / 'data.txt')\n"
                "    assert data.read_text(encoding='utf-8')"
                " == 'linked-ok'\n",
                encoding="utf-8",
            )
            vault = root / "vault"
            freeze(fixed, vault, package="pkg")
            run_dir = root / "run"
            FrozenAcceptor(vault)._prepare_run_dir(fixed, run_dir)
            self.assertTrue((run_dir / "nm").is_symlink())
            self.assertEqual(
                (run_dir / "nm" / "data.txt").read_text(
                    encoding="utf-8"
                ),
                "linked-ok",
            )
            verdict = FrozenAcceptor(vault).check(fixed)
            self.assertEqual(verdict.status, "PASS")


if __name__ == "__main__":
    unittest.main()
