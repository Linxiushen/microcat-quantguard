# MicroCat QuantGuard

**为 Agenthon 2026 · T1 Coding 开发的量化代码 Agent。** 用户已选择方案 A，以 MicroCat Solo 参赛。CodaBench T1 已获准；2026-09-20 已创建首个 Development 提交记录 **935007**，当前 **Submitting**，等待官方核验与调度。

QuantGuard 逐题读取说明与输入，调用赛事指定 House 模型生成 Python 程序，执行后校验交付文件。语法、执行或结构检查失败时，在同一资源预算内进行有限修复。它提交可运行的算法容器，解决评测时提供的新任务。

## 当前交付

| 项目 | 范围 |
|---|---|
| 运行入口 | `solve --task-dir /input --out /app/output`；镜像 interface 2.0 |
| 模型通路 | 仅注入的 House origin、model 和 bearer；固定请求形状，无外部模型回退 |
| 修复 | 默认最多 3 个候选，支持一次生成基线、关闭约束提示的消融 |
| 预算 | 最多 25 次请求、每次最多 4,000 输出 token、每题最多 1,000,000 输入 token；失败请求计入预算 |
| 输出 | 每题独立文件名，JSON/CSV/Parquet 等结构检查、有限数值、路径与总量检查；禁止评分器文件和污染标记 |
| 证据 | [本地单元测试](reports/unit-tests.xml)、[容器运行报告](reports/container-smoke.json)、[官方环境合约](IMPLEMENTATION-CONTRACT.md) |

**验证边界：** 测试使用合成 House HTTP 响应，验证真实代码执行、修复、隔离与结构检查。87 个公开任务的卡片和清单校验、任务上下文加载，也不等于解出了 87 题。v0.1.1 已完成首次 Development 上传；目前没有真实 House 解题成绩或官方榜分，官方团队绑定尚未核验。

## v0.1.1 发布与提交

| 项目 | 实际证据 |
|---|---|
| 固定源码 | `7f7e555decbf9b5cf7d96a7ba21396d429026ec6` |
| 原生 Linux CI | [成功运行](https://github.com/Linxiushen/microcat-quantguard/actions/runs/35502931967)：55 项通过、1 项跳过（未附带本地公开题库），10 个真实容器场景通过 |
| 本机单测 | [56 项通过](reports/v0.1.1-unit-tests.xml)，含 87 个任务上下文加载，非解题成绩 |
| 公开镜像 | `ghcr.io/linxiushen/microcat-quantguard@sha256:6034730e5d993a57e7e410002501e25f35dbead0baf6418b379f8ba214fd10a4` |
| 匿名可用性 | [manifest、config、全部层及完整拉取通过](reports/v0.1.1-public-image.json) |
| 开发投稿 | [T1 提交页](https://www.codabench.org/competitions/17765/#/participate-tab)：935007，Submitting，已用 1/20；非 Final |

[版本验收](reports/v0.1.1-release-validation.json)与[原生容器报告](reports/v0.1.1-ci-container-smoke.json)对应上述固定源码和镜像。此次原生 CI 的 Landlock、seccomp 和 NoNewPrivs 均实际启用；合成 House 响应只验证执行链路，不证明模型解题正确率。

## 构建与运行

```sh
docker build --platform linux/amd64 --target release -t microcat-quantguard:0.1.1 .
```

运行时由评测平台注入 `MODEL_ENDPOINT`、`MODEL_NAME`、`MODEL_TOKEN` 和代理变量。开发时使用自己的明确授权兼容端点；不要把这些变量写进镜像、报告或提交包。

```sh
mkdir -p out
chmod 777 out
docker run --rm --platform linux/amd64 \
  --read-only --user 65534:65534 --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  --pids-limit 256 --ulimit nofile=1024:1024 --ulimit nproc=256:256 \
  --cpus 2 --memory 2g --memory-swap 2g \
  -e MODEL_ENDPOINT -e MODEL_NAME -e MODEL_TOKEN \
  -e HTTP_PROXY -e HTTPS_PROXY -e NO_PROXY \
  -e QFBENCH_SEED=0 -e QFBENCH_NETWORK=restricted \
  --mount type=bind,src="/absolute/path/to/unit",dst=/input,readonly \
  --mount type=bind,src="$PWD/out",dst=/output \
  --mount type=bind,src="$PWD/out",dst=/app/output \
  microcat-quantguard:0.1.1 solve --task-dir /input --out /app/output
```

上面 2 CPU / 2 GiB 是小型本地验证配置；正式资源和时间按每题卡片与主办方执行环境。输出目录必须为空。没有有效模型配置会明确失败，不生成占位答案。成功时输出 `quantguard-audit.json`，其中 `official_score` 始终为 `null`；真正得分只能由官方评测给出。

## 本地复测

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/python -m pytest -q tests/test_house.py tests/test_task.py tests/test_validation.py tests/test_runtime_isolation.py
.venv/bin/python tools/container_smoke.py
```

`container_smoke.py` 启动一个只返回合成程序的本地测试服务，然后实际运行 Linux/amd64 镜像。测试 token 是固定无效字符串，无真实凭证。数值案例实际导入 NumPy、SciPy、Pandas、PyArrow 并计算结果。报告明确区分成功路径、修复路径与应拒绝的失败路径。

原生 macOS 的 `sandbox-exec` 启动兼容性尚未通过，所以本机执行候选代码统一使用上述 Docker 路径。`tests/test_cli.py` 保留给具备原生 Linux 内核隔离的环境；当前本机的对应端到端覆盖由容器报告承担。

## 执行边界

候选进程不继承模型凭证或代理变量，只读允许的输入，在私有工作目录生成产物，并受时间、内存、文件与进程限制。支持时启用 Landlock/seccomp。镜像显式设置容器兼容模式；只有 Linux、非 root、只读根文件系统、只读任务挂载条件满足时才允许使用它；候选进程自行设置 `no-new-privileges`，不依赖宿主提前设置。

兼容模式依赖外层容器边界，Python 路径审计是防错护栏，**不宣称能隔离任意恶意 Python 或原生扩展**。运行报告会记录实际模式。当前官方 CodaBench 文案与固定工具包对 gVisor/runc 的描述不同，正式环境仍需主办方确认及实际验收；本地 amd64 模拟速度也不能用作正式性能成绩。

## 后续验收与提交

1. 跟进提交 935007 的官方团队证明核验、调度与实际结果。网站报名、T1 准入、proof 本地通过和官方绑定分别记录。
2. 按真实评测反馈分析失败题型，再以相同模型和资源对照一次生成、有限修复与关闭约束版本。
3. 每次迭代固定源码与不可变镜像，并重新验收；当前版本已用 Development 1/20，当天 1/1，不重复上传。
4. Final 每赛道仅 1 次，留待充分验证后使用；当前没有 Final 提交或官方榜分。

## 来源与许可

官方公共工具包固定 `v2.4.3` / `03fc89cc666354e999768381bb923e60be5c1cee`；T1 资料固定 `3f6a7a6bd37c2fc0d59eb94b609f1098a42a9bc0`。来源与环境差异见 [合约说明](IMPLEMENTATION-CONTRACT.md)。

原创代码采用 MIT。`vendor/` 中官方数据、题目、检查器及工具包保留各自许可，不放入提交镜像，也不包含在本项目 Git 仓库中。
