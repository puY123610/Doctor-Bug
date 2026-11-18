# Doctor Bug — 冻结验收与证据改派平台

一句话定位：**Doctor Bug 把「AI 改完代码后由谁验收、验收不过把哪条失败证据退回给谁」编排成一条可复现、可审计的调度流水线。**

它解决的问题很具体：让 AI 执行者改代码，最大的风险不是改得慢，而是「自己说改好了」。Doctor Bug 把判卷程序冻结在独立金库里，执行者碰不到；验收不过时，不接受口头解释，只读取真实失败用例，按证据把工单改派给对应模块的专家，再验、再派，直到 PASS 或达到轮次上限。

名称：对外名称 **Doctor Bug**（dr 即 Doctor）；命令名与 npm 包名都是 **`drbug`**。

形态：本地优先的命令行工具。先 `cd` 进你自己的项目目录再启动，就地检测、修复 bug；没有 Doctor Bug 自己的服务器，主流程不上传你的仓库。唯一的外发是调用你自己配置的模型厂商 API（请求里会带上相关代码与上下文，发给该厂商）。交互入口里的对话助手称为 **主治医生**。

---

## 1. 它解决什么问题

一条修复任务里有两个必须分开的角色：

- **谁验收**：验收标准在开工前冻结——测试文件、`conftest.py`、`pytest.ini` 被复制进 `acceptor/vault/` 并记录 sha256。执行者改业务代码可以，改测试判卷面会被判 `TAMPERED`，判卷程序本身也只使用金库里的冻结副本。
- **验收不过派谁修**：失败用例的 node id（如 `tests/test_billing.py::test_tax_on_discounted_total`）被还原成模块（`billing`），再查人可审计的知识库派单表，派给 `billing-specialist`；根因在别的模块时（例如 orders 的用例其实死在计费逻辑），由表里的节点覆盖规则改派。

这样，每一轮「为什么派给它」都有机器可读的证据链，而不是靠执行者自述。

---

## 2. 安装

> **npm 尚未正式发布。** 目前没有可以从 npm registry 直接安装的包，下面只写本地打包安装的现状。

### 2.1 本地打包安装（npm 包，未上架）

前置：Node.js 18+（npm 随 Node 提供）。在仓库根目录打包，再从本地文件安装。两种包都已用 `scripts/cleanroom.sh`（干净 HOME 里装包并跑命令探针）实测通过。

**普通版**：Python 不随包提供。首次运行会在 `~/.drbug/` 自举 Python 3.12 与依赖（优先用本机 3.11+；没有时下载固定版本 python-build-standalone 并校验 sha256）。

```bash
cd npm
npm pack                         # 产出 drbug-0.1.0.tgz
npm i -g ./drbug-0.1.0.tgz      # 或 npm i -g --prefix <目录> ./drbug-0.1.0.tgz
```

**环境打包版**：包内自带 Python 运行时和预装依赖（`platform/runtime`），首次运行不需要再准备环境。目前只为 macOS arm64 固定了运行时资产，其他平台运行打包脚本会直接报错退出。

```bash
bash npm/build_bundled.sh        # 在仓库根目录执行
# 产出 npm/dist-bundled/drbug-bundled-<版本>-<平台>-<架构>.tgz
npm i -g ./npm/dist-bundled/drbug-bundled-*.tgz
```

装好后：

```bash
cd ~/your-own-project
drbug check          # 环境体检；普通版首次运行会准备 ~/.drbug，仅此一次
drbug                # 全屏交互页（直接运行，无需子命令）
```

启动器只做三件事：定位 npm 包内的 `platform/`、准备 Python 环境（普通版自举 `~/.drbug/venv`，环境打包版直接用包内运行时）、以用户当前目录为 cwd 执行 `python -m core.cli`；参数与退出码原样透传。

### 2.2 源码开发

前置：Python 3.11+（AgentScope 2.0.11 要求 `>=3.11`；本项目在 3.12.16 上实测），macOS/Linux shell。第 1 步按平台选 Python：

- **macOS**：用 Homebrew 的 python3.12（`brew install python@3.12`），不要用系统自带 python3。
- **Linux**：用本机 python3.11 或更高版本，先用 `python3 --version` 确认。

