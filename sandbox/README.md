# sandbox — scripted 全链的容器隔离小验证（SANDBOX-VAL）

本目录只服务一个主张的取证：**平台 scripted 全链可以在容器里改和跑，
改动不出工作副本，运行环境碰不到宿主机。** 它不改变平台内核
（`core/`、`acceptor/`、`kb/`、`tools/` 一行未动），也不覆盖真实模型档。

## 隔离契约

- 基底镜像固定 `python:3.12-slim`，只安装平台 `requirements.txt`。
- 构建上下文由 `build_image.sh` 显式装配：只带 `acceptor/`、`kb/`、
  `core/`、`demo_repo/`、`tools/run_mech.py`、`requirements.txt` 和本目录
  的 `c_arm_once.py`。`.env.local`、`model_config.json`、`.venv/`、
  `runs/`、缓存与 `.DS_Store` 从不进入上下文；装包前还会做一次文件名扫描。
- 镜像内 `/opt/platform` 是只读基线，旁边有构建时生成的
  `/opt/platform.sha256`；容器启动与运行结束各校验一次。
- 运行时固定使用：
  - `--read-only`：容器根文件系统只读；
  - `--tmpfs /work/platform`：唯一可写的代码/数据路径，属主 `sandbox`
    （uid 10001）；
  - `/dev/shm` 与 `/dev/mqueue` 也显式挂成只读 tmpfs（`mode=0555`），
    堵住 Docker 默认留下的内存盘写入点；
  - 非 root 用户 `sandbox` 运行；
  - 不挂载任何宿主机目录（`docker inspect` 的 `.Mounts` 为空）。
- `TMPDIR` / `HOME` 都指向 `/work/platform` 内，pytest 与临时目录也留在
  工作副本里，因此 `/tmp` 同样是只读的。
- 运行脚本内复制 `/opt/platform` → `/work/platform` 后才执行平台代码，
  容器内改动只发生在这一份副本里（tmpfs，容器退出即消失）。

## 文件

| 文件 | 作用 |
| --- | --- |
| `Dockerfile` | 固定基底、装 requirements、生成只读基线清单、非 root 用户 |
| `build_image.sh` | 装配白名单构建上下文、密钥文件名扫描、拉取并构建镜像 |
| `entrypoint.sh` | 容器入口：`chain` / `probe-write` / `probe-secrets` |
| `c_arm_once.py` | 在容器副本上跑一次 C 臂 scripted 修复，PASS 才退出 0 |
| `run_validation.sh` | 宿主机总控：哈希前后比对、两种网络配置、两个探针 |
| `logs/` | 本次运行保存的原始日志与哈希清单 |

## 运行

前置：Docker Desktop 已启动，本机可执行 `/usr/local/bin/docker`。

```bash
cd /Users/hostuser/Desktop/grok-bot/platform
./sandbox/run_validation.sh
```

脚本最后打印 `SANDBOX_VAL_ALL_CHECKS_DONE`；任何一项不符合预期都会
以非零退出。单独复跑某一档：

```bash
# scripted 全链（默认网络）
docker run --rm --read-only \
  --tmpfs /dev/shm:rw,noexec,nosuid,size=64m,mode=0555 \
  --tmpfs /dev/mqueue:rw,noexec,nosuid,size=64m,mode=0555 \
  --tmpfs /work/platform:rw,exec,size=1g,mode=0755,uid=10001,gid=10001 \
  sandbox-val:local chain

# 断网重跑
docker run --rm --read-only --network none \
  --tmpfs /dev/shm:rw,noexec,nosuid,size=64m,mode=0555 \
  --tmpfs /dev/mqueue:rw,noexec,nosuid,size=64m,mode=0555 \
  --tmpfs /work/platform:rw,exec,size=1g,mode=0755,uid=10001,gid=10001 \
  -e SANDBOX_NET_PROBE=1 sandbox-val:local chain

# 越界写 / 密钥可见性探针
docker run --rm --read-only \
  --tmpfs /dev/shm:rw,noexec,nosuid,size=64m,mode=0555 \
  --tmpfs /dev/mqueue:rw,noexec,nosuid,size=64m,mode=0555 \
  --tmpfs /work/platform:rw,exec,size=1g,mode=0755,uid=10001,gid=10001 \
  sandbox-val:local probe-write
docker run --rm --read-only \
  --tmpfs /dev/shm:rw,noexec,nosuid,size=64m,mode=0555 \
  --tmpfs /dev/mqueue:rw,noexec,nosuid,size=64m,mode=0555 \
  --tmpfs /work/platform:rw,exec,size=1g,mode=0755,uid=10001,gid=10001 \
  sandbox-val:local probe-secrets
```

## 边界（不要扩写）

- 只验证了 **scripted** 档；真实 LLM/AgentScope 运行需要出网，属于另一档
  配置，本目录没有验证，也不据此下结论。
- 这是 Docker 默认运行时的隔离，不是对抗性安全沙箱或多租户隔离；
  容器与 Docker Desktop Linux VM 共享内核。
- 镜像里带的是平台源码副本，不是宿主机工作区的挂载；需要更新验证对象时
  必须重新构建镜像。
- 结论与原始输出见 `ai/reports/SANDBOX-VAL.md`。
