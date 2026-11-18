# demo_repo4 — durlib

一个平铺包结构的迷你时长与单位换算库，只依赖标准库。源码位于仓库根目录的
`durlib/`。

## 目录结构

```text
demo_repo4/
└── durlib/
    ├── __init__.py
    ├── parse.py        # 时长字符串 -> 秒数
    ├── convert.py      # h / m / s 之间的 60 进制换算
    └── format.py       # 秒数 -> 时长字符串
```

## 预期行为（事实基准）

### 解析：`parse_duration(text) -> int`

把形如 `<数字><单位>` 的片段串联解析为**整秒数**，单位只允许小写 `h`、`m`、`s`：

```python
parse_duration("1h30m") == 5400
parse_duration("90m") == 5400
parse_duration("45s") == 45
parse_duration("1h30m15s") == 5415
parse_duration("2h") == 7200
```

单位片段必须连续、不可重复；空串、缺单位、未知单位、重复单位都抛出
`ValueError`。

### 换算：`convert(value, from_unit, to_unit) -> float`

在 `h`、`m`、`s` 之间按 60 进制换算：`1h == 60m == 3600s`。

```python
convert(2, "h", "m") == 120.0
convert(90, "m", "h") == 1.5
convert(1, "h", "s") == 3600.0
convert(45, "s", "m") == 0.75
```

`to_seconds(hours=0, minutes=0, seconds=0)` 把三个分量合成秒数；
`from_seconds(total_seconds)` 把非负秒数拆成 `(hours, minutes, seconds)` 三元组。
未知单位抛出 `ValueError`，负秒数传给 `from_seconds` 也抛出 `ValueError`。

### 格式化：`format_duration(total_seconds) -> str`

把秒数格式化为由 `h`、`m`、`s` 组成的紧凑字符串，只输出非零部分，
全零输出 `"0s"`：

```python
format_duration(5400) == "1h30m"
format_duration(3600) == "1h"
format_duration(90) == "1m30s"
format_duration(3661) == "1h1m1s"
format_duration(60) == "1m"
format_duration(0) == "0s"
```

负秒数抛出 `ValueError`。

## 使用示例

```python
from durlib import convert, format_duration, parse_duration

total = parse_duration("1h30m")      # 5400
total == 5400
format_duration(total)               # "1h30m"
convert(total, "s", "m")             # 90.0
```