```bash
cd platform                      # 仓库根目录

# 1) 建项目内虚拟环境（按上面的平台说明二选一）
/opt/homebrew/bin/python3.12 -m venv .venv    # macOS（Homebrew）
python3 -m venv .venv                         # Linux（本机 python3.11+）

# 2) 只凭 requirements.txt 安装依赖
.venv/bin/python -m pip install -r requirements.txt

# 3) 环境体检：最后一行应为「体检结论: 通过」
./drbug check
```

`check` 会验证 Python / AgentScope 2.0.11 基线、模型配置与验收金库。**还没配置模型时，「模型配置」一栏会显示未配置与首次填写指引，但体检结论仍可为「通过」**——环境基线与金库才是硬门槛，与模型无关。

`./drbug` 是仓库自带的垫片脚本，会调用同目录 `.venv/bin/python -m core.cli`，并保留你执行命令时的当前目录；未 `pip install` 也能用。

---

## 3. 用法

### 3.1 首启配置引导

第一次打开 `drbug` 全屏交互页时，如果还没有可用的模型配置，会先弹出配置引导：选模型厂商、填模型名、填你自己的 API key。也可以在命令行单独跑 `drbug setup`。

- 厂商列表共 8 家，DeepSeek 排第一，也是唯一经真实 key 端到端验证过的一家；其余 7 家已接线但**未验证**，引导里会如实标出，而且要你自己填模型名（不替未验证的厂商编默认模型）。
- 非密钥项（厂商、模型、密钥变量名）写入 `~/.config/drbug/model_config.json`；密钥值只写入 `~/.config/drbug/.env`（权限 0600），不会写进 JSON。
- 引导没配成就关掉，交互页会直接退出，而不是带着不可用的模型继续。

### 3.2 进工作区先问信任

每次进入一个目录作为工作区之前，都会先问「把这个目录作为工作区？」：

- **每次都问**：同意只在本次会话内有效，下次打开重新问；首页「最近工作区」列表只是访问历史，从那里进入也照样先问。
- **拒绝不进入**：启动时拒绝就不进入这个目录；`/cd` 时拒绝则取消切换。
- **不记住**：信任状态不写入任何文件。

在非项目目录打开时，主治医生先待命，并提示你到项目目录下打开或用 `/cd <项目路径>` 切换（同样先问信任）。

### 3.3 检查—修复—交付

打开交互页（`cd` 进项目目录后直接运行 `drbug`）后的典型流程：

1. **检查**：`/check` 做平台体检；`/status` 看版本、模型、密钥状态（只报是否已设置）、工作区、权限模式、外接工具与市场。
2. **建档与复现**：`/profile` 为当前项目自动建档（零模型调用）；没有测试的仓库用 `/repro` 先做复现分诊（不冻结，症状经你确认后才冻结）。
3. **修复**：`/run` 跑一轮修复。真实模型运行会消耗 API 额度，不带 `--yes` 只提示、不执行；确认后输入 `/run --arm C --yes`。TUI 默认修复轮上限 3，可用 `--max-rounds N` 调整。
4. **看结果**：运行结束后对话区显示验收结论，并追加本轮补丁的 diff；之后可用 `/diff` 重显（只读），`/runs` 查看历次运行记录。
5. **交付**：结果包落在 `<项目>/.drbug/` 下。交互页只展示补丁、不替你合入；确认要用时，在命令行用 `drbug apply <结果包目录> --workspace <原仓库目录>` 把补丁应用回原仓库（基线指纹防覆盖，可先加 `--dry-run` 只校验与预览），用 `drbug adopt` 记录采纳或拒绝决定（只记录，不合入）。

直接输入自然语言则交给 **主治医生**：它只能通过平台工具（`platform_check`、`run_repair`、`propose_repro`、`freeze_repro`、`accept_check`、`show_run`、`kb_explain` 等）操作平台，汇报以工具返回为准，不编造验收结论。主治医生与 `/run` 都需要模型配置；`/check`、`/accept` 等本地命令不需要。会话可用 `/save` 立刻存档，用 `/sessions` 恢复、重命名或删除本工作区的存档。

### 3.4 斜杠命令

斜杠命令由本地代码直接执行（不经过主治医生）。下表与交互页 `/help` 同源（`core/tui.py` 的 `COMMANDS`），顺序一致：

