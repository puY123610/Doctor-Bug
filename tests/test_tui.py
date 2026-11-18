# -*- coding: utf-8 -*-
"""TUI2b：OpenCode 同构双页 Textual 交互页的离线测试。

全程不实例化任何真实 provider 模型、不读密钥、不联网；调度员路径用
``FakeChatModel`` 的测试子类 ``ScriptedToolModel``（WIRE3 先例）脚本化一次
``run_repair`` 工具调用。Pilot 断言覆盖 Home/Session 双页、自绘输入框、
状态行后端轮换、命令面板与既有斜杠命令回归；另按 TUI2b 规格校验
「命令面板条目逐条等于后端命令清单」与「界面不得出现 OpenCode 独有功能」。
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentscope.message import Msg, TextBlock, ToolCallBlock
from agentscope.model import ChatResponse, ChatUsage, FinishedReason
from rich.cells import cell_len
from rich.console import Console
from textual.containers import VerticalScroll
from textual.widgets import OptionList, RichLog, Static

from agentscope_ext.fake_model import (
    ChunkedFakeChatModel,
    FakeChatModel,
    ThinkingChunkedFakeChatModel,
)
from core import dispatcher, model_config
from core.dispatcher import build_dispatcher
from core.model_config import ModelConfig, load_local_env
from core.runner import run_arm
from core.tui import (
    COLOR_ACCENT,
    COLOR_BACKGROUND_BASE,
    COLOR_BACKGROUND_PANEL,
    COLOR_BACKGROUND_RAISED,
    COLOR_BACKGROUND_USER,
    COLOR_ERROR,
    COLOR_SUCCESS,
    COLOR_TEXT_BASE,
    COLOR_TEXT_MUTED,
    COMMANDS,
    FOOTER_HINT,
    IMAGE_HINT,
    INTERRUPT_MARKER,
    LOGO_LINES,
    LOGO_TEXT,
    HINT_IDLE,
    PLACEHOLDER_EXAMPLES,
    SLASH_COMMANDS,
    TITLE_LIMIT,
    VERSION,
    CifixChatApp,
    FooterLine,
    ModelPicker,
    PromptBase,
    PromptBox,
    PromptInput,
    SessionHeader,
    _completion_candidates,
    _image_paths_in_message,
    _message_has_image_path,
)

PLATFORM_ROOT = Path(__file__).resolve().parents[1]
DEMO_REPO = PLATFORM_ROOT / "tests" / "fixtures" / "demo_repo"

# 斜杠命令清单。面板、补全与 /help 必须与 COMMANDS 逐条一致。
# WS1 起包含 /cd 与 /workspace。
BACKEND_COMMANDS = (
    "/help",
    "/check",
    "/run",
    "/accept",
    "/profile",
    "/repro",
    "/model",
    "/cd",
    "/workspace",
    "/team",
    "/brief",
    "/permissions",
    "/mode",
    "/pending",
    "/runs",
    "/diff",  # TUIC2
    "/copy",  # COPY1
    "/sessions",  # F1
    "/save",  # F2
    "/market",
    "/status",  # TUIE1
    "/mcp",  # TUIE1
    "/quit",
)

_RESPONSE_TIME = "1970-01-01T00:00:00.000000"

# 编排者无头探针同款回复：多段中文 + 编号列表 + 粗体。
# 修复前 assistant 分支不传 width、按纯文本 80 列折行，会把「4.」折成孤行
# 并把 ** 原样显示；修复后走 Markdown + 日志实际宽度。
LONG_MARKDOWN_REPLY = (
    "好的，我只用本地工具就能回答这个问题，先给你一个结论，再说明依据。\n"
    "\n"
    "**结论：** 这个仓库最该先修的是验收用例里那批跟账单计算相关的失败，"
    "它们彼此独立，修完就能显著降低失败数。\n"
    "\n"
    "**建议的修复顺序：**\n"
    "1. billing-specialist 先处理折扣与税额的取整边界；\n"
    "2. orders-specialist 再处理优惠券叠加后的应付金额；\n"
    "3. report-specialist 最后对齐汇总报表里的字段名；\n"
    "4. 我负责在每轮修完后重跑冻结验收并把结果贴回来。\n"
    "\n"
    "如果你同意这个顺序，我可以现在就开始第一轮，预计三轮内把失败数清零。"
)

# 只含序号标点的孤行（如单独成行的「4.」「4、」「4」）。
_ORPHAN_ORDINAL = re.compile(r"^\d+[.、]?$")


def _text_response(index: int, text: str) -> ChatResponse:
    return ChatResponse(
        content=[
            TextBlock(
                text=text,
                id=f"tui2-text-{index:04d}",
                created_at=_RESPONSE_TIME,
                finished_at=_RESPONSE_TIME,
            )
        ],
        is_last=True,
        id=f"tui2-response-{index:04d}",
        created_at=_RESPONSE_TIME,
        usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.0),
        finished_reason=FinishedReason.COMPLETED,
    )


class ScriptedToolModel(FakeChatModel):
    """WIRE3 先例：按脚本发 AgentScope 工具调用，再发最终文本。"""

    def __init__(self, steps: list[dict]) -> None:
        super().__init__(fallback_reply="script exhausted")
        self.steps = list(steps)
        self.seen_messages: list[list] = []

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list[dict] | None = None,
        tool_choice: object = None,
        **kwargs: object,
    ) -> ChatResponse:
        index = len(self.seen_messages)
        self.seen_messages.append(list(messages))
        if not self.steps:
            return _text_response(index, "done")

        step = self.steps.pop(0)
        if step["type"] == "tool":
            return ChatResponse(
                content=[
                    ToolCallBlock(
                        id=f"tui2-tool-{index:04d}",
                        name=step["name"],
                        input=json.dumps(step["input"], ensure_ascii=False),
                    )
                ],
                is_last=True,
                id=f"tui2-response-{index:04d}",
                created_at=_RESPONSE_TIME,
                usage=ChatUsage(input_tokens=1, output_tokens=1, time=0.0),
                finished_reason=FinishedReason.COMPLETED,
            )
        return _text_response(index, step["text"])


class LoopBoundModel(FakeChatModel):
    """仿真实 httpx 异步客户端：模型绑定首次调用时的事件循环。

    真实 ``DeepSeekChatModel`` 的 HTTP 客户端在首次请求时绑定当前事件循环；
    循环关闭后复用该客户端会抛 ``RuntimeError('Event loop is closed')``。
    本替身在每次 ``_call_api`` 时复刻这一约束：首次调用记录运行中的循环，
    之后若该循环已关闭、或当前运行循环不是同一个，立即抛同样的错误。
    """

    def __init__(self, fallback_reply: str) -> None:
        super().__init__(fallback_reply=fallback_reply)
        self.bound_loop: asyncio.AbstractEventLoop | None = None
        self.call_loops: list[asyncio.AbstractEventLoop] = []

    async def _call_api(
        self,
        model_name: str,
        messages: list,
        tools: list[dict] | None = None,
        tool_choice: object = None,
        **kwargs: object,
    ) -> ChatResponse:
        loop = asyncio.get_running_loop()
        if self.bound_loop is None:
            self.bound_loop = loop
        elif self.bound_loop.is_closed() or loop is not self.bound_loop:
            raise RuntimeError("Event loop is closed")
        self.call_loops.append(loop)
        return await super()._call_api(
            model_name,
            messages,
            tools,
            tool_choice,
            **kwargs,
        )


def _tool_result_texts(messages: list) -> list[str]:
    texts: list[str] = []
    for message in messages:
        for block in message.get_content_blocks():
            if type(block).__name__ != "ToolResultBlock":
                continue
            for output in getattr(block, "output", []) or []:
                text = getattr(output, "text", "")
                if text:
                    texts.append(text)
    return texts


def _log_text(app: CifixChatApp) -> str:
    log = app.screen.query_one("#log", RichLog)
    return "\n".join(strip.text for strip in log.lines)


def _status_text(app: CifixChatApp) -> str:
    return str(app.screen.query_one(".prompt-status", Static).content)


def _placeholder(app: CifixChatApp) -> str:
    return str(app.screen.query_one(PromptInput).placeholder)


def _screen_text(app: CifixChatApp) -> str:
    """从 Textual SVG 截图提取整屏可见文本。

    ``export_screenshot`` 返回的 SVG 里每个文本段都是一个 ``<text>``
    节点；还原其文本即可对整屏做计数断言，不依赖具体配色或坐标。
    """
    svg = app.export_screenshot()
    segments = re.findall(r"<text[^>]*>(.*?)</text>", svg, re.S)
    return html.unescape("\n".join(segments)).replace("\xa0", " ")


# DRBUG 五字母的参考块字：字形顺序即拼写 D、R、B、U、G，列宽 9/8/8/9/9。
# 这份参考独立于 core.tui 的实现常量，用来抓「少画一个字形」这类拼写错误。
# NAME1：原参考为 BUGDOC 六字母（B/U/G/D/O/C 列宽 8/9/9/9/9/9），随对外改名换成 DRBUG。
_REFERENCE_DRBUG = (
    ("D", ("██████╗  ", "██╔══██╗ ", "██║  ██║ ", "╚██████╔╝")),
    ("R", ("██████╗ ", "██╔══██╗", "██████╔╝", "██║  ██║")),
    ("B", ("██████╗ ", "██╔══██╗", "██████╔╝", "╚█████╔╝")),
    ("U", ("██╗   ██╗", "██║   ██║", "██║   ██║", "╚██████╔╝")),
    ("G", (" ██████╗ ", "██╔════╝ ", "██║  ███╗", "╚██████╔╝")),
)


class LogoSpellingTest(unittest.TestCase):
    def test_logo_spells_drbug_glyph_by_glyph(self) -> None:
        """按字形宽度切分 4 行块字，逐字比对 D、R、B、U、G。"""
        letters = "".join(letter for letter, _ in _REFERENCE_DRBUG)
        self.assertEqual(letters, "DRBUG")
        widths = [cell_len(glyph[0]) for _, glyph in _REFERENCE_DRBUG]
        self.assertEqual(widths, [9, 8, 8, 9, 9])

        self.assertEqual(len(LOGO_LINES), 4)
        for row_index, line in enumerate(LOGO_LINES):
            self.assertTrue(
                all(cell_len(char) == 1 for char in line),
                "块字行必须全部是单列字符，才能按列宽切分",
            )
            offset = 0
            for glyph_index, (letter, glyph) in enumerate(_REFERENCE_DRBUG):
                width = widths[glyph_index]
                chunk = line[offset : offset + width]
                self.assertEqual(
                    chunk,
                    glyph[row_index],
                    f"第 {row_index + 1} 行第 {glyph_index + 1} 个字"
                    f"应为 {letter}，实际切出 {chunk!r}",
                )
                offset += width
                if glyph_index < len(_REFERENCE_DRBUG) - 1:
                    self.assertEqual(
                        line[offset : offset + 2],
                        "  ",
                        f"第 {row_index + 1} 行 {letter} 后应有双空格间隔",
                    )
                    offset += 2
            self.assertEqual(
                offset, len(line), f"第 {row_index + 1} 行有多余字形"
            )
            expected = "  ".join(
                glyph[row_index] for _, glyph in _REFERENCE_DRBUG
            )
            self.assertEqual(line, expected)
        self.assertEqual(LOGO_TEXT.plain, "\n".join(LOGO_LINES))


class LoadLocalEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="tui2-env-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_loads_new_keys_without_overwriting_or_printing(self) -> None:
        env_file = self.tmp / ".env.local"
        env_file.write_text(
            "# comment\n"
            "TUI2_ALPHA=alpha-value\n"
            "export TUI2_BETA='beta=with-equals'\n"
            'TUI2_GAMMA="quoted value"\n'
            "TUI2_KEEP=from-file\n"
            "not a pair line\n",
            encoding="utf-8",
        )

        buffer = io.StringIO()
        with mock.patch.dict(
            os.environ, {"TUI2_KEEP": "from-environ"}, clear=True
        ):
            with contextlib.redirect_stdout(buffer):
                load_local_env(env_file)

            self.assertEqual(os.environ["TUI2_ALPHA"], "alpha-value")
            self.assertEqual(os.environ["TUI2_BETA"], "beta=with-equals")
            self.assertEqual(os.environ["TUI2_GAMMA"], "quoted value")
            self.assertEqual(os.environ["TUI2_KEEP"], "from-environ")

        self.assertEqual(buffer.getvalue(), "")

    def test_missing_file_is_a_silent_noop(self) -> None:
        buffer = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True):
            with contextlib.redirect_stdout(buffer):
                load_local_env(self.tmp / "does-not-exist.env")
            self.assertEqual(dict(os.environ), {})
        self.assertEqual(buffer.getvalue(), "")


class DispatcherToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="tui2-tools-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def test_paid_gate_rejects_non_scripted_without_confirm(self) -> None:
        with mock.patch.object(dispatcher, "RUNS_DIR", self.tmp):
            result = dispatcher.run_repair(
                workspace=str(DEMO_REPO),
                arm="C",
                actors="agentscope",
                confirm_paid=False,
            )

        self.assertIn("确认", result)
        self.assertIn("confirm_paid=True", result)
        self.assertIn("API 额度", result)
        # 没有产生任何运行记录或工作副本。
        self.assertEqual(list(self.tmp.rglob("*")), [])

    def test_scripted_run_repair_writes_pass_record(self) -> None:
        out = self.tmp / "record.json"
        events: list[dict] = []
        dispatcher.set_event_sink(events.append)
        self.addCleanup(dispatcher.set_event_sink, None)

        with mock.patch.object(dispatcher, "RUNS_DIR", self.tmp):
            result = dispatcher.run_repair(
                arm="C",
                actors="scripted",
                workspace=str(DEMO_REPO),
                max_rounds=3,
                out=str(out),
            )

        self.assertIn("outcome=PASS", result)
        self.assertIn("记录路径=", result)
        self.assertTrue(out.is_file())
        record = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(record["outcome"], "PASS")
        self.assertEqual(record["final_failed"], [])
        self.assertEqual(events[0]["event"], "tool_call")
        self.assertEqual(events[-1]["event"], "run_finished")
        self.assertEqual(events[-1]["outcome"], "PASS")

    def test_dispatcher_prompt_can_carry_tui_default_backend(self) -> None:
        agent = build_dispatcher(
            model=FakeChatModel(fallback_reply="x"),
            default_actors="llm",
        )
        self.assertIn("默认执行后端 llm", agent._system_prompt)
        dispatcher.set_dispatcher_default_actors(agent, "agentscope")
        self.assertIn("默认执行后端 agentscope", agent._system_prompt)
        with self.assertRaises(ValueError):
            dispatcher.set_dispatcher_default_actors(agent, "nope")

    def test_show_run_summarizes_rounds_and_assignments(self) -> None:
        record = run_arm(
            "C",
            DEMO_REPO,
            work_root=self.tmp / "work",
            vault_dir=DEMO_REPO.parent / "acceptor_vault",
            kb_path=PLATFORM_ROOT / "kb" / "dispatch_table.json",
        )
        path = self.tmp / "record.json"
        path.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False),
            encoding="utf-8",
        )

        result = dispatcher.show_run(str(path))

        self.assertIn("== 运行记录摘要 ==", result)
        self.assertIn("结果: PASS", result)
        self.assertIn("第 1 轮验收", result)
        self.assertIn("派单:", result)
        self.assertIn("billing-specialist", result)

    def test_kb_explain_names_specialist_and_rule(self) -> None:
        dispatcher._bind_default_workspace(str(DEMO_REPO))
        self.addCleanup(dispatcher._bind_default_workspace, None)
        result = dispatcher.kb_explain(
            "tests/test_orders.py::test_checkout_total_with_coupon, "
            "tests/test_billing.py::test_tax_on_discounted_total"
        )

        self.assertIn("billing-specialist", result)
        self.assertIn("节点覆盖规则", result)
        self.assertIn("模块默认规则", result)
        self.assertIn(
            "tests/test_orders.py::test_checkout_total_with_coupon", result
        )

    def test_accept_check_reports_frozen_verdict(self) -> None:
        result = dispatcher.accept_check(str(DEMO_REPO))

        self.assertIn("== 冻结验收 ==", result)
        self.assertIn("验收结论:", result)
        self.assertIn("通过/失败计数:", result)
        self.assertIn("失败用例", result)

    def test_platform_check_reports_key_status_without_secret(self) -> None:
        sentinel = "sk-tui2-offline-SECRET-do-not-leak"
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": sentinel}):
            result = dispatcher.platform_check()

        self.assertIn("体检结论", result)
        self.assertIn("密钥状态", result)
        self.assertIn("已设置", result)
        self.assertNotIn(sentinel, result)


class DispatcherPromptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher._bind_default_workspace, None)

    def test_prompt_binds_workspace_and_smalltalk_rule(self) -> None:
        """提示词锁定：绑定工作区 + 闲聊不调工具 + 既有诚实/付费条款。"""
        agent = build_dispatcher(
            model=FakeChatModel(fallback_reply="x"),
            workspace="/tmp/drbug-bound-project",
        )
        prompt = agent._system_prompt
        self.assertIn("当前绑定工作区: /tmp/drbug-bound-project", prompt)
        self.assertIn("你启动我的这个目录就是", prompt)
        self.assertIn("不调用任何工具", prompt)
        self.assertIn("打招呼", prompt)
        self.assertIn("一般闲聊", prompt)
        self.assertIn("只有用户明确要求执行、检查、修复、查询", prompt)
        self.assertIn("查不到就明说不知道", prompt)
        self.assertIn("禁止编造", prompt)
        self.assertIn("confirm_paid=True", prompt)


class WorkspaceBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="tui2-bind-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name).resolve()
        (self.project / "app.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        # WS1：没有项目特征的目录进入待命态。这个夹具要测「项目目录启动即绑定」，
        # 所以放一个 pyproject.toml。
        (self.project / "pyproject.toml").write_text(
            "[project]\nname = \"bind-fixture\"\n",
            encoding="utf-8",
        )
        self.addCleanup(dispatcher._bind_default_workspace, None)
        self.addCleanup(dispatcher.set_event_sink, None)

    def _agent_for(self, root: Path) -> tuple[CifixChatApp, object]:
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="x"), workspace_root=root
        )
        return app, app._ensure_dispatcher()

    def test_user_project_becomes_default_tool_workspace(self) -> None:
        _, agent = self._agent_for(self.project)
        prompt = agent._system_prompt
        self.assertIn(f"当前绑定工作区: {self.project}", prompt)
        self.assertIn("你启动我的这个目录就是", prompt)

        events: list[dict] = []
        dispatcher.set_event_sink(events.append)
        result = dispatcher.accept_check()

        self.assertIn("尚无冻结档案", result)
        self.assertNotIn("workspace_unbound", result)
        self.assertEqual(events[-1]["summary"], f"workspace={self.project}")

    def test_platform_root_binds_itself(self) -> None:
        _, agent = self._agent_for(PLATFORM_ROOT)
        self.assertIn(
            f"当前绑定工作区: {PLATFORM_ROOT}", agent._system_prompt
        )

        events: list[dict] = []
        dispatcher.set_event_sink(events.append)
        result = dispatcher.accept_check()

        self.assertIn("尚无冻结档案", result)
        self.assertNotIn("demo_repo", result)
        self.assertEqual(events[-1]["summary"], f"workspace={PLATFORM_ROOT}")

class DispatcherAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)

    def test_tool_call_executes_run_repair_and_returns_pass_summary(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="tui2-agent-")
        self.addCleanup(temporary.cleanup)
        tmp = Path(temporary.name)
        out = tmp / "record.json"

        model = ScriptedToolModel(
            [
                {
                    "type": "tool",
                    "name": "run_repair",
                    "input": {
                        "arm": "C",
                        "actors": "scripted",
                        "workspace": str(DEMO_REPO),
                        "max_rounds": 3,
                        "out": str(out),
                    },
                },
                {"type": "text", "text": "修复完成，验收 PASS。"},
            ]
        )
        events: list[dict] = []
        with mock.patch.object(dispatcher, "RUNS_DIR", tmp):
            agent = build_dispatcher(model=model, event_sink=events.append)
            reply = asyncio.run(
                agent.reply(
                    Msg(
                        name="用户",
                        role="user",
                        content=[TextBlock(text="请修复 demo_repo")],
                    )
                )
            )

        self.assertEqual(agent.name, "调度员")
        self.assertEqual(reply.get_text_content(), "修复完成，验收 PASS。")

        # 模型确实看到了全部平台工具。
        schemas = asyncio.run(agent.toolkit.get_tool_schemas())
        offered = [tool["function"]["name"] for tool in schemas]
        self.assertEqual(
            offered,
            [
                "platform_check",
                "run_repair",
                "propose_repro",
                "freeze_repro",
                "freeze_behavior_check",
                "accept_check",
                "make_brief",
                "show_brief",
                "show_run",
                "kb_explain",
                "show_agent_archive",
                "list_roster",
                "add_worker",
                "remove_worker",
            ],
        )

        # 工具真的执行了：运行记录 PASS，且工具结果（含 PASS）回灌给模型。
        self.assertTrue(out.is_file())
        record = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(record["outcome"], "PASS")
        result_texts = [
            text
            for messages in model.seen_messages
            for text in _tool_result_texts(messages)
        ]
        self.assertTrue(
            any("PASS" in text for text in result_texts), result_texts
        )
        self.assertEqual(events[0]["event"], "tool_call")
        self.assertEqual(events[-1]["event"], "run_finished")
        self.assertEqual(events[-1]["outcome"], "PASS")


class StatusLineConfigTest(unittest.TestCase):
    def test_status_line_shows_model_and_provider_from_config(self) -> None:
        app = CifixChatApp(model=None)
        cfg = ModelConfig(provider="deepseek", model="deepseek-chat")
        with mock.patch(
            "core.model_config.load_config", return_value=cfg
        ):
            line = app.status_line().plain

        self.assertEqual(
            line,
            "主治医生 · deepseek-chat DeepSeek · 思考 默认"
            f" · 工作区：{PLATFORM_ROOT} · 模式：逐次确认",
        )

    def test_status_line_degrades_without_config(self) -> None:
        app = CifixChatApp(model=None)
        with mock.patch(
            "core.model_config.load_config",
            side_effect=ValueError("model is not configured"),
        ):
            line = app.status_line().plain

        self.assertIn("未配置", line)
        self.assertNotIn("后端", line)


class TuiEntryTest(unittest.TestCase):
    def test_run_loads_local_env_before_starting_app(self) -> None:
        from core import tui as tui_module

        with mock.patch("core.model_config.load_local_env") as env_mock:
            with mock.patch.object(
                tui_module.CifixChatApp, "run", autospec=True
            ) as run_mock:
                code = tui_module.run(
                    workspace_root=PLATFORM_ROOT,
                    model=FakeChatModel(fallback_reply="x"),
                )

        self.assertEqual(code, 0)
        env_mock.assert_called_once_with()
        self.assertEqual(run_mock.call_count, 1)

    def test_cli_bare_invocation_wires_to_tui_and_chat_is_rejected(self) -> None:
        from core import cli
        from core import tui as tui_module

        with mock.patch.object(
            tui_module, "run", return_value=0
        ) as run_mock, mock.patch(
            "core.cli._stdout_is_tty", return_value=True
        ):
            code = cli.main([])

        self.assertEqual(code, 0)
        run_mock.assert_called_once_with(
            workspace_root=Path.cwd().resolve(), perm_mode="ask"
        )

        with mock.patch.object(tui_module, "run", return_value=0) as run_mock:
            code = cli.main(["chat"])

        self.assertEqual(code, 2)
        run_mock.assert_not_called()


class CifixChatAppTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)

    def _app(self) -> CifixChatApp:
        return CifixChatApp(
            model=FakeChatModel(fallback_reply="我是调度员替身，随时待命"),
            workspace_root=PLATFORM_ROOT,
        )

    async def _submit(self, app, pilot, text: str) -> None:
        prompt = app.screen.query_one(PromptInput)
        prompt.focus()
        prompt.text = text
        await pilot.pause()
        # SLASH1：浮层开着时第一下 enter 只填入、不提交；真实按键再按一次才提交。
        if prompt.box is not None and prompt.box.suggestions_visible:
            await pilot.press("enter")
            await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    def _assert_compact_prompt(self, box: PromptBox) -> None:
        """空态输入框总高 5 行：上内边距 1 + 输入 1 + 空 1 + 状态 1 + 底座 1。"""
        self.assertEqual(box.size.height, 5)
        panel = box.query_one(".prompt-panel")
        self.assertEqual(panel.outer_size.height, 4)
        self.assertEqual(box.query_one(".prompt-input").size.height, 1)
        self.assertEqual(box.query_one(".prompt-gap").size.height, 1)
        self.assertEqual(box.query_one(".prompt-status").size.height, 1)
        self.assertEqual(box.query_one(PromptBase).size.height, 1)

    async def test_home_startup_matches_opencode_layout(self) -> None:
        app = self._app()
        # HINT1：首页示例句是「空闲」状态的提示；固定在空闲再验首页布局。
        app.hint_state = lambda: HINT_IDLE  # type: ignore[method-assign]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            self.assertEqual(app.page, "home")
            self.assertEqual(app.screen.id, "home")
            self.assertEqual(
                app.screen.styles.background.hex, COLOR_BACKGROUND_BASE
            )

            # 4 行双色块字：左半 muted 不加粗，右半亮白加粗，阴影字符混色。
            logo = app.screen.query_one("#home-logo", Static).content
            self.assertEqual(str(logo), LOGO_TEXT.plain)
            self.assertEqual(len(LOGO_TEXT.plain.splitlines()), 4)
            self.assertEqual(len(LOGO_LINES), 4)
            style_texts = [str(span.style) for span in LOGO_TEXT.spans]
            self.assertEqual(style_texts[0], COLOR_TEXT_MUTED)
            self.assertIn(f"bold {COLOR_TEXT_BASE}", style_texts)
            self.assertTrue(
                any(
                    style not in (COLOR_TEXT_MUTED, f"bold {COLOR_TEXT_BASE}")
                    for style in style_texts
                ),
                "阴影字符应有独立混色",
            )

            placeholder = _placeholder(app)
            self.assertTrue(placeholder.startswith("问点什么，或让我办事…"))
            self.assertNotIn("Ask anything", placeholder)
            self.assertTrue(
                any(example in placeholder for example in PLACEHOLDER_EXAMPLES)
            )

            status = _status_text(app)
            self.assertIn("主治医生", status)
            self.assertIn("fake-chat-model", status)
            self.assertIn("DeepSeek · 思考 默认", status)
            self.assertNotIn("后端", status)

            screen = _screen_text(app)
            self.assertNotIn("Ask anything", screen)
            self.assertNotIn("shift+tab agents", screen)
            self.assertNotIn("ctrl+p commands", screen)
            self.assertIn("ctrl+p 命令面板", screen)
            self.assertNotIn("shift+tab", screen)
            self.assertIn(str(Path.cwd()), screen)
            self.assertIn(VERSION, screen)

            prompt = app.screen.query_one("#home-prompt", PromptBox)
            self.assertEqual(prompt.size.width, 75)
            self.assertEqual(app.screen.query_one(FooterLine).cwd, str(Path.cwd()))

            # 内容垂直居中：卡片 y = (内容区高 − 卡片高) // 2。
            body = app.screen.query_one("#home-body")
            card = app.screen.query_one("#home-card")
            self.assertEqual(
                card.region.y, (body.size.height - card.size.height) // 2
            )
            # 框内自上而下：输入区（空态 1 行）→ 空一行 → 状态行。
            self.assertEqual(
                app.screen.query_one(".prompt-input").size.height, 1
            )
            self.assertEqual(
                app.screen.query_one(".prompt-gap").size.height, 1
            )
            self.assertEqual(
                app.screen.query_one(".prompt-status").size.height, 1
            )
            # 框体总高 = 上内边距 1 + 输入 1 + 空 1 + 状态 1 + 底座 1。
            self.assertEqual(prompt.size.height, 5)

            # 版本号：pyproject 实值 + 右下角右对齐。
            version = app.screen.query_one("#version", Static)
            self.assertEqual(str(version.content), VERSION)
            self.assertEqual(str(version.styles.text_align), "right")

    async def test_home_prompt_width_narrows_with_screen(self) -> None:
        """Home 框宽 = min(屏宽−4, 75)：70 列屏 → 66 列。"""
        app = self._app()
        async with app.run_test(size=(70, 30)) as pilot:
            await pilot.pause()
            prompt = app.screen.query_one("#home-prompt", PromptBox)
            self.assertEqual(prompt.size.width, 66)

            # 窄屏页脚先收左端目录，右侧键位提示必须完整保留。
            footer = app.screen.query_one(FooterLine)
            line = footer.render().plain
            self.assertIn("ctrl+p 命令面板", line)
            self.assertNotIn("shift+tab", line)
            self.assertNotIn("agents", line)
            self.assertNotIn("commands", line)
            self.assertLessEqual(cell_len(line), footer.size.width)

    async def test_prompt_box_has_only_left_border_and_thick_base(self) -> None:
        """自绘输入框：只有左边框 ┃、内边距 1/2、框下 ╹+▀ 底座。"""
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            panel = app.screen.query_one(".prompt-panel")
            self.assertEqual(panel.styles.border_left[0], "heavy")
            for side in ("border_top", "border_right", "border_bottom"):
                self.assertEqual(getattr(panel.styles, side)[0], "")
            padding = panel.styles.padding
            self.assertEqual(
                (padding.top, padding.right, padding.bottom, padding.left),
                (1, 2, 0, 2),
            )
            self.assertEqual(panel.styles.background.hex, COLOR_BACKGROUND_PANEL)
            self.assertEqual(panel.outer_size.height, 4)
            prompt = app.screen.query_one("#home-prompt", PromptBox)
            self.assertEqual(prompt.size.height, 5)

            base_widget = app.screen.query_one(PromptBase)
            base = base_widget.render()
            self.assertTrue(base.plain.startswith("╹"))
            self.assertEqual(base.plain[1:], "▀" * (base_widget.size.width - 1))

    async def test_prompt_box_is_compact_in_home_and_session(self) -> None:
        """空态/短输入时 Home 与会话页输入框都收紧到 5 行。"""
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            self._assert_compact_prompt(
                app.screen.query_one("#home-prompt", PromptBox)
            )
            # Home 同样随行数长高，清空后收回。
            home_prompt = app.screen.query_one(PromptInput)
            home_prompt.text = "一\n二\n三"
            await pilot.pause()
            self.assertEqual(home_prompt.size.height, 3)
            self.assertEqual(
                app.screen.query_one("#home-prompt", PromptBox).size.height, 7
            )
            home_prompt.text = ""
            await pilot.pause()
            self.assertEqual(home_prompt.size.height, 1)
            self._assert_compact_prompt(
                app.screen.query_one("#home-prompt", PromptBox)
            )
            # Home 卡片高 = logo 4 + 空 1 + 输入框 5。
            self.assertEqual(app.screen.query_one("#home-card").size.height, 10)

            await self._submit(app, pilot, "短消息")
            await app.workers.wait_for_complete()
            await pilot.pause()

            self._assert_compact_prompt(
                app.screen.query_one("#session-prompt", PromptBox)
            )

            # 输入随逻辑行数长高：1 行 → 1；4 行 → 4；上限 6 行；清空收回。
            prompt = app.screen.query_one(PromptInput)
            prompt.text = "第一行\n第二行\n第三行\n第四行"
            await pilot.pause()
            self.assertEqual(prompt.size.height, 4)
            self.assertEqual(prompt.document.line_count, 4)
            prompt.text = "\n".join(f"第{index}行" for index in range(9))
            await pilot.pause()
            self.assertEqual(prompt.document.line_count, 9)
            self.assertEqual(prompt.size.height, 6)
            prompt.text = ""
            await pilot.pause()
            self.assertEqual(prompt.size.height, 1)
            self._assert_compact_prompt(
                app.screen.query_one("#session-prompt", PromptBox)
            )

    async def test_first_message_switches_to_session_full_width(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, "你好，介绍一下自己")
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual(app.page, "session")
            self.assertEqual(app.screen.id, "session")
            prompt = app.screen.query_one("#session-prompt", PromptBox)
            self.assertEqual(prompt.size.width, 116)

            text = _log_text(app)
            self.assertIn("┃ 你好，介绍一下自己", text)
            # 修复后调度员标签独占首行，正文由 Markdown 渲染在下一行。
            self.assertIn("主治医生:", text)
            self.assertIn("我是调度员替身，随时待命", text)
            self.assertNotIn("后端", _status_text(app))
            # 会话页沿用 OpenCode 的 prompt.footer：目录 + 键位提示。
            session_footer = app.screen.query_one(
                "#session-footer", FooterLine
            )
            self.assertEqual(session_footer.cwd, str(Path.cwd()))
            footer_text = session_footer.render().plain
            self.assertIn("ctrl+p 命令面板", footer_text)
            self.assertNotIn("shift+tab", footer_text)

    async def test_backend_is_fixed_to_real_agentscope(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.actor_backend, "agentscope")
            self.assertNotIn("后端", _status_text(app))

            await pilot.press("shift+tab")
            await pilot.pause()
            self.assertEqual(app.actor_backend, "agentscope")

            await self._submit(app, pilot, "你好")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIn(
                "默认执行后端 agentscope", app._dispatcher._system_prompt
            )

            await self._submit(app, pilot, "/run --arm C")
            await pilot.pause()
            text = _log_text(app)
            self.assertIn("API 额度", text)
            self.assertIn("--yes", text)
            self.assertNotIn("开始修复", text)

    async def test_command_palette_filters_and_esc_closes(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+p")
            await pilot.pause()

            self.assertEqual(app.screen.id, "palette")
            card = app.screen.query_one("#palette-card")
            self.assertEqual(
                card.region.x,
                (app.screen.size.width - card.region.width) // 2,
            )
            self.assertEqual(
                card.region.y,
                (app.screen.size.height - card.region.height) // 2,
            )
            option_list = app.screen.query_one("#palette-list", OptionList)
            self.assertEqual(
                [str(option.id) for option in option_list.options],
                [command for command, _ in COMMANDS],
            )
            self.assertIn("/runs", [option.id for option in option_list.options])
            self.assertIn("/profile", [option.id for option in option_list.options])
            self.assertIn("/repro", [option.id for option in option_list.options])

            await pilot.press("r", "e", "p", "r", "o")
            await pilot.pause()
            self.assertEqual(
                [str(option.id) for option in option_list.options],
                ["/repro"],
            )

            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.screen.id, "home")
            self.assertEqual(app.page, "home")

            # 面板 enter 执行命令：过滤到 /help 后回车落到会话页。
            await pilot.press("ctrl+p")
            await pilot.pause()
            await pilot.press("h", "e", "l", "p")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            # TUIE2：/help 是弹窗，内容与 COMMANDS 同源。
            self.assertEqual(app.screen.id, "help-view")
            help_body = str(app.screen.query_one("#info-body", Static).render())
            self.assertIn("/check", help_body)
            self.assertIn("/accept", help_body)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.screen.id, "session")

    async def test_command_catalog_matches_backend_only(self) -> None:
        """面板/斜杠命令逐条等于命令清单（含 /cd 与 /workspace）。"""
        commands = tuple(command for command, _ in COMMANDS)
        self.assertEqual(len(commands), len(BACKEND_COMMANDS))
        self.assertEqual(set(commands), set(BACKEND_COMMANDS))
        self.assertEqual(len(commands), len(set(commands)))
        self.assertEqual(
            SLASH_COMMANDS, frozenset(BACKEND_COMMANDS) | {"/exit"}
        )

        # TUIE2：HELP_TEXT 退役；/help 弹窗正文逐条读 COMMANDS。
        from core.tui import help_view_text

        help_text = help_view_text()
        for command, description in COMMANDS:
            self.assertIn(f"{command}", help_text)
            self.assertIn(description, help_text)

        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+p")
            await pilot.pause()
            option_list = app.screen.query_one("#palette-list", OptionList)
            ids = [str(option.id) for option in option_list.options]
            self.assertEqual(tuple(ids), commands)
            self.assertEqual(set(ids), set(BACKEND_COMMANDS))

    async def test_ui_has_no_opencode_only_features(self) -> None:
        """界面全文不得出现本项目后端没有的 OpenCode 独有功能。"""
        app = self._app()
        chunks: list[str] = []
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            chunks.append(_screen_text(app))
            chunks.append(str(app.screen.query_one("#home-logo", Static).content))

            await pilot.press("ctrl+p")
            await pilot.pause()
            chunks.append(_screen_text(app))
            await pilot.press("escape")
            await pilot.pause()

            await self._submit(app, pilot, "你好")
            await app.workers.wait_for_complete()
            await pilot.pause()
            chunks.append(_screen_text(app))
            chunks.append(_status_text(app))
            chunks.append(
                app.screen.query_one("#session-footer", FooterLine).render().plain
            )

            await self._submit(app, pilot, "/help")
            await pilot.pause()
            await pilot.pause()
            # TUIE2：/help 改为弹窗；弹窗正文也纳入禁用词检查，然后关掉。
            chunks.append(
                str(app.screen.query_one("#info-body", Static).render())
            )
            await pilot.press("escape")
            await pilot.pause()
            chunks.append(_log_text(app))
            await self._submit(app, pilot, "/model")
            await pilot.pause()
            chunks.append(_screen_text(app))
            await pilot.press("escape")
            await pilot.pause()
            chunks.append(_log_text(app))

        joined = "\n".join(chunks)
        # TUIE1：/mcp 是本单按单子新增的命令（后端 MCP1 已有外接工具），
        # 命令名 "mcp" 会出现在面板与 /help 里，故移出禁用词；大写 "MCP"
        # 与其余 OpenCode 独有功能仍禁用。
        # TUIC1：会话页侧边栏已按单子实现（同 TUIE1 /mcp 之例），
        # 「侧边栏」移出禁用词。
        # TUIC2：/diff 补丁块已按单子实现，「diff」同例移出禁用词。
        # F2：/sessions 会话列表已实现（F1 + F2 存档入口），「会话列表」
        # 同例移出禁用词。
        for banned in (
            "MCP",
            "插件",
            "主题",
            "会话历史",
            "历史会话",
            "外部编辑器",
            "导出会话",
            "更新提示",
            "@",
            "Ask anything",
            "shift+tab agents",
            "ctrl+p commands",
        ):
            self.assertNotIn(banned, joined)

    async def test_platform_events_are_compact_colored_single_lines(self) -> None:
        """工具/运行事件为单行圆点：成功绿、失败红。"""

        class CaptureLog:
            def __init__(self) -> None:
                self.written: list = []

            def write(self, content, width=None) -> None:
                self.written.append(content)

        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            app._on_platform_event(
                {
                    "event": "verdict",
                    "round": 1,
                    "status": "PASS",
                    "failed_count": 0,
                }
            )
            passed = app.history[-1]
            capture = CaptureLog()
            app.render_record(capture, passed)
            line = capture.written[0]
            self.assertTrue(line.plain.startswith("● "))
            self.assertNotIn("\n", line.plain)
            self.assertTrue(
                any(COLOR_SUCCESS in str(span.style) for span in line.spans)
            )

            app._on_platform_event(
                {
                    "event": "verdict",
                    "round": 2,
                    "status": "FAIL",
                    "failed_count": 3,
                }
            )
            failed = app.history[-1]
            capture = CaptureLog()
            app.render_record(capture, failed)
            line = capture.written[0]
            self.assertTrue(line.plain.startswith("● "))
            self.assertNotIn("\n", line.plain)
            self.assertTrue(
                any(COLOR_ERROR in str(span.style) for span in line.spans)
            )

    async def test_user_message_uses_left_bar_and_full_width_block(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            width = 80
            rendered = app._render_user("你好\n第二行", width)
            self.assertTrue(rendered.plain.startswith("┃"))
            console = Console(width=width, legacy_windows=False)
            parts = rendered.split("\n")
            self.assertEqual(len(parts), 2)
            for part in parts:
                self.assertTrue(part.plain.startswith("┃ "))
                # 背景铺满整行：每条可视行正好等于日志内容宽度。
                self.assertEqual(part.cell_len, width)
                last = part.get_style_at_offset(console, len(part.plain) - 1)
                self.assertIsNotNone(last.bgcolor)
                self.assertEqual(
                    last.bgcolor.get_truecolor().hex,
                    # BLK1：用户消息改用专属背景（比 raised 更亮）。
                    COLOR_BACKGROUND_USER,
                )
                bar = part.get_style_at_offset(console, 0)
                self.assertEqual(bar.color.get_truecolor().hex, COLOR_ACCENT)

            # 长消息按可视行折行，每一行都保留竖条并铺满宽度。
            long_parts = app._render_user("长" * 100, 40).split("\n")
            self.assertGreater(len(long_parts), 1)
            for part in long_parts:
                self.assertTrue(part.plain.startswith("┃ "))
                self.assertEqual(part.cell_len, 40)

            # Pilot：真实 RichLog 中的首条用户消息也按日志宽度铺满。
            await self._submit(app, pilot, "短消息")
            await app.workers.wait_for_complete()
            await pilot.pause()
            log = app.screen.query_one("#log", RichLog)
            user_line = log.lines[0]
            self.assertTrue(user_line.text.startswith("┃ 短消息"))
            self.assertEqual(user_line.cell_length, log.size.width)
            self.assertGreater(log.size.width, 80)

    async def test_busy_indicator_and_esc_interrupt(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, "/help")
            await pilot.pause()
            await pilot.pause()
            await pilot.press("escape")  # TUIE2：/help 是弹窗，先关掉
            await pilot.pause()

            worker = mock.Mock()
            worker.is_finished = False
            app._dispatcher_worker = worker
            app._set_busy(True)
            await pilot.pause()

            busy = app.screen.query_one("#busy", Static)
            self.assertEqual(str(busy.styles.display), "block")
            busy_text = str(busy.content)
            self.assertIn("主治医生思考中", busy_text)
            self.assertIn("esc 中断", busy_text)

            await pilot.press("escape")
            await pilot.pause()
            self.assertTrue(worker.cancel.called)
            self.assertFalse(app._agent_busy)
            self.assertEqual(str(busy.styles.display), "none")
            self.assertIn("已按 esc 中断", _log_text(app))

    async def test_placeholder_examples_rotate(self) -> None:
        app = self._app()
        # HINT1：示例句轮换是「空闲」状态的提示；把状态钉在空闲再验轮换。
        app.hint_state = lambda: HINT_IDLE  # type: ignore[method-assign]
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            seen = {_placeholder(app)}
            for _ in range(len(PLACEHOLDER_EXAMPLES) * 2):
                app._rotate_placeholder()
                await pilot.pause()
                seen.add(_placeholder(app))
            self.assertEqual(len(seen), len(PLACEHOLDER_EXAMPLES))
            for example in PLACEHOLDER_EXAMPLES:
                self.assertTrue(any(example in item for item in seen))

    async def test_check_and_accept_commands_report_real_results(self) -> None:
        """/check 与 /accept 走线程 worker，事件与结果都必须落进消息流。"""
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            await self._submit(app, pilot, "/check")
            await app.workers.wait_for_complete()
            await pilot.pause()
            text = _log_text(app)
            self.assertIn("调用工具 platform_check", text)
            self.assertIn("体检结论", text)
            self.assertIn("密钥状态", text)

            await self._submit(app, pilot, "/accept")
            await app.workers.wait_for_complete()
            await pilot.pause()
            text = _log_text(app)
            self.assertIn("验收无法执行", text)
            self.assertIn("尚无冻结档案", text)

    async def test_help_command_lists_commands(self) -> None:
        """TUIE2：/help 打开弹窗，命令表逐条等于 COMMANDS（不再打印文本）。"""
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, "/help")
            await pilot.pause()

            self.assertEqual(app.screen.id, "help-view")
            text = str(app.screen.query_one("#info-body", Static).render())
            for command, description in COMMANDS:
                self.assertIn(f"  {command}", text)
                self.assertIn(description, text)
            # 不再往历史区打印文本版清单
            self.assertFalse(
                any("可用命令（本地执行" in str(text) for _, text, _ in app.history)
            )

    async def test_model_command_opens_picker_without_secret(self) -> None:
        sentinel = "sk-tui2-model-SECRET"
        temporary = tempfile.TemporaryDirectory(prefix="tui2-model-picker-")
        self.addCleanup(temporary.cleanup)
        tmp = Path(temporary.name)
        config_path = tmp / "model_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "name": "work",
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "api_key_env": "TUI2_WORK_KEY",
                        },
                        {
                            "name": "claude",
                            "provider": "anthropic",
                            "model": "claude-test",
                            "api_key_env": "TUI2_CLAUDE_KEY",
                        },
                    ],
                    "active_profile": "work",
                }
            ),
            encoding="utf-8",
        )
        app = self._app()
        with mock.patch.object(
            model_config, "DEFAULT_CONFIG_PATH", config_path
        ), mock.patch.object(
            model_config, "user_config_path", lambda: tmp / "user.json"
        ), mock.patch.dict(
            os.environ, {"TUI2_WORK_KEY": sentinel, "TUI2_CLAUDE_KEY": ""}
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await self._submit(app, pilot, "/model")
                await pilot.pause()

                self.assertEqual(app.screen.id, "model-picker")
                picker = app.screen
                self.assertIsInstance(picker, ModelPicker)
                options = picker.query_one("#model-list", OptionList)
                self.assertEqual(
                    [str(option.id) for option in options.options],
                    [
                        "model:deepseek-flash",
                        "model:deepseek-v4-flash",
                        "model:deepseek-v4-pro",
                        "model:deepseek-v4-flash-vision-exp",
                        "work",
                        "claude",
                        "__setup__",
                    ],
                )
                rows = "\n".join(
                    option.prompt.plain for option in options.options
                )
                self.assertIn("deepseek/deepseek-chat", rows)
                self.assertIn("密钥 已设置", rows)
                self.assertIn("密钥 未设置", rows)
                self.assertNotIn(sentinel, _screen_text(app))

    async def test_model_picker_switch_rebuilds_and_keeps_context(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="tui2-model-switch-")
        self.addCleanup(temporary.cleanup)
        tmp = Path(temporary.name)
        config_path = tmp / "model_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "name": "work",
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "api_key_env": "TUI2_WORK_KEY",
                        },
                        {
                            "name": "claude",
                            "provider": "anthropic",
                            "model": "claude-test",
                            "api_key_env": "TUI2_CLAUDE_KEY",
                        },
                    ],
                    "active_profile": "work",
                }
            ),
            encoding="utf-8",
        )
        sentinel = "sk-tui2-switch-SECRET"
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="旧模型回复"),
            workspace_root=PLATFORM_ROOT,
        )
        with mock.patch.object(
            model_config, "DEFAULT_CONFIG_PATH", config_path
        ), mock.patch.object(
            model_config, "user_config_path", lambda: tmp / "user.json"
        ), mock.patch.object(
            model_config, "user_env_path", lambda: tmp / ".env"
        ), mock.patch.dict(
            os.environ,
            {
                "TUI2_WORK_KEY": sentinel,
                "TUI2_CLAUDE_KEY": "",
            },
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await self._submit(app, pilot, "你好")
                await app.workers.wait_for_complete()
                await pilot.pause()

                old_agent = app._dispatcher
                old_state = old_agent.state
                self.assertTrue(old_state.context)

                await self._submit(app, pilot, "/model")
                await pilot.pause()
                self.assertEqual(app.screen.id, "model-picker")
                option_list = app.screen.query_one("#model-list", OptionList)
                ids = [str(option.id) for option in option_list.options]
                option_list.highlighted = ids.index("work")
                await pilot.press("enter")
                await pilot.pause()

                self.assertIsNot(app._dispatcher, old_agent)
                self.assertIs(app._dispatcher.state, old_state)
                self.assertEqual(app._dispatcher.model.model, "deepseek-chat")
                self.assertEqual(app._active_profile.name, "work")
                self.assertIn("deepseek-chat", _status_text(app))
                self.assertIn("DeepSeek", _status_text(app))
                events = [
                    text for kind, text, _ in app.history if kind == "event"
                ]
                self.assertTrue(
                    any(
                        "已切换到 work" in text
                        and "deepseek/deepseek-chat" in text
                        for text in events
                    ),
                    events,
                )
                user_text = (tmp / "user.json").read_text(encoding="utf-8")
                self.assertEqual(
                    json.loads(user_text)["active_profile"], "work"
                )
                self.assertNotIn(sentinel, user_text)

    async def test_model_picker_refuses_missing_key_with_guidance(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="tui2-model-no-key-")
        self.addCleanup(temporary.cleanup)
        tmp = Path(temporary.name)
        config_path = tmp / "model_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "profiles": [
                        {
                            "name": "work",
                            "provider": "deepseek",
                            "model": "deepseek-chat",
                            "api_key_env": "TUI2_WORK_KEY",
                        },
                        {
                            "name": "claude",
                            "provider": "anthropic",
                            "model": "claude-test",
                            "api_key_env": "TUI2_CLAUDE_KEY",
                        },
                    ],
                    "active_profile": "work",
                }
            ),
            encoding="utf-8",
        )
        app = self._app()
        with mock.patch.object(
            model_config, "DEFAULT_CONFIG_PATH", config_path
        ), mock.patch.object(
            model_config, "user_config_path", lambda: tmp / "user.json"
        ), mock.patch.object(
            model_config, "user_env_path", lambda: tmp / ".env"
        ), mock.patch.dict(
            os.environ,
            {
                "TUI2_WORK_KEY": "sk-tui2-work-key",
                "TUI2_CLAUDE_KEY": "",
            },
        ):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await self._submit(app, pilot, "/model")
                await pilot.pause()
                option_list = app.screen.query_one("#model-list", OptionList)
                ids = [str(option.id) for option in option_list.options]
                option_list.highlighted = ids.index("claude")
                await pilot.press("enter")
                await pilot.pause()

                self.assertIsNone(app._active_profile)
                self.assertIsNone(app._dispatcher)
                text = "\n".join(
                    text for _, text, _ in app.history
                )
                self.assertIn("未切换", text)
                self.assertIn("TUI2_CLAUDE_KEY", text)
                self.assertIn(str(tmp / ".env"), text)
                self.assertFalse((tmp / "user.json").exists())

    async def test_slash_autocomplete_filters_completes_and_hides(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()

            prompt.text = "/re"
            await pilot.pause()
            box = app.screen.query_one(PromptBox)
            self.assertTrue(box.suggestions_visible)
            suggested = str(box._suggest.content)
            self.assertIn("/repro", suggested)
            self.assertNotIn("/check", suggested)

            await pilot.press("tab")
            await pilot.pause()
            # SLASH1：/repro 带参数，填入补尾随空格；填入后浮层关闭。
            self.assertEqual(prompt.text, "/repro ")
            self.assertFalse(box.suggestions_visible)

            prompt.text = "/"
            await pilot.pause()
            self.assertTrue(box.suggestions_visible)
            suggested = str(box._suggest.content)
            for command in ("/check", "/market", "/run", "/exit"):
                self.assertIn(command, suggested)
            prompt.text = "/mar"
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(prompt.text, "/market ")

            prompt.text = "/ru"
            await pilot.pause()
            self.assertTrue(box.suggestions_visible)
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(box.suggestions_visible)
            self.assertEqual(prompt.text, "/ru")

            for ordinary in ("hello", "/Users/hostuser/Desktop", "//not-a-command"):
                prompt.text = ordinary
                await pilot.pause()
                self.assertFalse(
                    box.suggestions_visible,
                    f"{ordinary!r} must not trigger autocomplete",
                )

    def test_completion_candidates_cover_existing_commands_and_exit(self) -> None:
        candidates = dict(_completion_candidates("/"))
        self.assertEqual(
            tuple(candidates),
            tuple(command for command, _ in COMMANDS) + ("/exit",),
        )
        self.assertEqual(candidates["/exit"], "退出（/quit 的别名）")
        self.assertEqual(candidates["/check"], "平台体检（环境基线 + 模型配置 + 验收引擎）")
        self.assertEqual(_completion_candidates("hello"), [])
        self.assertEqual(_completion_candidates("/Users/hostuser/x"), [])
        self.assertEqual(_completion_candidates("//x"), [])
        self.assertEqual(
            [command for command, _ in _completion_candidates("/help")],
            ["/help"],
        )

    async def test_run_actors_flag_is_rejected(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, "/run --actors scripted --yes")
            await pilot.pause()

            text = _log_text(app)
            self.assertIn("未知参数", text)
            self.assertNotIn("开始修复", text)

    async def test_run_paid_gate_asks_for_yes(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="tui2-gate-")
        self.addCleanup(temporary.cleanup)
        tmp = Path(temporary.name)

        app = self._app()
        with mock.patch.object(dispatcher, "RUNS_DIR", tmp):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await self._submit(
                    app, pilot, "/run"
                )

                text = _log_text(app)
                self.assertIn("API 额度", text)
                self.assertIn("--yes", text)
                self.assertNotIn("开始修复", text)

        self.assertEqual(list(tmp.rglob("*")), [])

    async def test_natural_language_goes_through_dispatcher_agent(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, "你好，介绍一下自己")
            await app.workers.wait_for_complete()
            await pilot.pause()

            text = _log_text(app)
            self.assertIn("主治医生:", text)
            self.assertIn("我是调度员替身，随时待命", text)

    async def test_pasted_path_message_is_sent_not_treated_as_command(self) -> None:
        """粘贴的绝对路径（含空格/CJK/大写）原样发给调度员，不报未知命令。"""
        pasted = (
            "/Users/hostuser/Desktop/截屏 2026-11-14 15.26.13.png 这是个什么玩意"
        )
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, pasted)
            await app.workers.wait_for_complete()
            await pilot.pause()

            text = _log_text(app)
            self.assertNotIn("未知命令", text)
            self.assertIn("主治医生:", text)
            self.assertIn("我是调度员替身，随时待命", text)
            self.assertNotIn(IMAGE_HINT, text)
            kinds = [kind for kind, _, _ in app.history]
            self.assertIn("user", kinds)
            self.assertIn("assistant", kinds)
            # 原文保留：不截断、不小写化。
            self.assertEqual(app.history[0][:2], ("user", pasted))

    async def test_existing_image_path_gets_honest_hint(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="tui2-image-")
        self.addCleanup(temporary.cleanup)
        image = Path(temporary.name) / "截屏 2026-11-14.png"
        image.write_bytes(b"offline fake png")
        # 用户从终端粘贴时常见「反斜杠转义空格」形式；原文照发。
        pasted = f"{str(image).replace(' ', chr(92) + ' ')} 这是个什么玩意"
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, pasted)
            await app.workers.wait_for_complete()
            await pilot.pause()

            text = _log_text(app)
            self.assertNotIn("未知命令", text)
            self.assertIn(IMAGE_HINT, text)
            self.assertIn("我是调度员替身，随时待命", text)
            hints = [
                record
                for record in app.history
                if record[0] == "event" and record[1] == IMAGE_HINT
            ]
            self.assertEqual(len(hints), 1)
            self.assertEqual(hints[0][2], "muted")

    async def test_plain_message_has_no_image_hint(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, "你好，介绍一下自己")
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertNotIn(IMAGE_HINT, _log_text(app))

    async def test_two_natural_language_messages_reuse_one_event_loop(self) -> None:
        """回归：第二条消息不得在新事件循环上复用已关闭循环的客户端。"""
        model = LoopBoundModel(fallback_reply="收到")
        app = CifixChatApp(model=model, workspace_root=PLATFORM_ROOT)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, "第一条消息")
            await app.workers.wait_for_complete()
            await pilot.pause()
            first_dispatcher = app._dispatcher
            self.assertIsNotNone(first_dispatcher)
            await self._submit(app, pilot, "第二条消息")
            await app.workers.wait_for_complete()
            await pilot.pause()

            text = _log_text(app)
            reused_dispatcher = app._dispatcher

        self.assertIs(reused_dispatcher, first_dispatcher)
        # 调度员标签独占一行，正文在下一行：两次回复各出现一次标签与正文。
        self.assertEqual(text.count("主治医生:"), 2, text)
        self.assertEqual(text.count("收到"), 2, text)
        self.assertNotIn("Event loop is closed", text)
        self.assertNotIn("主治医生暂时不可用", text)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(len(set(map(id, model.call_loops))), 1)
        self.assertFalse(model.bound_loop.is_closed())

    async def test_ctrl_c_exits(self) -> None:
        app = self._app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+c")
            await pilot.pause()

        self.assertFalse(app.is_running)

    async def test_quit_command_exits(self) -> None:
        app = self._app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, "/quit")
            await pilot.pause()

        self.assertFalse(app.is_running)

    async def test_unknown_slash_command_is_reported(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._submit(app, pilot, "/hlep")

            text = _log_text(app)
            self.assertIn("未知命令: /hlep", text)
            self.assertIn("/help", text)


class AssistantMarkdownRenderTest(unittest.IsolatedAsyncioTestCase):
    """TUI2b-fix3 回归：调度员回复的 Markdown 渲染、折行宽度与记录间距。"""

    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)

    def _app(self) -> CifixChatApp:
        return CifixChatApp(
            model=FakeChatModel(fallback_reply=LONG_MARKDOWN_REPLY),
            workspace_root=PLATFORM_ROOT,
        )

    async def _ask(self, app, pilot) -> None:
        prompt = app.screen.query_one(PromptInput)
        prompt.focus()
        prompt.text = "你好"
        await pilot.pause()
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()

    @staticmethod
    def _lines(app) -> list[str]:
        log = app.screen.query_one("#log", RichLog)
        return [strip.text for strip in log.lines]

    def _assert_no_orphan_ordinals(self, lines: list[str]) -> None:
        orphans = [
            line for line in lines if _ORPHAN_ORDINAL.match(line.strip())
        ]
        self.assertEqual(orphans, [], f"出现序号孤行: {orphans!r}")

    async def test_markdown_reply_has_no_orphans_or_literal_asterisks(self) -> None:
        """(a)(b)(c)：120 列下无序号孤行、无字面 **、assistant 前有空行。"""
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._ask(app, pilot)

            log = app.screen.query_one("#log", RichLog)
            lines = self._lines(app)
            text = "\n".join(lines)

            # 发送者标签仍可见；正文逐条仍在（Markdown 只解释语法不吞字）。
            self.assertIn("主治医生:", lines)
            for fragment in (
                "billing-specialist",
                "orders-specialist",
                "report-specialist",
                "预计三轮内把失败数清零",
            ):
                self.assertIn(fragment, text)

            # (b) 粗体语法被解释，不残留字面 **。
            self.assertNotIn("**", text)
            # (a) 不存在只含序号标点的孤行。
            self._assert_no_orphan_ordinals(lines)

            # (c) assistant 记录前有且只有一行空行；用户块后也有空行。
            label_index = lines.index("主治医生:")
            self.assertGreater(label_index, 0)
            self.assertEqual(lines[label_index - 1].strip(), "")
            self.assertEqual(lines[1].strip(), "")

            # 所有行按日志实际宽度折行，不按默认 80 列：最宽行正好等于日志宽度。
            widths = [strip.cell_length for strip in log.lines]
            self.assertTrue(all(width <= log.size.width for width in widths))
            self.assertEqual(max(widths), log.size.width)

    async def test_narrow_screen_has_no_orphans(self) -> None:
        """(d)：70 列窄屏下同一回复不出现序号孤行，且按窄屏宽度折行。"""
        app = self._app()
        async with app.run_test(size=(70, 30)) as pilot:
            await pilot.pause()
            await self._ask(app, pilot)

            log = app.screen.query_one("#log", RichLog)
            lines = self._lines(app)
            text = "\n".join(lines)

            self.assertLessEqual(log.size.width, 70)
            self._assert_no_orphan_ordinals(lines)
            self.assertNotIn("**", text)
            self.assertIn("billing-specialist", text)
            widths = [strip.cell_length for strip in log.lines]
            self.assertTrue(all(width <= log.size.width for width in widths))
            self.assertEqual(max(widths), log.size.width)

    async def test_history_replay_matches_live_rendering(self) -> None:
        """历史重放复用同一 render 路径，逐行等于 live 追加结果。"""
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._ask(app, pilot)
            live = self._lines(app)

            app.screen._replay_history()
            await pilot.pause()
            replayed = self._lines(app)

        self.assertEqual(replayed[: len(live)], live)
        self.assertEqual(replayed[len(live) :], live)

    async def test_plain_records_stay_plain_text(self) -> None:
        """plain 记录不走 Markdown：** 与序号保持字面，且按日志宽度渲染。"""
        app = self._app()
        async with app.run_test(size=(70, 30)) as pilot:
            await pilot.pause()
            await self._ask(app, pilot)

            app._log("**纯文本** 与 4. 序号不得被 Markdown 解释")
            await pilot.pause()

            log = app.screen.query_one("#log", RichLog)
            text = "\n".join(strip.text for strip in log.lines)
            self.assertIn("**纯文本** 与 4. 序号不得被 Markdown 解释", text)
            widths = [strip.cell_length for strip in log.lines]
            self.assertTrue(all(width <= log.size.width for width in widths))
            self.assertEqual(max(widths), log.size.width)


class NoUsageModel(FakeChatModel):
    """不带 usage 的假模型：验证「没有 token 数据就不显示 token 段」。"""

    async def _call_api(self, *args, **kwargs) -> ChatResponse:
        response = await super()._call_api(*args, **kwargs)
        assert isinstance(response, ChatResponse)
        # ChatResponse 是 DictMixin（dict 风格），属性赋值即写字典项。
        response.usage = None
        return response


async def _submit_text(app, pilot, text: str) -> None:
    prompt = app.screen.query_one(PromptInput)
    prompt.focus()
    prompt.text = text
    await pilot.pause()
    # SLASH1：浮层开着时第一下 enter 只填入、不提交；真实按键再按一次才提交。
    if prompt.box is not None and prompt.box.suggestions_visible:
        await pilot.press("enter")
        await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()


async def _wait_until(pilot, predicate, label: str, attempts: int = 300) -> None:
    """事件门控等待：只轮询 pilot.pause，不 sleep、不靠墙钟竞速。"""
    for _ in range(attempts):
        await pilot.pause()
        if predicate():
            return
    raise AssertionError(f"等待超时：{label}")


class StreamedReplyTest(unittest.IsolatedAsyncioTestCase):
    """TUI3 §1：流式增量显示、落定全文、esc 中断与复用。"""

    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)

    async def test_settled_text_equals_plain_reply(self) -> None:
        """① 多块流式落定全文 == 同输入 Agent.reply() 全文（只存一条记录）。"""
        chunks = ["第一段，", "第二段，", "第三段。"]
        full = "".join(chunks)
        model = ChunkedFakeChatModel(chunks, output_tokens=9)
        app = CifixChatApp(model=model, workspace_root=PLATFORM_ROOT)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "你好")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assistants = [
                text for kind, text, _ in app.history if kind == "assistant"
            ]
            self.assertEqual(assistants, [full])
            self.assertIn(full, _log_text(app))
            # 真的走了流式路径（模型侧按段产出）
            self.assertTrue(model.calls[0]["streamed"])
            self.assertEqual(model.emitted, len(chunks))

            # 落定后紧跟元信息行；token 只认 ModelCallEnd 实报值。
            meta_index = app.history.index(("assistant", full, "plain")) + 1
            meta = app.history[meta_index]
            self.assertEqual(meta[0], "event")
            self.assertEqual(meta[2], "muted")
            self.assertRegex(meta[1], r"^用时 \d+\.\d+s · 输出 9 tok$")

            # 历史重放与落定态逐行一致。
            live = [strip.text for strip in app.screen.query_one("#log", RichLog).lines]
            app.screen._replay_history()
            await pilot.pause()
            replayed = [
                strip.text
                for strip in app.screen.query_one("#log", RichLog).lines
            ]
            self.assertEqual(replayed[: len(live)], live)

        # 同输入、同分段模型走 Agent.reply()：全文逐字一致。
        baseline_agent = build_dispatcher(
            model=ChunkedFakeChatModel(chunks, output_tokens=9)
        )
        baseline = await baseline_agent.reply(
            Msg(
                name="用户",
                role="user",
                content=[TextBlock(text="你好")],
            )
        )
        self.assertEqual(baseline.get_text_content(), full)
        self.assertEqual(assistants[0], baseline.get_text_content())

    async def test_partial_text_visible_midstream(self) -> None:
        """② 门控分块：落定前就能看到已到达的增量文本（纯文本）。"""
        chunks = ["第一段；", "第二段；", "第三段。"]
        full = "".join(chunks)
        gates = [asyncio.Event() for _ in chunks]

        async def gate(index: int) -> None:
            await gates[index].wait()

        model = ChunkedFakeChatModel(chunks, gate=gate, output_tokens=3)
        app = CifixChatApp(model=model, workspace_root=PLATFORM_ROOT)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "你好")

            # 首个 delta 之前：思考指示可见，带真实计时的秒数。
            busy = app.screen.query_one("#busy", Static)
            await _wait_until(
                pilot,
                lambda: str(busy.styles.display) == "block",
                "思考指示出现",
            )
            self.assertRegex(
                str(busy.content), r"主治医生思考中… \d+\.\d+s"
            )
            self.assertIn("esc 中断", str(busy.content))

            gates[0].set()
            await _wait_until(
                pilot,
                lambda: app.stream_text == chunks[0],
                "首段增量到达",
            )
            stream = app.screen.query_one("#stream", VerticalScroll)
            self.assertEqual(str(stream.styles.display), "block")
            self.assertEqual(
                str(app.screen.query_one("#stream-text", Static).content),
                "主治医生:\n" + chunks[0],
            )
            # 流式期间思考指示让位给增量文本；落定态尚未出现。
            self.assertEqual(str(busy.styles.display), "none")
            self.assertNotIn(full, _log_text(app))

            for event in gates[1:]:
                event.set()
            await app.workers.wait_for_complete()
            await pilot.pause()

            self.assertEqual(str(stream.styles.display), "none")
            self.assertIn(full, _log_text(app))
            assistants = [
                text for kind, text, _ in app.history if kind == "assistant"
            ]
            self.assertEqual(assistants, [full])

    async def test_esc_interrupt_keeps_partial_and_next_reply_works(self) -> None:
        """③ esc 中断：部分文本落定 + muted「（已中断）」，下一条照常回复。"""
        chunks = ["甲段；", "乙段；", "丙段。"]
        gates = [asyncio.Event() for _ in chunks]

        async def gate(index: int) -> None:
            await gates[index].wait()

        model = ChunkedFakeChatModel(chunks, gate=gate, output_tokens=5)
        app = CifixChatApp(model=model, workspace_root=PLATFORM_ROOT)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "第一条")
            gates[0].set()
            await _wait_until(
                pilot, lambda: app.stream_text == chunks[0], "首段增量"
            )

            await pilot.press("escape")
            await _wait_until(pilot, lambda: not app._agent_busy, "中断收敛")
            await pilot.pause()

            assistants = [
                text for kind, text, _ in app.history if kind == "assistant"
            ]
            self.assertEqual(assistants, [chunks[0]])
            markers = [
                (text, tone)
                for kind, text, tone in app.history
                if kind == "event" and text == INTERRUPT_MARKER
            ]
            self.assertEqual(markers, [(INTERRUPT_MARKER, "muted")])
            self.assertIn("● （已中断）", _log_text(app))
            # 不得把 AgentScope 默认的英文中断消息当成本条回复落定。
            self.assertNotIn("I notice the interruption", _log_text(app))
            self.assertEqual(
                str(
                    app.screen.query_one("#stream", VerticalScroll).styles.display
                ),
                "none",
            )
            self.assertEqual(app.stream_text, "")

            first_dispatcher = app._dispatcher
            # 同一 Agent / 同一事件循环：下一条消息必须还能正常回复。
            for event in gates[1:]:
                event.set()
            await _submit_text(app, pilot, "第二条")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assistants = [
                text for kind, text, _ in app.history if kind == "assistant"
            ]
            self.assertEqual(assistants, [chunks[0], "".join(chunks)])
            self.assertIs(app._dispatcher, first_dispatcher)
            self.assertEqual(model.call_count, 2)
            self.assertNotIn("主治医生暂时不可用", _log_text(app))
            self.assertNotIn("Event loop is closed", _log_text(app))


    async def test_tool_events_keep_flowing_during_stream(self) -> None:
        """工具事件（既有 event_sink 通道）在流式期间照常进日志，不被吞掉。"""
        chunks = ["先看仓库；", "再看验收。"]
        gates = [asyncio.Event() for _ in chunks]

        async def gate(index: int) -> None:
            await gates[index].wait()

        model = ChunkedFakeChatModel(chunks, gate=gate, output_tokens=4)
        app = CifixChatApp(model=model, workspace_root=PLATFORM_ROOT)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "看看这个仓库")
            gates[0].set()
            await _wait_until(
                pilot, lambda: app.stream_text == chunks[0], "首段增量"
            )

            app._on_platform_event(
                {
                    "event": "tool_call",
                    "tool": "accept_check",
                    "summary": "workspace=demo_repo",
                }
            )
            await pilot.pause()

            text = _log_text(app)
            self.assertIn("调用工具 accept_check", text)
            self.assertIn("workspace=demo_repo", text)
            # 文本流仍在（工具事件没有把它顶掉/清空）
            self.assertEqual(app.stream_text, chunks[0])
            self.assertEqual(
                str(app.screen.query_one("#stream", VerticalScroll).styles.display),
                "block",
            )

            for event in gates[1:]:
                event.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIn("".join(chunks), _log_text(app))

    async def test_agent_without_reply_stream_falls_back_to_plain_reply(
        self,
    ) -> None:
        """兜底：Agent 没有 reply_stream → 与改动前一致，整段落定 + 元信息。"""
        from agentscope.agent import Agent

        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="整段回复"),
            workspace_root=PLATFORM_ROOT,
        )
        with mock.patch.object(Agent, "reply_stream", None, create=True):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await _submit_text(app, pilot, "你好")
                await app.workers.wait_for_complete()
                await pilot.pause()

                assistants = [
                    text for kind, text, _ in app.history if kind == "assistant"
                ]
                self.assertEqual(assistants, ["整段回复"])
                self.assertIn("整段回复", _log_text(app))
                metas = [
                    text
                    for kind, text, _ in app.history
                    if kind == "event" and text.startswith("用时 ")
                ]
                self.assertEqual(len(metas), 1)


class ReplyMetaTest(unittest.IsolatedAsyncioTestCase):
    """TUI3 §2：回复元信息（墙钟耗时 + 实报 token）。"""

    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)

    def _meta_records(self, app) -> list[tuple[str, str]]:
        return [
            (kind, text)
            for kind, text, tone in app.history
            if kind == "event" and text.startswith("用时 ")
        ]

    async def test_meta_line_has_elapsed_and_reported_tokens(self) -> None:
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="收到"),
            workspace_root=PLATFORM_ROOT,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "你好")
            await app.workers.wait_for_complete()
            await pilot.pause()

            metas = self._meta_records(app)
            self.assertEqual(len(metas), 1)
            self.assertRegex(
                metas[0][1], r"^用时 \d+\.\d+s · 输出 [1-9]\d* tok$"
            )
            # 元信息紧跟在 assistant 记录之后。
            assistant_index = next(
                index
                for index, record in enumerate(app.history)
                if record[0] == "assistant"
            )
            self.assertEqual(app.history[assistant_index + 1][0], "event")

    async def test_no_token_data_means_no_token_segment(self) -> None:
        app = CifixChatApp(
            model=NoUsageModel(fallback_reply="收到"),
            workspace_root=PLATFORM_ROOT,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "你好")
            await app.workers.wait_for_complete()
            await pilot.pause()

            metas = self._meta_records(app)
            self.assertEqual(len(metas), 1)
            self.assertRegex(metas[0][1], r"^用时 \d+\.\d+s$")
            self.assertNotIn("tok", metas[0][1])


class SessionHeaderTest(unittest.IsolatedAsyncioTestCase):
    """TUI3 §4：会话页 slim 标题栏。"""

    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)

    async def test_header_shows_title_and_live_message_count(self) -> None:
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="收到"),
            workspace_root=PLATFORM_ROOT,
        )
        # 尚无消息：标题叫「新会话」，计数为 0。
        self.assertEqual(app.session_title(), "新会话")
        self.assertEqual(app.message_count(), 0)

        first = "请你先看看这个仓库到底为什么验收不过，再决定要不要动手改它"
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertFalse(app.screen.query(SessionHeader))
            await _submit_text(app, pilot, first)
            await app.workers.wait_for_complete()
            await pilot.pause()

            header = app.screen.query_one("#session-header", SessionHeader)
            self.assertEqual(header.region.y, 0)
            self.assertEqual(header.size.height, 1)
            title = app.session_title()
            self.assertEqual(title, first[:TITLE_LIMIT] + "…")
            self.assertEqual(app.message_count(), 2)
            rendered = header.render().plain
            self.assertIn(title, rendered)
            self.assertIn("共 2 条消息", rendered)
            self.assertLessEqual(cell_len(rendered), header.size.width)

            await _submit_text(app, pilot, "继续")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(app.message_count(), 4)
            self.assertIn("共 4 条消息", header.render().plain)
            # 标题始终取首条用户消息。
            self.assertEqual(app.session_title(), title)

    async def test_short_title_is_not_truncated(self) -> None:
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="收到"),
            workspace_root=PLATFORM_ROOT,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "修一下账单")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(app.session_title(), "修一下账单")


class InputHistoryTest(unittest.IsolatedAsyncioTestCase):
    """TUI3 §6：↑/↓ 召回历史、去重、发送后复位。"""

    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)

    async def _send(self, app, pilot, text: str) -> None:
        await _submit_text(app, pilot, text)
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_up_down_recall_dedup_and_reset(self) -> None:
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="收到"),
            workspace_root=PLATFORM_ROOT,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._send(app, pilot, "第一条")
            await self._send(app, pilot, "第二条")
            await self._send(app, pilot, "第二条")  # 连续重复只存一条
            self.assertEqual(app.input_history, ["第一条", "第二条"])

            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(prompt.text, "第二条")
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(prompt.text, "第一条")
            await pilot.press("up")  # 已在最早一条：不越界
            await pilot.pause()
            self.assertEqual(prompt.text, "第一条")
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(prompt.text, "第二条")
            await pilot.press("down")  # 越过最新一条回到草稿
            await pilot.pause()
            self.assertEqual(prompt.text, "")

            # 发送后指针复位：下一次 ↑ 又从最新一条开始。
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(prompt.text, "第二条")
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            prompt = app.screen.query_one(PromptInput)
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(prompt.text, "第二条")

    async def test_arrows_in_multiline_edit_are_not_hijacked(self) -> None:
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="收到"),
            workspace_root=PLATFORM_ROOT,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._send(app, pilot, "历史一条")
            prompt = app.screen.query_one(PromptInput)
            prompt.text = "第一行\n第二行"
            prompt.cursor_location = (1, 3)
            await pilot.pause()
            # 光标在末行：↑ 是多行编辑（上移一行），不召回历史。
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(prompt.text, "第一行\n第二行")
            self.assertEqual(prompt.cursor_location[0], 0)
            # 现在在首行：↑ 才进入历史。
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(prompt.text, "历史一条")

    async def test_down_at_last_line_walks_history(self) -> None:
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="收到"),
            workspace_root=PLATFORM_ROOT,
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await self._send(app, pilot, "老消息")
            prompt = app.screen.query_one(PromptInput)
            prompt.focus()
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(prompt.text, "老消息")
            self.assertEqual(prompt.cursor_location[0], 0)
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(prompt.text, "")


def _thinking_rows(app) -> list[str]:
    return [
        text
        for kind, text, _tone in app.history
        if kind == "event" and (text == "思考" or text.startswith("思考 ·"))
    ]


def _user_data_block_counts(model) -> list[int]:
    counts = []
    for call in model.calls:
        for item in call.get("messages", []):
            if item.get("role") == "user":
                counts.append(item["block_types"].count("DataBlock"))
    return counts


class ThinkingDisplayTest(unittest.IsolatedAsyncioTestCase):
    """SEC1：只有模型真的产出 ThinkingBlock 时才显示思考。"""

    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)

    async def test_thinking_streams_then_collapses_to_one_line(self) -> None:
        thinking = ["先核对失败用例，", "再看派单。"]
        chunks = ["结论：", "还没过。"]
        full_thinking = "".join(thinking)
        full = "".join(chunks)
        gates = [asyncio.Event() for _ in range(len(thinking) + len(chunks))]

        async def gate(index: int) -> None:
            await gates[index].wait()

        model = ThinkingChunkedFakeChatModel(
            thinking, chunks, gate=gate, output_tokens=6
        )
        app = CifixChatApp(model=model, workspace_root=PLATFORM_ROOT)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "看看验收")
            gates[0].set()
            await _wait_until(
                pilot,
                lambda: thinking[0] in str(
                    app.screen.query_one("#stream-text", Static).content
                ),
                "思考增量可见",
            )
            stream = app.screen.query_one("#stream-text", Static).content
            plain = str(stream)
            self.assertIn("思考中", plain)
            self.assertIn(thinking[0], plain)
            self.assertTrue(
                any(
                    COLOR_TEXT_MUTED in str(span.style) for span in stream.spans
                )
            )
            self.assertEqual(
                [text for kind, text, _ in app.history if kind == "assistant"],
                [],
            )

            for event in gates[1:]:
                event.set()
            await app.workers.wait_for_complete()
            await pilot.pause()

            assistants = [
                text for kind, text, _ in app.history if kind == "assistant"
            ]
            self.assertEqual(assistants, [full])
            rows = _thinking_rows(app)
            self.assertEqual(len(rows), 1)
            self.assertRegex(rows[0], r"^思考 · \d+\.\d+s$")
            self.assertEqual(
                [
                    tone
                    for kind, text, tone in app.history
                    if kind == "event" and text == rows[0]
                ],
                ["muted"],
            )
            self.assertNotIn(full_thinking, _log_text(app))
            self.assertEqual(
                str(app.screen.query_one("#stream", VerticalScroll).styles.display),
                "none",
            )

        baseline = await build_dispatcher(
            model=ThinkingChunkedFakeChatModel(thinking, chunks, output_tokens=6)
        ).reply(
            Msg(name="用户", role="user", content=[TextBlock(text="看看验收")])
        )
        self.assertEqual(baseline.get_text_content(), full)
        self.assertEqual(assistants[0], baseline.get_text_content())

    async def test_thinking_without_text_delta_omits_duration(self) -> None:
        model = ThinkingChunkedFakeChatModel(
            ["只在脑子里转了一圈。"],
            ["落定正文"],
            emit_text_deltas=False,
            output_tokens=2,
        )
        app = CifixChatApp(model=model, workspace_root=PLATFORM_ROOT)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "想一下")
            await app.workers.wait_for_complete()
            await pilot.pause()

            assistants = [
                text for kind, text, _ in app.history if kind == "assistant"
            ]
            self.assertEqual(assistants, ["落定正文"])
            self.assertEqual(_thinking_rows(app), ["思考"])
            # BLK1：收起态折叠行带首行摘要；判「未展开」= 没有单独一行全文。
            self.assertNotIn(
                "只在脑子里转了一圈。",
                [line.strip() for line in _log_text(app).splitlines()],
            )

        baseline = await build_dispatcher(
            model=ThinkingChunkedFakeChatModel(
                ["只在脑子里转了一圈。"],
                ["落定正文"],
                emit_text_deltas=False,
                output_tokens=2,
            )
        ).reply(
            Msg(name="用户", role="user", content=[TextBlock(text="想一下")])
        )
        self.assertEqual(assistants[0], baseline.get_text_content())

    async def test_reply_without_thinking_has_no_thinking_row(self) -> None:
        model = ChunkedFakeChatModel(["只有正文。"], output_tokens=1)
        app = CifixChatApp(model=model, workspace_root=PLATFORM_ROOT)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "你好")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(_thinking_rows(app), [])
            assistants = [
                text for kind, text, _ in app.history if kind == "assistant"
            ]
            self.assertEqual(assistants, ["只有正文。"])


class VisionAttachmentTest(unittest.IsolatedAsyncioTestCase):
    """SEC1：vision=true 才附图；vision=false 保持还不能看图。"""

    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        temporary = tempfile.TemporaryDirectory(prefix="sec1-image-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def _png(self, name: str) -> Path:
        path = self.tmp / name
        path.write_bytes(b"\x89PNG\r\n\x1a\noffline")
        return path

    async def test_vision_model_attaches_all_images(self) -> None:
        # PST1：去掉只附第一张的限制，视觉模型多图全发。
        first = self._png("shot.png")
        second = self._png("other.png")
        model = FakeChatModel(fallback_reply="看到了", model_name="deepseek-flash")
        model.formatter.input_types = ["text/plain", "image/*"]
        app = CifixChatApp(model=model, workspace_root=PLATFORM_ROOT)
        pasted = f"{first} 和 {second}"
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, pasted)
            await app.workers.wait_for_complete()
            await pilot.pause()

            note = f"已附图：{first.name}、{second.name}"
            notes = [
                record
                for record in app.history
                if record[0] == "event" and record[1] == note
            ]
            self.assertEqual(len(notes), 1)
            self.assertEqual(notes[0][2], "muted")
            self.assertNotIn(IMAGE_HINT, _log_text(app))
            self.assertIn("看到了", _log_text(app))

        self.assertEqual(sum(_user_data_block_counts(model)), 2)

    async def test_single_image_note_names_the_file(self) -> None:
        image = self._png("only.png")
        model = FakeChatModel(fallback_reply="看到了", model_name="deepseek-flash")
        model.formatter.input_types = ["text/plain", "image/*"]
        app = CifixChatApp(model=model, workspace_root=PLATFORM_ROOT)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, str(image))
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIn(f"已附图：{image.name}", _log_text(app))
            self.assertNotIn("只附了第一张", _log_text(app))
            self.assertNotIn(IMAGE_HINT, _log_text(app))
        self.assertEqual(sum(_user_data_block_counts(model)), 1)

    async def test_non_vision_model_keeps_honest_hint(self) -> None:
        image = self._png("blind.png")
        model = FakeChatModel(fallback_reply="只看到路径", model_name="deepseek-chat")
        # 即使替身 formatter 能收图片，能力表 vision=false 也不许把图发出去。
        model.formatter.input_types = ["text/plain", "image/*"]
        app = CifixChatApp(model=model, workspace_root=PLATFORM_ROOT)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, str(image))
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIn(IMAGE_HINT, _log_text(app))
            self.assertNotIn("已附图：", _log_text(app))
        self.assertEqual(sum(_user_data_block_counts(model)), 0)


class Trace1RunsCommandTest(unittest.IsolatedAsyncioTestCase):
    """/runs 在 TUI 里能列出平台真实运行记录（只读呈现）。"""

    def setUp(self) -> None:
        dispatcher.set_event_sink(None)
        self._tmp = tempfile.TemporaryDirectory(prefix="trace1-tui-")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        root = Path(self._tmp.name).resolve()
        self.workspace = root / "proj"
        self.workspace.mkdir(parents=True)
        (self.workspace / "pyproject.toml").write_text(
            "[project]\nname = \"trace1-fixture\"\n", encoding="utf-8"
        )

    async def test_runs_lists_platform_records(self) -> None:
        model = ScriptedToolModel([{"type": "text", "text": "你好"}])
        app = CifixChatApp(model=model, workspace_root=self.workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "你好")
            await _wait_until(
                pilot, lambda: not app._agent_busy, "首轮回复落定"
            )
            await _submit_text(app, pilot, "/runs")
            await _wait_until(
                pilot,
                lambda: "运行记录" in _log_text(app),
                "/runs 列表回显",
            )
            self.assertIn("最近的运行记录", _log_text(app))


if __name__ == "__main__":
    unittest.main()


class TuiConfirmWiringTest(unittest.IsolatedAsyncioTestCase):
    """PERM1B2T 第 1 步：界面秘书挂上文件工具与可确认模式。"""

    async def test_tui_agent_has_secretary_read_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = (Path(tmp) / "proj").resolve()
            workspace.mkdir()
            (workspace / "pyproject.toml").write_text(
                "[project]\nname = \"wiring-fixture\"\n",
                encoding="utf-8",
            )
            app = CifixChatApp(
                model=FakeChatModel(fallback_reply="x"),
                workspace_root=workspace,
            )
            agent = app._ensure_dispatcher()
            read_tool = await agent.toolkit.get_tool("Read")
            self.assertIsNotNone(read_tool)
            self.assertIs(
                getattr(agent, "_drbug_secretary_read", None),
                read_tool,
            )
            self.assertTrue(read_tool._has_confirm_responder)
            self.assertEqual(read_tool.executions, 0)


class TuiConfirmTest(unittest.IsolatedAsyncioTestCase):
    """PERM1B2T2：界面行内确认（答 Y 读、答 N 拒、工作区内不问）。"""

    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        temporary = tempfile.TemporaryDirectory(prefix="tui-confirm-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.workspace = root / "proj"
        self.workspace.mkdir()
        (self.workspace / "pyproject.toml").write_text(
            "[project]\nname = \"confirm-fixture\"\n", encoding="utf-8"
        )
        self.payload = "tui-confirm-payload-7391"
        self.outside = root / "outside-notes.txt"
        self.outside.write_text(self.payload + "\n", encoding="utf-8")

    def _app_reading(
        self, target: Path
    ) -> tuple[CifixChatApp, ScriptedToolModel]:
        model = ScriptedToolModel(
            [
                {
                    "type": "tool",
                    "name": "Read",
                    "input": {"file_path": str(target)},
                },
                {"type": "text", "text": "读完了"},
            ]
        )
        app = CifixChatApp(model=model, workspace_root=self.workspace)
        return app, model

    def _audit_text(self) -> str:
        log = self.workspace / ".drbug" / "audit.log"
        return log.read_text(encoding="utf-8") if log.exists() else ""

    async def test_tui_confirm_yes_reads_outside_file(self) -> None:
        app, model = self._app_reading(self.outside)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "读一下项目外那个文件")
            await _wait_until(
                pilot, lambda: app._awaiting_confirm, "进入等待确认"
            )
            self.assertIn("在等你答复", str(app._spinner_text(0)))
            from core.tui import ConfirmModal

            self.assertIsInstance(app.screen, ConfirmModal)
            await pilot.press("y")
            await _wait_until(
                pilot, lambda: not app._agent_busy, "回复落定"
            )
            log_text = _log_text(app)
            self.assertIn(str(self.outside), log_text)
            self.assertIn("输入 Y 放行", log_text)
            read_tool = await app._dispatcher.toolkit.get_tool("Read")
            self.assertEqual(read_tool.executions, 1)
            results = "\n".join(
                text
                for messages in model.seen_messages
                for text in _tool_result_texts(messages)
            )
            self.assertIn(self.payload, results)
            self.assertIn("答复 Y；已执行", self._audit_text())

    async def test_tui_confirm_no_denies(self) -> None:
        app, model = self._app_reading(self.outside)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "读一下项目外那个文件")
            await _wait_until(
                pilot, lambda: app._awaiting_confirm, "进入等待确认"
            )
            await pilot.press("n")
            await _wait_until(
                pilot, lambda: not app._agent_busy, "回复落定"
            )
            read_tool = await app._dispatcher.toolkit.get_tool("Read")
            self.assertEqual(read_tool.executions, 0)
            results = "\n".join(
                text
                for messages in model.seen_messages
                for text in _tool_result_texts(messages)
            )
            self.assertNotIn(self.payload, results)
            self.assertIn("答复 N；未执行", self._audit_text())

    async def test_tui_in_workspace_read_needs_no_confirm(
        self,
    ) -> None:
        inner = self.workspace / "inner.txt"
        inner.write_text(self.payload + "\n", encoding="utf-8")
        app, model = self._app_reading(inner)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "读一下项目里那个文件")
            await _wait_until(
                pilot,
                lambda: len(model.seen_messages) >= 2
                and not app._agent_busy,
                "回复落定",
            )
            self.assertFalse(app._awaiting_confirm)
            read_tool = await app._dispatcher.toolkit.get_tool("Read")
            self.assertEqual(read_tool.executions, 1)
            self.assertNotIn("输入 Y 放行", _log_text(app))
            self.assertNotIn("答复", self._audit_text())


class TuiConfirmModalTest(unittest.IsolatedAsyncioTestCase):
    """PERM2a：审批弹窗按键全路径（Y/A/P 两段/Esc + 描述不进弹窗）。"""

    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        temporary = tempfile.TemporaryDirectory(prefix="tui-modal-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.root = root
        self.workspace = root / "proj"
        self.workspace.mkdir()
        (self.workspace / "pyproject.toml").write_text(
            "[project]\nname = \"modal-fixture\"\n", encoding="utf-8"
        )
        self.payload = "tui-modal-payload-5150"
        self.outside = root / "outside-modal.txt"
        self.outside.write_text(self.payload + "\n", encoding="utf-8")

    def _app(self, script: list) -> tuple[CifixChatApp, ScriptedToolModel]:
        model = ScriptedToolModel(script)
        app = CifixChatApp(model=model, workspace_root=self.workspace)
        return app, model

    def _read_script(self, extra: dict | None = None) -> dict:
        tool_input = {"file_path": str(self.outside)}
        if extra:
            tool_input.update(extra)
        return {"type": "tool", "name": "Read", "input": tool_input}

    def _audit_text(self) -> str:
        log = self.workspace / ".drbug" / "audit.log"
        return log.read_text(encoding="utf-8") if log.exists() else ""

    def _modal_text(self, app: CifixChatApp) -> str:
        return "\n".join(getattr(app.screen, "_lines", []))

    def _modal_preview_text(self, app: CifixChatApp) -> str:
        return "\n".join(getattr(app.screen, "_preview_lines", []) or [])

    def _results_text(self, model: ScriptedToolModel) -> str:
        return "\n".join(
            text
            for messages in model.seen_messages
            for text in _tool_result_texts(messages)
        )

    async def test_modal_hides_model_description(self) -> None:
        app, model = self._app(
            [
                self._read_script(
                    {"description": "只是整理一下文件，完全无害"}
                ),
                {"type": "text", "text": "读完了"},
            ]
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "读一下项目外那个文件")
            await _wait_until(
                pilot, lambda: app._awaiting_confirm, "进入等待确认"
            )
            from core.tui import ConfirmModal

            self.assertIsInstance(app.screen, ConfirmModal)
            modal_text = self._modal_text(app)
            self.assertIn(str(self.outside), modal_text)
            self.assertIn("需要你确认", modal_text)
            self.assertNotIn("完全无害", modal_text)
            # 到此为止：本用例只验弹窗文案面。带 description 的
            # 调用在执行层会因未知参数被框架挡下（既有行为，
            # 非本单范围），故用 Esc 收尾，不验执行计数。
            await pilot.press("escape")
            await _wait_until(
                pilot, lambda: not app._agent_busy, "回复落定"
            )
            read_tool = await app._dispatcher.toolkit.get_tool("Read")
            self.assertEqual(read_tool.executions, 0)

    async def test_modal_a_session_allows_second_read(self) -> None:
        app, model = self._app(
            [
                self._read_script(),
                self._read_script(),
                {"type": "text", "text": "读完了"},
            ]
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "读两遍项目外那个文件")
            await _wait_until(
                pilot, lambda: app._awaiting_confirm, "进入等待确认"
            )
            await pilot.press("a")
            await _wait_until(
                pilot, lambda: not app._agent_busy, "回复落定"
            )
            read_tool = await app._dispatcher.toolkit.get_tool("Read")
            self.assertEqual(read_tool.executions, 2)
            self.assertIn("add_session_rule", self._audit_text())

    async def test_modal_p_two_step_writes_global_rule(self) -> None:
        import os
        from unittest import mock

        fake_home = self.root / "fake-home"
        fake_home.mkdir()
        app, model = self._app(
            [
                self._read_script(),
                {"type": "text", "text": "读完了"},
            ]
        )
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await _submit_text(app, pilot, "读一下项目外那个文件")
                await _wait_until(
                    pilot, lambda: app._awaiting_confirm, "进入等待确认"
                )
                await pilot.press("p")
                await _wait_until(
                    pilot,
                    lambda: getattr(app.screen, "_in_preview", False),
                    "规则预览屏",
                )
                self.assertIn("[[rule]]", self._modal_preview_text(app))
                self.assertIn(
                    str(self.outside), self._modal_preview_text(app)
                )
                await pilot.press("p")
                await _wait_until(
                    pilot, lambda: not app._agent_busy, "回复落定"
                )
                policy_file = (
                    fake_home / ".config" / "drbug" / "policies.toml"
                )
                self.assertTrue(policy_file.exists())
                text = policy_file.read_text(encoding="utf-8")
                self.assertIn("cross_dir_read", text)
                self.assertIn(str(self.outside), text)
                read_tool = await app._dispatcher.toolkit.get_tool("Read")
                self.assertEqual(read_tool.executions, 1)
                self.assertIn("add_global_rule", self._audit_text())

    async def test_modal_escape_denies(self) -> None:
        app, model = self._app(
            [
                self._read_script(),
                {"type": "text", "text": "读完了"},
            ]
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "读一下项目外那个文件")
            await _wait_until(
                pilot, lambda: app._awaiting_confirm, "进入等待确认"
            )
            await pilot.press("escape")
            await _wait_until(
                pilot, lambda: not app._agent_busy, "回复落定"
            )
            read_tool = await app._dispatcher.toolkit.get_tool("Read")
            self.assertEqual(read_tool.executions, 0)
            self.assertNotIn(self.payload, self._results_text(model))


class ImagePathGluedTest(unittest.TestCase):
    """TEAM1-fix1 c3: 路径粘连在中文里也要能认出图片（正则第二遍捞取）。"""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="tui-img-")
        self.addCleanup(temporary.cleanup)
        self.image = Path(temporary.name) / "shot.png"
        self.image.write_bytes(b"\x89PNG\r\n")

    def test_glued_between_chinese_characters(self) -> None:
        text = f"看这张图{self.image}是报错截图"
        self.assertTrue(_message_has_image_path(text))
        self.assertEqual(_image_paths_in_message(text), [self.image])

    def test_glued_with_chinese_punctuation(self) -> None:
        text = f"图在{self.image}，你看一下。"
        self.assertEqual(_image_paths_in_message(text), [self.image])

    def test_glued_nonexistent_path_never_counts(self) -> None:
        text = "看这张图/tmp/definitely-missing-12345.png是报错"
        self.assertFalse(_message_has_image_path(text))
        self.assertEqual(_image_paths_in_message(text), [])

    def test_spaced_and_glued_dedupes(self) -> None:
        text = f"{self.image} 再看这张{self.image}哦"
        self.assertEqual(_image_paths_in_message(text), [self.image])


class TeamCommandTest(unittest.IsolatedAsyncioTestCase):
    """TEAM1-fix1 c3: /team 列表（来源标注）/ 加人 / 删人确认回路。"""

    def setUp(self) -> None:
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        temporary = tempfile.TemporaryDirectory(prefix="tui-team-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        workspace = root / "proj"
        (workspace / "tiny").mkdir(parents=True)
        (workspace / "tiny" / "__init__.py").write_text(
            "", encoding="utf-8"
        )
        (workspace / "tiny" / "calc.py").write_text(
            "def add(left, right):\n    return left - right\n",
            encoding="utf-8",
        )
        (workspace / "tiny" / "util.py").write_text(
            "def shout(text):\n    return text.upper()\n", encoding="utf-8"
        )
        (workspace / "tests").mkdir()
        (workspace / "tests" / "test_calc.py").write_text(
            "from tiny.calc import add\n\n\n"
            "def test_add():\n"
            "    assert add(1, 1) == 2\n",
            encoding="utf-8",
        )
        (workspace / "pyproject.toml").write_text(
            "[project]\nname = \"team-fixture\"\n", encoding="utf-8"
        )
        self.workspace = workspace
        from core.autoprofile import (
            build_profile,
            default_profile_name,
            state_root_for,
        )

        build_profile(
            workspace,
            out_root=state_root_for(workspace) / "profiles",
            name=default_profile_name(workspace),
        )

    def _app(self) -> CifixChatApp:
        return CifixChatApp(
            model=FakeChatModel(fallback_reply="x"),
            workspace_root=self.workspace,
        )

    def _roster_names(self) -> list[str]:
        from core.roster import list_roster

        return [row["name"] for row in list_roster(self.workspace)]

    async def test_team_lists_roster_with_source_labels(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "/team")
            await _wait_until(
                pilot, lambda: "名册" in _log_text(app), "名册列表"
            )
            log_text = _log_text(app)
            self.assertIn("calc-specialist", log_text)
            self.assertIn("自动建档", log_text)

    async def test_team_remove_no_keeps_worker(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "/team remove calc-specialist")
            await _wait_until(
                pilot, lambda: app._awaiting_confirm, "进入等待确认"
            )
            self.assertIn("确认移除员工 calc-specialist", _log_text(app))
            self.assertIn("在等你答复", str(app._spinner_text(0)))
            await _submit_text(app, pilot, "N")
            await _wait_until(
                pilot, lambda: not app._awaiting_confirm, "确认结束"
            )
            self.assertIn("calc-specialist", self._roster_names())
            self.assertIn("已取消移除", _log_text(app))

    async def test_team_remove_yes_removes_and_records_overlay(
        self,
    ) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "/team remove calc-specialist")
            await _wait_until(
                pilot, lambda: app._awaiting_confirm, "进入等待确认"
            )
            await _submit_text(app, pilot, "Y")
            await _wait_until(
                pilot,
                lambda: "calc-specialist" not in self._roster_names(),
                "移除落盘",
            )
            from core.autoprofile import default_profile_name, state_root_for
            from core.roster import load_overlay

            profile_dir = (
                state_root_for(self.workspace)
                / "profiles"
                / default_profile_name(self.workspace)
            )
            overlay = load_overlay(profile_dir)
            self.assertEqual(
                [entry["id"] for entry in overlay["removed"]],
                ["calc-specialist"],
            )

    async def test_team_add_adds_with_boss_label(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "/team add util-pro tiny/util.py")
            await _wait_until(
                pilot,
                lambda: "util-pro" in self._roster_names(),
                "加人落盘",
            )
            from core.roster import list_roster

            rows = {
                row["name"]: row for row in list_roster(self.workspace)
            }
            self.assertEqual(rows["util-pro"]["source_label"], "老板添加")

    async def test_team_standby_shows_hint(self) -> None:
        bare_dir = tempfile.TemporaryDirectory(prefix="tui-team-bare-")
        self.addCleanup(bare_dir.cleanup)
        bare = Path(bare_dir.name).resolve()
        app = CifixChatApp(
            model=FakeChatModel(fallback_reply="x"), workspace_root=bare
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "/team")
            await _wait_until(
                pilot, lambda: "/team 未执行" in _log_text(app), "待命提示"
            )


class Perm2b2CommandTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        dispatcher.set_event_sink(None)
        self._tmp = tempfile.TemporaryDirectory(prefix="perm2b2-tui-")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        root = Path(self._tmp.name).resolve()
        self.workspace = root / "proj"
        self.workspace.mkdir(parents=True)
        (self.workspace / "pyproject.toml").write_text(
            "[project]\nname = \"perm2b2-fixture\"\n", encoding="utf-8"
        )
        self.outside = root / "outside.txt"
        self.outside.write_text(
            "perm2b2-payload\n", encoding="utf-8"
        )

    def _app(self) -> CifixChatApp:
        model = ScriptedToolModel([{"type": "text", "text": "你好"}])
        return CifixChatApp(model=model, workspace_root=self.workspace)

    async def _start(self, app: CifixChatApp, pilot) -> None:
        await pilot.pause()
        await _submit_text(app, pilot, "你好")
        await _wait_until(
            pilot, lambda: not app._agent_busy, "首轮回复落定"
        )

    async def test_mode_switch_rebuilds_dispatcher(self) -> None:
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._start(app, pilot)
            await _submit_text(app, pilot, "/mode auto-review")
            await _wait_until(
                pilot,
                lambda: "自动审核" in _log_text(app),
                "模式切换回显",
            )
            self.assertEqual(app._perm_mode, "auto-review")
            self.assertEqual(
                app._dispatcher._drbug_mode, "auto-review"
            )
            self.assertIn("自动审核", str(app.status_line()))

    async def test_permissions_list_and_revoke_session_rule(self) -> None:
        from core.policy import Rule

        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await self._start(app, pilot)
            engine = app._dispatcher._drbug_secretary_read._engine
            engine.add_session_rule(
                Rule(
                    "secretary", "cross_dir_read",
                    str(self.outside), "allow",
                )
            )
            await _submit_text(app, pilot, "/permissions")
            await _wait_until(
                pilot,
                lambda: str(self.outside) in _log_text(app),
                "规则清单显示",
            )
            self.assertIn("会话 1", _log_text(app))
            await _submit_text(
                app, pilot, "/permissions revoke 会话 1"
            )
            await _wait_until(
                pilot,
                lambda: "已撤销会话规则" in _log_text(app),
                "撤销回显",
            )
            self.assertEqual(engine.session_rules, [])

    async def test_pending_approve_executes(self) -> None:
        from core.pending import list_pending, record_pending

        record_pending(
            self.workspace,
            actor="secretary",
            action="cross_dir_read",
            target=str(self.outside),
        )
        app = self._app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "/pending")
            await _wait_until(
                pilot,
                lambda: str(self.outside) in _log_text(app),
                "待批准清单显示",
            )
            await _submit_text(app, pilot, "/pending approve 1")
            await _wait_until(
                pilot,
                lambda: "perm2b2-payload" in _log_text(app),
                "批准执行回显",
            )
            self.assertEqual(list_pending(self.workspace), [])

class MarketCommandTest(unittest.IsolatedAsyncioTestCase):
    """/market 在 TUI 里能列出扩展目录（只读浏览，不动真实配置）。"""

    def setUp(self) -> None:
        dispatcher.set_event_sink(None)
        self._tmp = tempfile.TemporaryDirectory(prefix="market-tui-")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        root = Path(self._tmp.name).resolve()
        self.workspace = root / "proj"
        self.workspace.mkdir(parents=True)
        (self.workspace / "pyproject.toml").write_text(
            "[project]\nname = \"market-fixture\"\n", encoding="utf-8"
        )

    async def test_market_lists_catalog(self) -> None:
        model = ScriptedToolModel([{"type": "text", "text": "你好"}])
        app = CifixChatApp(model=model, workspace_root=self.workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "你好")
            await _wait_until(
                pilot, lambda: not app._agent_busy, "首轮回复落定"
            )
            await _submit_text(app, pilot, "/market")
            await _wait_until(
                pilot,
                lambda: "repo-triage" in _log_text(app),
                "/market 列表回显",
            )
            self.assertIn("fs-tools", _log_text(app))
            self.assertIn("/market install", _log_text(app))

class McpAttachWiringTest(unittest.IsolatedAsyncioTestCase):
    """TUI 回复前会尝试给秘书挂 MCP 工具（替身验证接线，不碰真实配置）。"""

    def setUp(self) -> None:
        dispatcher.set_event_sink(None)
        self._tmp = tempfile.TemporaryDirectory(prefix="mcp-tui-")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(dispatcher.set_event_sink, None)
        self.addCleanup(dispatcher._bind_default_workspace, None)
        root = Path(self._tmp.name).resolve()
        self.workspace = root / "proj"
        self.workspace.mkdir(parents=True)
        (self.workspace / "pyproject.toml").write_text(
            "[project]\nname = \"mcp-fixture\"\n", encoding="utf-8"
        )

    async def test_respond_attaches_mcp_once(self) -> None:
        calls: list = []

        async def fake_attach(agent, workspace, **kwargs):
            calls.append((agent, workspace))
            return None

        original = dispatcher.attach_secretary_mcp
        dispatcher.attach_secretary_mcp = fake_attach
        self.addCleanup(
            setattr, dispatcher, "attach_secretary_mcp", original
        )
        model = ScriptedToolModel([{"type": "text", "text": "你好"}])
        app = CifixChatApp(model=model, workspace_root=self.workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _submit_text(app, pilot, "你好")
            await _wait_until(
                pilot, lambda: not app._agent_busy, "首轮回复落定"
            )
            self.assertEqual(len(calls), 1)
            await _submit_text(app, pilot, "你好")
            await _wait_until(
                pilot, lambda: not app._agent_busy, "第二轮回复落定"
            )
            self.assertEqual(len(calls), 1)

