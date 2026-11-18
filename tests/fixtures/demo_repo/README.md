# demo_repo — backoffice

一个用于演示与验收的迷你后台仓库：四个业务模块（鉴权、库存、计费、订单）加一个最小 sqlite 订单存储，配套一套 pytest 业务规则测试。仓库当前处于 v1.3 发版后的状态。

## 模块地图

```text
demo_repo/
├── pytest.ini
├── conftest.py
├── backoffice/
│   ├── __init__.py
│   ├── auth.py        # 用户、角色、退款权限
│   ├── inventory.py   # 库存台账：入库、可售量、预留、释放
│   ├── billing.py     # 计价：优惠券、税、总额（整数分）
│   ├── orders.py      # 结账、取消、退款；组合库存与存储
│   └── db.py          # sqlite3 :memory: 订单存储（save/get/update_status）
└── tests/
    ├── test_auth.py
    ├── test_inventory.py
    ├── test_billing.py
    └── test_orders.py
```

## 业务规则摘要

金额一律使用整数分（cents）。税率 6%，四舍五入使用 `decimal.ROUND_HALF_UP`。

- **鉴权**：`User(user_id, roles)`；`require_role(user, role)` 在用户缺少该角色时抛 `PermissionDenied`；`can_refund(user)` 当且仅当 roles 含 `finance` 时为 `True`（`support` 不可以）。
- **库存**：`Inventory` 提供 `add_stock`、`available`、`reserve`、`release`。`available = 总库存 − 已预留`；超量预留抛 `InsufficientStock` 且不改变状态；`release` 归还预留。
- **计费**：`price_order(lines, coupons)` 接收 `(sku, unit_price_cents, qty)` 列表与 `Coupon(kind, value)` 列表。`percent` 按小计计算并先作用，`fixed` 以分计并后作用；折扣总额不超过小计；税按（小计 − 折扣）的 6% 计算；`Bill` 含 `subtotal_cents`、`discount_cents`、`tax_cents`、`total_cents`。
- **订单**：`OrderService.checkout(user, cart, coupons)` 从内部 `PRICE_LIST` 取单价，逐行预留库存（任一行失败则释放此前已预留的行并抛错），计价后把订单写入 `Store`；`cancel(order_id)` 释放预留；`refund(user, order_id)` 先检查退款权限，通过后把订单标记为已退款。
- **存储**：`Store` 使用 sqlite3 `:memory:`，提供 `save`、`get`、`update_status`。

## 运行测试

在本仓库根目录执行：

```bash
python -m pytest -q
```

只依赖标准库与 pytest。

## 当前版本状态

当前为 **v1.3**（结账性能优化）。v1.3 发版后 CI 出现 8 个失败，见 [REGRESSION_NOTES.md](REGRESSION_NOTES.md)。
