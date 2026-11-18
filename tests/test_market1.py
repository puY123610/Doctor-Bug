# -*- coding: utf-8 -*-
"""MARKET1: local extension catalog — browse, install, uninstall."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.market import (
    format_entry_line,
    install,
    installed_ids,
    load_catalog,
    uninstall,
)
from core.mcp_tools import load_mcp_servers

CATALOG = {
    "entries": [
        {
            "id": "fs-tools",
            "name": "文件系统工具",
            "kind": "mcp",
            "description": "官方文件系统服务器",
            "version": "1.0.0",
            "server": {
                "name": "filesystem",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem"],
            },
        },
        {
            "id": "repo-triage",
            "name": "仓库分诊流程包",
            "kind": "skill",
            "description": "分诊流程",
            "version": "1.0.0",
            "pack": "skills/repo-triage",
        },
    ]
}


class MarketFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="market1-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.market_dir = root / "market"
        pack = self.market_dir / "skills" / "repo-triage"
        pack.mkdir(parents=True)
        (pack / "manifest.json").write_text(
            json.dumps({"id": "repo-triage", "name": "仓库分诊"}),
            encoding="utf-8",
        )
        (pack / "SKILL.md").write_text("# 分诊\n", encoding="utf-8")
        self.catalog_path = self.market_dir / "catalog.json"
        self.catalog_path.write_text(
            json.dumps(CATALOG, ensure_ascii=False), encoding="utf-8"
        )
        self.mcp_path = root / "cfg" / "mcp.json"
        self.skills_dir = root / "cfg" / "skills"

    def _kw(self) -> dict:
        return {
            "catalog_path": self.catalog_path,
            "mcp_config_path": self.mcp_path,
            "skills_dir": self.skills_dir,
        }


class CatalogTests(MarketFixture):
    def test_shipped_catalog_is_valid(self) -> None:
        entries = load_catalog()
        ids = {e["id"] for e in entries}
        self.assertEqual(
            ids,
            {"fs-tools", "memory-store", "web-fetch", "git-tools", "repo-triage"},
        )

    def test_duplicate_id_rejected(self) -> None:
        bad = {"entries": CATALOG["entries"] + [CATALOG["entries"][0]]}
        self.catalog_path.write_text(
            json.dumps(bad, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            load_catalog(self.catalog_path)

    def test_mcp_entry_without_server_rejected(self) -> None:
        bad = {"entries": [{"id": "x", "name": "x", "kind": "mcp"}]}
        self.catalog_path.write_text(
            json.dumps(bad, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            load_catalog(self.catalog_path)


class McpInstallTests(MarketFixture):
    def test_install_registers_and_preserves_others(self) -> None:
        self.mcp_path.parent.mkdir(parents=True)
        self.mcp_path.write_text(
            json.dumps(
                {"servers": [{"name": "keepme", "command": "true", "args": []}]}
            ),
            encoding="utf-8",
        )
        message = install("fs-tools", **self._kw())
        self.assertIn("已安装", message)
        names = [s["name"] for s in load_mcp_servers(self.mcp_path)]
        self.assertEqual(names, ["keepme", "filesystem"])
        with self.assertRaises(ValueError):
            install("fs-tools", **self._kw())

    def test_install_refuses_to_clobber_broken_config(self) -> None:
        self.mcp_path.parent.mkdir(parents=True)
        self.mcp_path.write_text("{broken", encoding="utf-8")
        before = self.mcp_path.read_bytes()
        with self.assertRaises(ValueError):
            install("fs-tools", **self._kw())
        self.assertEqual(self.mcp_path.read_bytes(), before)

    def test_uninstall_removes_only_that_server(self) -> None:
        install("fs-tools", **self._kw())
        message = uninstall("fs-tools", **self._kw())
        self.assertIn("已卸载", message)
        self.assertEqual(load_mcp_servers(self.mcp_path), [])
        with self.assertRaises(ValueError):
            uninstall("fs-tools", **self._kw())


class SkillInstallTests(MarketFixture):
    def test_install_copy_and_uninstall(self) -> None:
        install("repo-triage", **self._kw())
        target = self.skills_dir / "repo-triage"
        self.assertTrue((target / "SKILL.md").is_file())
        with self.assertRaises(ValueError):
            install("repo-triage", **self._kw())
        uninstall("repo-triage", **self._kw())
        self.assertFalse(target.exists())

    def test_uninstall_refuses_foreign_directory(self) -> None:
        foreign = self.skills_dir / "repo-triage"
        foreign.mkdir(parents=True)
        (foreign / "manifest.json").write_text(
            json.dumps({"id": "someone-else"}), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            uninstall("repo-triage", **self._kw())
        self.assertTrue(foreign.exists())


class ListingTests(MarketFixture):
    def test_installed_ids_and_line_marks(self) -> None:
        entries = load_catalog(self.catalog_path)
        id_kw = {
            "mcp_config_path": self.mcp_path,
            "skills_dir": self.skills_dir,
        }
        self.assertEqual(installed_ids(**id_kw), set())
        install("fs-tools", **self._kw())
        install("repo-triage", **self._kw())
        self.assertEqual(
            installed_ids(**id_kw), {"fs-tools", "repo-triage"}
        )
        line = format_entry_line(entries[0], True)
        self.assertIn("[已安装]", line)
        self.assertIn("MCP 服务器", line)
        self.assertIn("[未安装]", format_entry_line(entries[0], False))


if __name__ == "__main__":
    unittest.main()
