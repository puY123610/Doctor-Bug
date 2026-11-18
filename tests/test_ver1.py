"""VER1：员工整套定义（档案+名册+派活对照表）的版本号、变更记录、快照与回滚。

断言数据全部来自真实建档产物（build_profile 跑 tests/fixtures/demo_repo）
与真实派活路径（kb.lookup.DispatchKB.from_file(...).lookup）。
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from acceptor.evidence import Evidence
from core.agent_archive import append_memory_entries, read_archive
from core.agent_versions import (
    VersionError,
    begin,
    check_drift,
    current_version,
    load_snapshot,
    load_state,
    rollback,
    version_header,
    with_version_header,
)
from core.autoprofile import build_profile, default_profile_name
from core.roster import add_worker, list_roster, remove_worker
from kb.lookup import DispatchKB

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"
BILLING_NODE = "tests/test_billing.py::test_tax_on_discounted_total"
WHO = "billing-specialist"


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class Ver1Base(unittest.TestCase):
    _seed: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls) -> None:
        cls._seed = tempfile.TemporaryDirectory(prefix="ver1-seed-")
        root = Path(cls._seed.name)
        workspace = root / "ws"
        shutil.copytree(DEMO_REPO, workspace)
        build_profile(workspace, root / "profiles", default_profile_name(workspace))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._seed.cleanup()

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="ver1-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "r"
        shutil.copytree(self._seed.name, self.root)
        self.workspace = self.root / "ws"
        self.profiles = self.root / "profiles"
        self.profile = self.profiles / default_profile_name(self.workspace)
        self.table_path = self.profile / "dispatch_table.json"

    # 真实派活路径：只读 lookup 的返回
    def dispatch(self, node: str = BILLING_NODE) -> dict[str, list[str]]:
        kb = DispatchKB.from_file(self.table_path)
        module = node.split("::", 1)[0].rsplit("/", 1)[-1][len("test_"):-len(".py")]
        return kb.lookup([Evidence(node_id=node, module=module)]).assignments

    def move_billing_to_orders(self, label: str = "改辖区") -> list[dict]:
        """经版本入口把 billing-specialist 的辖区从 billing.py 挪到 orders.py。"""
        change = begin(self.profile)
        table = _read(self.table_path)
        table["roster"]["specialists"][WHO]["files"] = ["backoffice/orders.py"]
        for item in table["specialists"]:
            if item["id"] == WHO:
                item["files"] = ["backoffice/orders.py"]
        table["module_defaults"]["billing"] = [
            name for name in table["module_defaults"]["billing"] if name != WHO
        ]
        if not table["module_defaults"]["billing"]:
            del table["module_defaults"]["billing"]
        table["module_defaults"]["orders"].append(WHO)
        _write(self.table_path, table)
        return change.commit(label)


class Ver1Test(Ver1Base):
    def test_1_change_scope_bumps_version_and_logs(self) -> None:
        self.assertEqual(current_version(self.profile, WHO), 1)
        identity_before = (self.profile / "agents" / WHO / "IDENTITY.md").read_bytes()
        events = self.move_billing_to_orders("改辖区：billing.py → orders.py")
        self.assertEqual([e["name"] for e in events], [WHO, WHO])
        state = load_state(self.profile, WHO)
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["log"][-1]["detail"], "改辖区：billing.py → orders.py")
        self.assertEqual(state["log"][0]["kind"], "baseline")
        table = _read(self.table_path)
        self.assertEqual(table["roster"]["specialists"][WHO]["files"], ["backoffice/orders.py"])
        self.assertIn(WHO, table["module_defaults"]["orders"])
        self.assertNotIn("billing", table["module_defaults"])
        # 档案：IDENTITY.md 一字节不碰，版本头在展示时带上
        self.assertEqual(
            (self.profile / "agents" / WHO / "IDENTITY.md").read_bytes(), identity_before
        )
        archive = read_archive(self.profile, WHO)
        shown = with_version_header(archive.identity, version_header(self.profile, WHO))
        self.assertIn("- 版本: v2", shown)
        self.assertIn("改辖区：billing.py → orders.py", shown)
        self.assertTrue(shown.startswith(f"# IDENTITY — {WHO}\n"))

    def test_2_rollback_restores_all_three_carriers(self) -> None:
        table_before = _read(self.table_path)
        meta_before = _read(self.profile / "profile.json")
        identity_before = (self.profile / "agents" / WHO / "IDENTITY.md").read_bytes()
        self.move_billing_to_orders()
        self.assertNotEqual(_read(self.table_path), table_before)
        result = rollback(self.profile, WHO, 1)
        self.assertTrue(result["restored"])
        self.assertEqual(result["version"], 3)
        table_after = _read(self.table_path)
        self.assertEqual(
            table_after["roster"]["specialists"][WHO],
            table_before["roster"]["specialists"][WHO],
        )
        self.assertEqual(table_after["specialists"], table_before["specialists"])
        self.assertEqual(table_after["module_defaults"], table_before["module_defaults"])
        self.assertEqual(table_after["node_overrides"], table_before["node_overrides"])
        self.assertEqual(table_after, table_before)
        self.assertEqual(_read(self.profile / "profile.json"), meta_before)
        self.assertEqual(
            (self.profile / "agents" / WHO / "IDENTITY.md").read_bytes(), identity_before
        )
        self.assertEqual(load_state(self.profile, WHO)["log"][-1]["detail"], "从 v2 回滚到 v1")

    def test_3_dispatch_after_rollback_follows_old_scope(self) -> None:
        self.assertEqual(self.dispatch(), {WHO: [BILLING_NODE]})
        self.move_billing_to_orders()
        self.assertEqual(self.dispatch(), {"generalist": [BILLING_NODE]})
        rollback(self.profile, WHO, 1)
        self.assertEqual(self.dispatch(), {WHO: [BILLING_NODE]})

    def test_4_memory_bytes_untouched_by_snapshot_and_rollback(self) -> None:
        append_memory_entries(
            self.profile,
            WHO,
            [{"test": BILLING_NODE, "file": "backoffice/billing.py", "cause": "税基"}],
        )
        memory = self.profile / "agents" / WHO / "MEMORY.md"
        before = memory.read_bytes()
        self.assertIn("税基".encode("utf-8"), before)
        self.move_billing_to_orders()
        self.assertEqual(memory.read_bytes(), before)
        rollback(self.profile, WHO, 1)
        self.assertEqual(memory.read_bytes(), before)
        for path in (self.profile / "agents" / WHO / "versions").iterdir():
            self.assertNotIn("税基", path.read_text(encoding="utf-8"))

    def test_5_legacy_profile_loads_and_first_change_starts_at_v1(self) -> None:
        agents = self.profile / "agents"
        self.assertFalse(any(agents.glob("*/VERSION.json")))
        rows = list_roster(self.workspace, profiles_root=self.profiles)
        self.assertIn(WHO, [row["name"] for row in rows])
        self.assertEqual(current_version(self.profile, WHO), 1)
        self.assertEqual(version_header(self.profile, WHO), "")
        self.move_billing_to_orders()
        state = load_state(self.profile, WHO)
        self.assertEqual([line["version"] for line in state["log"]], [1, 2])
        v1 = load_snapshot(self.profile, WHO, 1)
        self.assertEqual(v1["definition"]["module_defaults"], ["billing"])

    def test_6_two_changes_reach_v3_and_roll_back_to_v1(self) -> None:
        table_v1 = _read(self.table_path)
        self.move_billing_to_orders("第一次")
        change = begin(self.profile)
        table = _read(self.table_path)
        table["roster"]["specialists"][WHO]["blurb"] = "订单计价与下单流程。"
        _write(self.table_path, table)
        change.commit("第二次：改 blurb")
        self.assertEqual(current_version(self.profile, WHO), 3)
        for version in (1, 2, 3):
            self.assertIsNotNone(load_snapshot(self.profile, WHO, version))
        rollback(self.profile, WHO, 1)
        self.assertEqual(_read(self.table_path), table_v1)
        self.assertEqual(current_version(self.profile, WHO), 4)
        self.assertEqual(self.dispatch(), {WHO: [BILLING_NODE]})


class Ver1EntryPathsTest(Ver1Base):
    def test_unchanged_people_get_no_version_file(self) -> None:
        self.move_billing_to_orders()
        written = sorted(p.parent.name for p in (self.profile / "agents").glob("*/VERSION.json"))
        # orders-specialist 的 module_defaults.orders 列表没少它，定义不变，不记版
        self.assertEqual(written, [WHO])

    def test_external_edit_recorded_once_and_only_on_mismatch(self) -> None:
        self.move_billing_to_orders()
        state_path = self.profile / "agents" / WHO / "VERSION.json"
        stamp = state_path.read_bytes()
        self.assertEqual(check_drift(self.profile), [])
        self.assertEqual(state_path.read_bytes(), stamp)
        table = _read(self.table_path)
        table["roster"]["specialists"][WHO]["blurb"] = "老板手改"
        _write(self.table_path, table)
        events = check_drift(self.profile)
        self.assertEqual([(e["name"], e["kind"]) for e in events], [(WHO, "external")])
        self.assertEqual(current_version(self.profile, WHO), 3)
        self.assertEqual(check_drift(self.profile), [])

    def test_add_worker_versions_generalist_scope_change(self) -> None:
        (self.workspace / "backoffice" / "extra.py").write_text(
            '"""Extra helper."""\n', encoding="utf-8"
        )
        add_worker(self.workspace, "extra-hand", "backoffice/extra.py",
                   profiles_root=self.profiles)
        state = load_state(self.profile, "generalist")
        self.assertEqual(state["version"], 2)
        self.assertIn("加人 extra-hand", state["log"][-1]["detail"])
        self.assertIsNone(load_state(self.profile, "extra-hand"))
        rollback(self.profile, "generalist", 1)
        files = _read(self.table_path)["roster"]["specialists"]["generalist"]["files"]
        self.assertNotIn("backoffice/extra.py", files)

    def test_remove_logs_then_archives_and_rollback_refuses_revival(self) -> None:
        self.move_billing_to_orders()
        result = remove_worker(self.workspace, WHO, profiles_root=self.profiles)
        archived = self.profile / result["archived_to"]
        state = _read(archived / "VERSION.json")
        self.assertEqual(state["log"][-1]["detail"], f"移除 {WHO}")
        with self.assertRaises(VersionError) as caught:
            rollback(self.profile, WHO, 1)
        self.assertIn("/team add", str(caught.exception))

    def test_rebuild_without_change_writes_nothing(self) -> None:
        build_profile(self.workspace, self.profiles, default_profile_name(self.workspace))
        self.assertFalse(any((self.profile / "agents").glob("*/VERSION.json")))

    def test_team_command_versions_and_rollback(self) -> None:
        from core import tui
        from core.autoprofile import state_root_for

        # /team 走默认位置（state_root_for(workspace)/profiles），与 /team add 同一解析。
        default_profile = state_root_for(self.workspace) / "profiles" / self.profile.name
        default_profile.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.profile, default_profile)
        self.profile = default_profile
        self.table_path = default_profile / "dispatch_table.json"
        self.move_billing_to_orders()
        logged: list[tuple[str, str | None]] = []

        class Probe:
            def _log(self, text: str, tone: str | None = None) -> None:
                logged.append((text, tone))

        tui.CifixChatApp._team_versions(Probe(), self.workspace, "versions", ["versions", WHO])
        self.assertIn(f"{WHO} 当前 v2", logged[-1][0])
        tui.CifixChatApp._team_versions(Probe(), self.workspace, "rollback", ["rollback", WHO, "1"])
        self.assertIn("从 v2 回滚到 v1", logged[-1][0])
        self.assertIsNone(logged[-1][1])
        self.assertEqual(self.dispatch(), {WHO: [BILLING_NODE]})
        tui.CifixChatApp._team_versions(Probe(), self.workspace, "rollback", ["rollback", WHO, "9"])
        self.assertEqual(logged[-1][1], "error")


if __name__ == "__main__":
    unittest.main()