在输入框打「/」会弹出命令列表：继续打字按前缀过滤，用 ↑↓ 或鼠标单击选择，回车把命令名填入输入框（带参数的命令自动带一个空格，接着就能打参数），填入后再按回车才提交；Esc 关闭列表。

| 命令 | 作用 |
| --- | --- |
| `/check` | 平台体检（环境基线 + 模型配置 + 验收引擎） |
| `/run [--arm A\|B\|C] [--max-rounds N] [--workspace 路径] [--yes]` | 跑一轮修复；真实模型必须加 `--yes` 确认 |
| `/accept` | 冻结验收（默认当前工作区） |
| `/profile` | 自动建档（零模型、零费用） |
| `/repro` | 复现测试分诊（不冻结） |
| `/model` | 打开模型选择器（切换已配置 profile） |
| `/cd <路径>` | 切换会话工作区到指定目录（先问信任） |
| `/workspace` | 列出、纳入或移出本次会话已纳管目录 |
| `/team` | 查看名册、添加或移除员工 |
| `/brief` | 查看项目简报 |
| `/permissions` | 查看权限规则、撤销会话或文件规则 |
| `/mode` | 查看或切换权限模式（逐次确认/自动改/自动审核） |
| `/pending` | 查看待批准清单、批准并重新执行或丢弃 |
| `/runs` | 查看历次运行记录与详情 |
| `/diff` | 重显最近一轮运行的补丁（只读） |
| `/copy` | 复制上一条主治医生回复的原文到系统剪贴板 |
| `/sessions` | 会话列表：恢复、重命名或删除本工作区存档 |
| `/save` | 立刻存档当前会话 |
| `/market` | 浏览扩展市场、安装/卸载扩展 |
| `/status` | 状态总览（版本、模型、密钥、工作区、模式、外接工具、市场） |
| `/mcp` | 外接工具服务器清单与状态 |
| `/help` | 命令清单 |
| `/quit`（`/exit`） | 退出 |

鼠标：用鼠标拖过回复区的文字，松手就自动复制到系统粘贴板（再按 ⌘V 粘贴到别处）；点回复里的链接，http/https 的会在默认浏览器打开，本地路径的链接不打开、只给一行提示，file:、javascript: 这类危险写法根本不会显示成链接，点它什么都不发生；点普通文字什么都不发生。想复制上一条回复的原文，用 `/copy`。

### 3.5 批命令

```bash
drbug check                                                # 环境体检
drbug setup                                                # 首次配置向导
drbug run --arm C                                          # 修复当前项目；默认 profile=auto
drbug run --arm C --workspace <项目路径>                    # 修复指定项目
drbug accept                                               # 只验收当前目录
drbug profile                                              # 为当前项目自动建档
drbug repro --symptom "描述了什么问题"                     # 无测试仓库先做复现分诊
drbug apply <结果包目录> --workspace <原仓库目录> [--dry-run]  # 把补丁应用回原仓库
drbug adopt <结果包目录> --yes|--no [--note 备注]           # 记录采纳/拒绝决定
```

其余子命令：`model`（列出/切换模型 profile）、`agents`、`team`、`brief`、`runs`、`mcp`、`market`、`policy`、`js-inventory`，用 `drbug <子命令> -h` 查看参数。

- `check`：环境基线 + 模型配置来源（密钥只报「已设置/未设置」）+ 验收金库文件数；缺配置时打印一步到位的 BYOK 指引。
- `run`：在状态目录的 `runs/work/` 下复制一份私有工作副本，实时打印每轮验收、派单、专家完成情况，记录写入 JSON。退出码 0 仅当 `PASS`。省略 `--workspace` 时用当前目录；外部项目默认 `--profile auto`（先零模型自动建档）。
- `accept`：对指定工作区执行冻结验收，打印 `PASS/FAIL/TAMPERED` 与通过/失败计数；工作区尚无冻结档案时会明确提示先建档。
- 状态目录：平台自身的开发运行写平台的 `profiles/`、`runs/`；对外部项目运行时改写 `<项目>/.drbug/{profiles,runs,bundles}/`，不会污染你的源码树。

修复轮次由 `max_rounds` 限制（CLI 默认 6，TUI/调度工具默认 3；用户配置可统一覆盖，见下节）。后端会真实调用模型，运行结束打印 Token 用量（输入/输出/缓存/调用次数）。

