"""SHIM1: 仓库根 drbug 垫片在无 .venv 新克隆、不同 locale 下的回归测试。

故障：第 8 行 `$PYTHON；` 在 UTF-8 locale（Mac 的 bash 3.2 / sh）下把全角分号吞进变量名，
set -u 报 `PYTHON?: unbound variable`。Linux 的 dash/bash 5 不会这样解析，所以这里另加一条
静态检查，在任何平台都能拦住同类写法。
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / "drbug"
LOCALES = ("C", "en_US.UTF-8")


def _shells():
    return [s for s in ("sh", "bash") if shutil.which(s)]


class ShimNoVenvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="drbug-shim-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        shutil.copy2(SHIM, self.tmp / "drbug")
        self.assertFalse((self.tmp / ".venv").exists())

    def _run(self, shell, loc):
        env = dict(os.environ)
        env.pop("LANG", None)
        env.pop("LC_CTYPE", None)
        env["LC_ALL"] = loc
        return subprocess.run(
            [shell, "./drbug"], cwd=self.tmp, env=env,
            capture_output=True, timeout=30,
        )

    def _check(self, loc):
        shells = _shells()
        self.assertTrue(shells, "找不到 sh/bash")
        for shell in shells:
            with self.subTest(shell=shell, locale=loc):
                proc = self._run(shell, loc)
                out = (proc.stdout + proc.stderr).decode("utf-8", "replace")
                self.assertEqual(proc.returncode, 127, out)
                self.assertIn("找不到", out)
                self.assertIn(str(self.tmp / ".venv" / "bin" / "python"), out)
                self.assertIn("请先按 README 建好项目 .venv", out)
                self.assertNotIn("unbound variable", out)

    def test_no_venv_c_locale(self):
        self._check("C")

    def test_no_venv_utf8_locale(self):
        self._check("en_US.UTF-8")


class ShimSourceTest(unittest.TestCase):
    def test_no_bare_variable_followed_by_non_ascii(self):
        bad = []
        text = SHIM.read_text("utf-8")
        for no, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for m in re.finditer(r"\$[A-Za-z_][A-Za-z0-9_]*", line):
                nxt = line[m.end():m.end() + 1]
                if nxt and ord(nxt) > 127:
                    bad.append((no, line))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
