# QuantGuard · Agenthon T1 implementation contract

本文件基于 2026-09-20 落地的官方源码。用户已选 A / QuantGuard / T1；CodaBench T1 已批准上传；作品和官方评测结果仍分别核验。本文件不记录 Team Key、真实 MODEL_TOKEN 或任何凭据。

## 1. 固定版本与已准备环境

| 项目 | 本地位置 / 固定值 |
|---|---|
| Shared toolkit | `vendor/Agenthon2026-public`，tag `v2.4.3`，commit `03fc89cc666354e999768381bb923e60be5c1cee` |
| T1 public kit | `vendor/track1-coding-public`，detached commit `3f6a7a6bd37c2fc0d59eb94b609f1098a42a9bc0` |
| Python | 项目内 `vendor/python/cpython-3.13.15-macos-aarch64-none/bin/python3.13` |
| 隔离环境 | `.venv`，Python 3.13.15；未更改全局 Python |
| 已实装 | `qfbench2-common==2.4.3`、`qfbench2-track-coding==3.1.0`，contract set `1.1.0` |
| 检查依赖 | numpy、pandas、scipy、pyarrow、scikit-learn、pytest、pytest-json-report、pytest-timeout；精确版本见 `requirements-official.lock.txt` |

`requirements-official.lock.txt`中的两个官方包已使用上述不可变commit的Git URL，shared包带`#subdirectory=common`，不依赖本机`file:///Users/...`路径。新checkout建立Python3.13的`.venv`后，在项目根执行`uv pip install --python .venv/bin/python -r requirements-official.txt`即可按锁文件安装。其余版本保留本轮官方checker环境的实测快照；QuantGuard应用依赖另行管理，不把后续环境变更误记为此快照已验证。

已先读取两个 `pyproject.toml`：分别为标准 hatchling 和 setuptools build backend，未声明自定义构建钩子。已安装官方声明的检查依赖；Harbor 是可选执行路径，本轮未安装。完整版金融计算依赖在 T1 `docker/requirements-sandbox.txt`，这不等于本轮宿主已安装全部金融库。

源版本与安装审查记录：`vendor/OFFICIAL-SOURCES.json`。不同页面存在历史版本文字：T1 README 仍称 tag 装出 2.4.2，但本次 tag 的 pyproject 和实际 distribution metadata 均为 **2.4.3**，采用实测值。

## 2. House model 接口

唯一获准模型披露为：

```json
{"name":"nvidia/nemotron-3-super-120b-a12b","version":"rl-030326-fp8","revision":"rl-030326-fp8","training_cutoff":"unpublished","access":"api"}
```

这只是 descriptor 披露。运行时 `model` 必须读取注入的 `MODEL_NAME`，不能把披露名称当成必然可调用的 route alias。

| 输入 | 必须遵守的用途 |
|---|---|
| `MODEL_ENDPOINT` | 注入的 route **origin**，形如 `scheme://host:port`，没有路径；保留其 scheme/host/port，不硬编码某个域名。 |
| API URL | `MODEL_ENDPOINT.rstrip('/') + '/v1/chat/completions'`，只允许 `POST`。遗漏 `/v1` 会被拒绝。 |
| `MODEL_TOKEN` | 每单元 bearer；请求头 `Authorization: Bearer <injected token>`。不得记录原值或把认证错误请求完整打印。 |
| `MODEL_NAME` | 请求 JSON 的 `model` 值。 |
| `HTTP_PROXY` / `HTTPS_PROXY` 及小写版本 | 保留官方注入值；正常 HTTP 客户端读取代理，不自行绕过或打印含认证信息的代理 URL。 |
| `QFBENCH_NETWORK` | 官方为 `restricted`，本地断网检查为 `none`。断网检查通过不证明 House 调用已成功。 |
| `QFBENCH_SEED` | 本地/官方注入时作为可复现随机种子来源；模型参数只使用端点支持的字段。 |

不调用外部 OpenAI / Anthropic / Gemini API；不启用模型服务端的 web、retrieval、code-execution tools。禁止自带神经模型与 LoRA。允许本地数值计算不意味着允许新增模型服务器。

如需关闭 House thinking，官方支持 `chat_template_kwargs.enable_thinking=false`；使用 Python SDK 时通过 `extra_body`。它不改变预算和模型披露。默认 thinking 开启。

## 3. 预算、截止和运行边界