### 3.6 执行后端

产品只用一种后端：平台原生 AgentScope 2.0.11 Agent（工具为 `Read`/`Write`/`Edit`/`Glob`/只读 `Shell`，写操作受政策门禁与工作副本限制），经 `model_config.json` 配置的真实模型执行修复。没有脚本演员，也没有演示后端：跑起来就是真修复。开发测试在仓库内以注入替身模型离线验证机制，那是测试方法，不随产品提供。运行时 CLI 会醒目提示「将调用真实模型、消耗 API 额度」，TUI 还要求 `--yes` 二次确认，工具层也会拦截未确认的付费运行。

---

## 4. 模型配置（BYOK）

用户级配置放在 `~/.config/drbug/`，与项目目录无关：

```bash
mkdir -p ~/.config/drbug
```

`~/.config/drbug/model_config.json`（非密钥）：

```json
{
  "provider": "deepseek",
  "model": "deepseek-flash",
  "api_key_env": "DEEPSEEK_API_KEY",
  "max_rounds": 6
}
```

`~/.config/drbug/.env`（密钥，权限建议 `chmod 600`）：

```text
DEEPSEEK_API_KEY=你的密钥
```

- 配置优先级（从高到低）：进程环境变量 > 用户级 `~/.config/drbug/model_config.json` > 平台目录 `model_config.json`（旧配置兼容）> 默认值；JSON 与旧文件按字段合并，用户级覆盖平台级。
- 密钥文件优先级：进程环境变量 > `~/.config/drbug/.env` > 平台目录 `.env.local`（旧配置兼容）；先定义者生效，绝不被后读文件覆盖。
- `drbug check` 会打印「配置来源」与各文件是否存在，但永远只报密钥「已设置/未设置」，不显示值。
- 首次运行未配置时，`drbug`（无参数）与 `drbug check` 会打印一步到位的填写指引，不会静默失败。
- `.gitignore` 已排除平台内的 `model_config.json` 与 `.env.local`；用户级目录不在仓库内，也请勿把密钥提交到任何仓库。
- 密钥只属于使用者本人，请自行申请与保管。
- 已接线 8 家厂商：DeepSeek、Anthropic、DashScope、MiniMax、Moonshot、OpenAI Chat、OpenAI Responses、Volcengine。**只有 DeepSeek 经真实 key 端到端验证；其余 7 家未验证**，能否跑通、效果如何都没有实测结论。扩展点在 `core/model_config.py::build_model`。
- 可以配置多个模型 profile，用 `drbug model` 或交互页 `/model` 切换。

> 旧入口 `python -m core run ...` 不会自动加载这些配置文件；使用 `agentscope` 后端请优先走 `drbug run`。

---

## 5. 核心机制四件套

### 5.1 冻结验收器（`acceptor/`）

判卷面只有三类文件：`tests/test_*.py`、仓库根 `conftest.py`、`pytest.ini`。它们被复制进 `acceptor/vault/`，并生成逐文件 sha256 的 `manifest.json`。

验收时先比对工作区判卷面：内容不同、文件缺失、或凭空多出 `tests/test_*.py`，一律判 `TAMPERED`，且**不运行任何测试代码**。未被篡改时，只取工作区的 `backoffice/` 业务代码，测试与配置全部使用金库冻结副本，在临时目录里用 `python -m pytest -q` 判卷，输出结构化的 `Verdict`（状态、失败 node id、通过/失败计数）。判卷程序与执行者工作区物理分离，执行者改测试骗不过去。

### 5.2 失败证据（`acceptor/evidence.py`）

每次验收的失败用例 node id 会被还原成模块：

```text
tests/test_billing.py::test_tax_on_discounted_total  ->  billing
tests/test_orders.py::test_checkout_total_with_coupon ->  orders
```

这份「node id → 模块」的映射就是派单的原始证据；它来自真实 pytest 输出，不来自执行者自述。

### 5.3 知识库派单表（`kb/dispatch_table.json`）

一张人可审计的 JSON：

- `specialists`：专家清单与职责边界（计费、库存、鉴权、数据库、通用修理工）。
- `module_defaults`：模块默认派给谁（`billing` → `billing-specialist` 等）。
- `node_overrides`：单条用例的改派规则，附 `rationale` 与 `source`（例如 orders 的用券用例根因在计费，派给计费专家）。
- `fallback`：无明确归因时兜底给 `generalist`。

