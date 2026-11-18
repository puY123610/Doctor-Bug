# demo_repo3 — todolib

一个平铺包结构的迷你命令行任务清单库，用于练习按业务规则读写 Python 仓库。
源码位于仓库根目录的 `todolib/`，测试位于 `tests/`。只依赖标准库与 pytest。

## 目录结构

```text
demo_repo3/
├── pytest.ini          # testpaths = tests, pythonpath = .
├── todolib/
│   ├── __init__.py
│   ├── models.py       # Task 数据模型与字段校验
│   ├── dates.py        # 到期日解析
│   ├── tags.py         # 标签比较
│   └── queries.py      # 排序与筛选
└── tests/
    ├── test_models.py
    ├── test_dates.py
    ├── test_tags.py
    └── test_queries.py
```

## 业务规则

### Task

`Task(id, title, priority, created, due=None, tags=(), done=False)`：

- `priority` 为正整数，`1` 表示最高优先级，数字越大优先级越低；
- `created` 是创建序号（非负整数），越小表示创建越早；
- `title` 不能为空或全空白；
- `tags` 保留调用方给出的显示大小写（`Task(tags=["Work"]).tags == ("Work",)`）；
- `done` 默认为 `False`。

### 排序

`sort_tasks(tasks)` 返回**新列表**，不修改入参：

- 先按 `priority` 升序（`1` 在最前）；
- 优先级相同时按 `created` 升序（创建早的在前），与入参顺序无关；
- 该次序是稳定、确定的：同一批任务无论以什么顺序传入，输出次序都一致。

### 到期日

`parse_due(text)` 接受两种格式，返回 `datetime.date`：

- `YYYY-MM-DD`，例如 `2026-12-31`；
- `DD/MM/YYYY`（**日在前、月在后**），例如 `03/04/2026` 表示 2026 年 4 月 3 日，
  `25/12/2026` 表示 2026 年 12 月 25 日。

输入两端空白会被忽略；其他格式或非法日期抛出 `ValueError`。

### 标签

- 标签比较**大小写不敏感**，并忽略标签两端的空白：
  `has_tag(Task(tags=["Work"]), "work")` 为 `True`，
  `has_tag(Task(tags=["Work"]), "  WORK  ")` 也为 `True`；
- 不存在的标签返回 `False`。

### 列表查询

`list_tasks(tasks, tag=None, include_done=False)`：

- 默认隐藏 `done=True` 的任务，`include_done=True` 时一并返回；
- `tag` 不为 `None` 时只保留带该标签的任务，匹配规则与 `has_tag` 相同
  （大小写不敏感）；
- 结果始终按上面的排序规则返回。

## 运行测试

在本仓库根目录执行：

```bash
python -m pytest -q
```

`pytest.ini` 里的 `pythonpath = .` 让测试可以直接 `from todolib... import ...`。