- **最多 25 次 admitted generation requests / unit**；每次至多 **4,000 output tokens**，累计 **1,000,000 input tokens / unit**。不请求多个 alternatives。服务端已接收后即使失败或丢失响应仍扣额度；SDK自动重试也可能扣次数。客户端预算应保守计数，不能假设网络失败免费。
- 每题读取 `card.toml` 的 **`[agent].timeout_sec`**；公开87题当前为 1200–5400秒，1800最常见。`[verifier].timeout_sec` 和 `environment.build_timeout_sec` 是其他阶段，不能混用。
- 官方 unit clock 包含创建容器及必要拉镜像；House绝对窗口可能更短，重启不重置。执行相对时间规则的资料仍标注待部署核验，不编造新的运行时deadline变量。客户端以自己的起始时间和可见card预算保守留出保存产物时间。
- Development ingestion stage总计43,200秒，**不是每单元12小时**；最终Final资源另行公布。当前公开卡16 CPU quota、128GiB、GPU可用，但按每个实际card处理，不能硬编码设备可用。
- 非root用户、只读rootfs；`/input`只读；`/tmp`为64MiB且`noexec,nosuid,nodev`；仅output挂载可写。总output tree至多64MiB，PID/threads总计256，open files每进程1024。依赖/可执行文件必须预先装入镜像，运行时不联网安装。
- 交付必须 Linux/amd64，镜像 label 为 `qfbench2.interface_version="2.0"`。本次 `.venv` 是 macOS arm64，只验证本地工具与数据，不能代替Linux容器验收或官方性能。

## 4. 每题输入与真实输出

接口固定为：

```text
solve --task-dir /input --out /app/output
```

典型公开任务目录如下；具体题目可以没有某类数据，必须发现实际内容：

```text
units/<unit-id>/
  instruction.md
  card.toml
  manifest.json
  environment/
    Dockerfile
    data/                 # CSV/JSON/TSV/XML/HTML/XLSX/parquet/ZIP/Python等，不只表格
  checks/                 # 公开开发有；官方参与者运行时不能依赖它存在
    test.sh
    test_outputs.py
    ...
```

1. **正式实现必须不依赖 `checks/`。** starter pack有“整目录可读checks”的文字，但当前T1 scorer说明正式mounted tree剥离checks；公开开发时可读公开检查器帮助理解contract，私密hidden checks不获取。离线验收必须额外覆盖无checks的输入。
2. 不假设输入/输出都是parquet，也不固定交付文件名为`results.parquet`。每题以instruction和获准数据定义输出；文中`/app/data`在官方运行中对应`/input/environment/data`，不要访问不存在的宿主路径。
3. `[metadata].category`并不总是文档所列十个标准值；当前卡包含更多扩展/别名。未知类别应走通用流程，不能直接拒绝。
4. `/app/output`与`/output`在官方绑定到同一个目录；本地Docker验收也必须双挂载。只写真实task deliverables；**不自行伪造 `reward.json`、`reward.txt` 或 `pytest_report.json`**，这些由checker产生。
5. 不复制instruction、数据原文或含canary GUID的debug内容到output；题目的canary值也不能写进日志产物。失败时保留真实、格式合法的产物与受控诊断，不把空占位答卷冒称题目已解。
6. 限制输出树，不跟随逃出授权目录的路径/符号链接，不把临时执行工作区、凭据或整份输入归档成参赛产物。

## 5. 首轮10个公开代表任务

以下都已确认真实存在且card、manifest校验通过。它们用于覆盖不同输入结构与领域，**不是已解决列表，也不声称覆盖隐藏集**；不要按题名硬编码答案。路径均相对于项目根：

| 路径（`vendor/track1-coding-public/units/`之下） | 覆盖点 |
|---|---|
| `t1-EXAMPLE-bs-greeks-pde` | 期权与Greeks；parquet示例，仅此例官方提供确定性接口solver |
| `t1-zero-coupon-bootstrapping` | 利率曲线；JSON配置对象，不是DataFrame |
| `t1-credit-portfolio-var-cvar` | 信用组合VaR/CVaR；CSV与JSON组合 |
| `t1-binance-btc-participation-tca` | 交易执行与成本；quotes/trades CSV、订单/规则JSON |
| `t1-brinson-sector-attribution` | 绩效归因；sector/cash数据与参数 |
| `t1-corporate-action-adjustment` | 公司行动调整；价格CSV与事件JSON |
| `t1-13f-amendment-aware-crowding` | 申报修订与拥挤度；多TSV、多CSV |
| `t1-etf-overlap-redemption-pressure` | ETF重叠与赎回压力；多XLSX与CSV |
| `t1-polars-api-migration` | 代码迁移；Python旧接口与CSV |
| `t1-fomc-tone-event-study` | 文本/市场事件研究；语调词典JSON与CSV |

## 6. 官方本地核验命令与边界

