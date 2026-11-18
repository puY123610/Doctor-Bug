# demo_repo2 — studyroom

一个用于陌生仓库试适配的迷你自习室预约仓库。源码采用 src 布局，包位于
`src/studyroom/`，测试位于仓库根目录的 `tests/`。

## 目录结构

```text
demo_repo2/
├── pytest.ini          # testpaths = tests, pythonpath = src
├── src/studyroom/
│   ├── __init__.py
│   ├── slots.py        # 时间段、时间解析与可用性计算
│   ├── conflicts.py    # 半开区间冲突判定
│   ├── bookings.py     # 预约、取消与房间台账
│   └── members.py      # 会员每日预约配额
└── tests/
    ├── test_slots.py
    ├── test_conflicts.py
    ├── test_bookings.py
    ├── test_members.py
    └── test_end_to_end.py
```

## 业务规则摘要

时间统一用“当天 00:00 起的分钟数”表示；对外用 `HH:MM` 展示。

- **半开区间**：预约区间为 `[start, end)`。前一场在 10:00 结束、后一场在
  10:00 开始时，两场互不冲突，可以背靠背预约。
- **房间独立**：每间自习室的预约相互独立、互不影响。A 房某时段被占用，
  不会让 B 房同一时段不可用；B 房的可用性也不应受 A 房预约或取消的影响。
- **取消立即释放**：取消预约立即释放该时段，之后其他会员可以预约同一房间
  的同一时段；已取消记录仍保留为历史，状态为 `cancelled`。
- **会员每日配额**：每名会员每天最多 3 次预约（按预约日期 `day` 计）。
  取消预约会释放该次配额；不同日期、不同会员分别计算。

## 运行测试

在本仓库根目录执行：

```bash
python -m pytest -q
```

只依赖标准库与 pytest。`pytest.ini` 里的 `pythonpath = src` 让测试可直接
`from studyroom... import ...`。

## 当前版本状态

当前版本在一次预约改造后出现回归，CI 有测试失败，见
[REGRESSION_NOTES.md](REGRESSION_NOTES.md)。