改派规则可以直接读、可以评审、可以版本化，不需要读代码才能知道「为什么派给它」。

### 5.4 调度循环（`core/runner.py`）

```text
复制工作区到 runs/work/<臂>-<kb|nokb>-<随机后缀>
  -> 冻结验收（round 0，首检）
  -> PASS：结束；TAMPERED：立即结束
  -> FAIL 且未到 max_rounds：按策略臂派单 -> 演员修复 -> 再验收
  -> 轮次耗尽仍 FAIL：CAPPED
```

`max_rounds` 是**修复轮**上限，首检不算修复轮；因此首检即篡改时修复轮为 0。三种策略臂：A 每轮都把全部失败交给同一个通用执行者；B 首轮通用、之后按文件自报改派；C 按冻结验收证据查知识库派单。

---

## 6. 平台能力

对照多智能体编排平台的六项能力要求，本平台的落地情况如下（每项都说清做到什么程度，不夸大）：

1. **多智能体编排、调度、执行与验证**：主治医生负责理解需求与调度，员工按负责的文件分工修复，调度循环「验收 → 按失败证据派单 → 修复 → 再验收」自动运转，验收由定下来的检查程序判卷（见第 1、5 节）。
2. **Agent 的动态注册、发现与版本管理**：员工可经 `/team` 与主治医生增删；每个员工的整套定义（身份、角色、负责的文件、指令、工具集，载体是档案与名册）有版本号，改一次加一版，变更前存整套快照；回滚时整套定义真恢复，之后的派活按旧版走。员工的运行记忆是积累，不随定义版本回滚。
3. **证据强度**：每轮验收出成绩单，数字带分子分母（目标修复率：定下来的目标里修好几个/共几个；回归保持率：原来好的还保持几个/共几个）；失败分四类写清——真报错、没跑起来、测试本身写错、警告（不算数）；一个问题都没查出来时，成绩单明写「目前没有检测出来问题」，不拿空白冒充全过。
4. **指令段变体（按失败类型自适应）**：失败先分四类——断言错、导入失败、超时、其他异常；派活时自动给员工加一段对症的附加说明（如导入失败先查缺哪个包），运行记录里记明这一轮用了哪一段，方便复盘。
5. **按负载调度**：同一条失败有多个能派的人时，只派本次运行里累计被派次数最少的那一个，不再一拥而上；这一轮没修好，下一轮按账本自然换人。多份并行尝试模式保持原样（每份都全派）。
6. **子工作流嵌套**：一轮修复已经抽成一步可独立调用的子流程，主流程每轮调用它，父子记录互相挂钩（父记录能找到每一轮的子记录，子记录标明自己是第几轮、第几层）；嵌套层数上限为 1，只作防失控的保险。

---

## 7. 已验证记录（截至 2026-11-18）

**当前状态（2026-11-18）**：平台自身测试全套 1087 条全绿（skipped=1）。第 6 节六项能力均已落地并各自通过验收，每项都有对应的测试模块在全套里。真实陌生项目验证：开源项目 more-itertools（取其一个真实修复提交、回退成只有 1 条测试失败的考题）建档与首验准确报出 767 passed / 1 failed。

机制验证（开发期记录）：调度/验收机制曾以四组夹具考题（coupled 耦合、honesty 诚实、tamper 防篡改、kb_ablation 知识库消融）在脚本演员下复跑——coupled 只有 C 臂 1 轮 PASS、tamper 首检即 TAMPERED、消融后 C 臂 CAPPED。该自检入口已随产品定版移除，夹具与演员只留在开发测试里，原始运行记录只留在开发仓库、不随本仓库发布。真实模型验证记录如下：

**EXP1（`llm` 后端，真实 OpenCode/DeepSeek）**：C 臂 `PASS`，3 个修复轮，最终工作副本复验 24 passed / 0 failed，diff 恰为三行正确修复。如实记录一次越界事故：第 2 轮有专家 `cd` 进夹具源目录 `demo_repo` 就地修改，导致夹具被污染；已用运行前备份恢复，PASS 结论本身不受影响，沙箱缺失列为已知限制。