以下命令从`agenthon-quantguard/`执行。`qfbench2 smoke`只检查已经存在的产物，**不运行Agent**。当前固定版本T1又要求组织者私有证据，因此本机实际返回**exit 2 / no local preview**；这是未评分，不是pass，不必伪造组织者上下文让它变绿。

```bash
.venv/bin/python -c 'from qfbench2_common.contracts import CONTRACT_SET; print(CONTRACT_SET)'
# 已实测 1.1.0

.venv/bin/qfbench2 smoke \
  vendor/track1-coding-public/units/t1-EXAMPLE-bs-greeks-pde \
  vendor/environment-check/exemplar-output --track coding
# 已实测 exit 2: This track has no local preview ... organizer input
```

数值正确性以当前T1 README的**isolated Linux checker route**进行；这些是下一步可执行命令，本环境代理未构建/运行Docker：

```bash
docker build --platform linux/amd64 \
  -t finance-bench-sandbox:quantguard-check \
  -f vendor/track1-coding-public/docker/sandbox.Dockerfile \
  vendor/track1-coding-public

# 先用自研QuantGuard镜像生成输出；先前从官方示例得到的输出不是QuantGuard成果。
mkdir -p vendor/environment-check/container-output
docker run --rm --platform linux/amd64 --network=none \
  -v "$PWD/vendor/track1-coding-public/units/t1-EXAMPLE-bs-greeks-pde:/input:ro" \
  -v "$PWD/vendor/environment-check/container-output:/app/output" \
  -v "$PWD/vendor/environment-check/container-output:/output" \
  YOUR_QUANTGUARD_IMAGE solve --task-dir /input --out /app/output

# checker与Agent进程分离；输出目录同时挂到两处。
docker run --rm --platform linux/amd64 --network=none \
  -e OUTPUT_DIR=/app/output -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD/vendor/track1-coding-public/units/t1-EXAMPLE-bs-greeks-pde:/input:ro" \
  -v "$PWD/vendor/environment-check/container-output:/app/output" \
  -v "$PWD/vendor/environment-check/container-output:/output" \
  finance-bench-sandbox:quantguard-check bash /input/checks/test.sh

.venv/bin/python -c 'import json,sys; r=json.load(open(sys.argv[1])); print(r); sys.exit(0 if r.get("reward")==1 else 1)' \
  vendor/environment-check/container-output/reward.json
```

`checks/test.sh`自己可能在检查失败时也exit0，必须读真实reward与pytest report。上面的简单Docker命令尚未施加完整平台nonroot/read-only/resource条件；正式验收再按固定toolkit `docs/DEVELOPMENT-RUNTIME.md`的设置执行。当前本地Docker约7.75GiB内存、10 CPU，低于公开卡128GiB/16 CPU，不能声称本地严格复现官方完整资源。

当前 CodaBench 页面给出的是较短启动示例；`SUBMISSION_CLI.md`还链接了 v2.4.3 runtime guide，后者明确非root、只读rootfs和no-new-privileges。公开仓库没有实际组织者launcher源代码，按完整runtime guide验收，并保留正式运行结果的独立边界。

## 7. 本轮实际验证结果

- Python与六个核心检查库成功import；toolkit/track distribution、scorer和contract版本回读成功。
- **87个官方公开unit的card及manifest均通过**，未修改官方题目或检查器。
- 官方`examples/exemplar_agent`成功生成单个示例的`results.parquet`。这是官方接口例子，**不是 QuantGuard或House运行成绩**。
- `qfbench2 smoke`实际返回exit2并说明T1无本地preview，未得到官方分数。
- 宿主直接跑该示例checker：4 passed、1 skipped、10 setup errors，均因缺少硬编码的`/input/environment/data/options.parquet`挂载。没有在宿主创建`/input`、没有修改检查器；完整数值验收留给Linux容器。
- 未调用真实House端点、未读取token或Team Key、未进行CodaBench上传、未启动收费资源。

完整本地事实记录在`vendor/environment-check/evidence.json`；安装锁文件为`requirements-official.lock.txt`。

## 8. 权利与提交状态

官方shared common与T1仓库有CC-BY-NC-4.0限制，逐题数据还可能有自己的许可。vendor作为本地开发资料，不将题库数据、参考材料或官方检查器混入自研公开镜像/仓库，不把其许可证改成项目代码许可证。镜像只包含合法运行依赖与自研Agent。

最终descriptor使用自己的immutable image digest、真实model disclosure及合法team proof。获准入场、proof绑定、Development上传、Final唯一提交分别核验；本文件不执行这些步骤。T1每日1次、Development共20次上传，held/cancelled也扣次数；Final每赛道仅一次。
