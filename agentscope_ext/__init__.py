# -*- coding: utf-8 -*-
"""本平台对 AgentScope 的离线扩展。

当前只包含自研的确定性 ChatModel 替身（``FakeChatModel``）及其配套的
最小 formatter，用于在完全离线的条件下把 AgentScope 的 ``Agent`` 接线跑通。

注意：替身只存在于开发树（测试用），不随产品包发货；所以这里的导入
必须容忍它缺席——产品包里 import 本包不能因为没有替身而失败。
"""

try:
    from .fake_model import FakeChatModel, FakeFormatter

    __all__ = ["FakeChatModel", "FakeFormatter"]
except ImportError:  # 产品包不含替身模块
    __all__ = []