**EXP2（`agentscope` 原生后端）**：C 臂 `PASS`，1 个修复轮，总耗时约 27.5 秒；最终工作副本复验 PASS，`demo_repo` 本体未被改动。

诚实边界：

- 四考题使用 `scripted` 演员，证明的是**调度/验收机制**成立，不是真实模型能力。
- 真实模型的 EXP1、EXP2 **各只跑了 1 次**，是现象记录，不是统计结论，不能据此比较后端或估计成功率。
- honesty 三臂全过是预期行为（简单任务不该被冤枉），**不作为创新证据**。

---

## 8. 已知限制

1. **开发期的外部子进程后端没有文件沙箱**：EXP1 期间的 `llm` 后端是外部 CLI 子进程，「只在工作区内行动」只是提示词软约束，曾实际越界改动夹具源目录（已恢复）。该后端已随产品定版移除；现行原生后端先把工作区复制到工作副本再跑，写操作受门禁限制（见下一条）。
2. **写操作限工作副本内，负责范围由权限门禁强制**：`agentscope` 后端用 `DONT_ASK` + `working_directories` 把写操作限制在工作副本内，越界写会被拒绝。每个员工只能改自己负责的文件（辖区），这条由权限门禁强制执行、不是靠提示词自觉：越界写直接拒绝；确需改负责范围外的文件时，走扩权申请、批准后才放行（扩权通道已在真实模型运行中验证过；员工硬撞围栏被拒的通道目前只有单元测试覆盖）。工作区内改测试会被定版验收判 `TAMPERED` 兜底，但不是物理阻止；工作区外的**读**仍放行（敏感路径如 `.env` 内容除外，只可见文件名、读不到内容）。
3. **测试夹具是教学规模数据**：`tests/fixtures/` 下的样本仓库只有 4 个业务模块、24 条测试、8 条失败，适合验证机制，不适合做性能或规模结论。
4. **真实运行样本极少**：EXP1、EXP2 各 1 次；耗时、轮次受任务规模与模型状态影响，只作现象记录。
5. **模型供应商只深度验证了 DeepSeek**：其余供应商已接线，但未逐个端到端实测。
6. **CLI 配色只在真实 TTY 生效**：重定向输出时 `drbug` 走纯文本降级，信息一致但无 rich 样式；TUI 本身需要交互终端。
7. **员工记忆效果没有证据**：项目员工档案里有 `MEMORY.md`（`drbug agents` 可列出），但两次对照实验都没看到记忆对修复效果的影响，本项目不宣称它能提升修复效果。
8. **npm 尚未正式发布**：只能本地打包安装（见第 2 节）；环境打包版目前只为 macOS arm64 固定了运行时，Windows 未适配、未验证。

---

## 9. 目录地图

本仓库是公开发布快照，只含产品代码与平台自身测试；开发过程文件（任务单、报告、运行记录、平台内档案）不在其中。

```text
platform/
├── README.md                  # 本文件
├── drbug                      # CLI 垫片：调用同目录 .venv 的 core.cli
├── pyproject.toml             # 包元数据与 drbug 入口点
├── requirements.txt           # AgentScope / pytest / rich / textual
├── model_config.example.json  # 模型配置示例（非密钥）
├── acceptor/                  # 冻结验收器：manifest、判决与证据（金库随各项目档案走）
├── agentscope_ext/            # 离线测试替身模型（只用于测试）
├── core/                      # 调度内核：runner、策略臂、CLI、TUI、主治医生
├── kb/                        # 派单表样本（dispatch_table*.json）与「证据 -> 专家」查表实现（lookup.py）
├── market/                    # 扩展市场：目录（catalog.json）与技能包（skills/）
├── npm/                       # npm 启动器：bin/drbug.js + prepack.js（普通版）+ build_bundled.sh（环境打包版）
├── sandbox/                   # Docker 沙箱验证：镜像、入口探针与校验脚本（不含运行日志）
├── scripts/                   # cleanroom.sh：干净环境装包验收脚本与命令探针
├── tests/                     # 平台自身 unittest（含 TUI 无头测试与打包测试）
│   └── fixtures/              # 测试夹具：埋了 bug 的样本仓库与冻结金库副本
└── tools/
    └── env_check.py           # 环境基线检查
```
