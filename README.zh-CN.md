# SheetBenchKit

给 AI 表格助手使用的契约回归测试工具。

[English](README.md) · [自定义案例教程](docs/custom-cases.md) · [报告预览](docs/report-preview.md)

SheetBenchKit 先冻结表格输入、明确的计算契约和已确认的预期答案，再离线评分保存下来的适配器输出。它分别检查数值、单位、拒答原因和声明的数据来源。通过修改目标数据与干扰数据组成的三元组，还能检查输出是否按预期变化。

内置数据包含 **30 个合成输入：23 个 VALUE、7 个 ABSTAIN**，其中有四组三元组。这些是回归测试样例，不代表 30 个独立任务，也不是真实模型能力榜单。公开的标准答案可以直接查看，因此不提供防作弊保证。

## 从零开始

先安装 Git 和 Python 3.12 或 3.13。下面的命令从尚未包含 `sheetbenchkit` 仓库的目录开始执行。安装时可能下载 Python 依赖；演示本身不调用模型，也不需要 API Key。

### macOS / Linux

```sh
git clone https://github.com/xwj123456/sheetbenchkit.git
cd sheetbenchkit
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

使用 Python 3.13 时，将 `python3.12` 换成 `python3.13`。接下来把生成结果放在仓库外，用内置的确定性计算示例跑完整演示：

```sh
mkdir -p ../sheetbenchkit-demo
cd ../sheetbenchkit-demo

python -m sheetbenchkit generate --demo --seed 42 --output suite
python -m sheetbenchkit validate --suite suite
python -m sheetbenchkit run --suite suite --results results --config-id independent -- \
  python -m sheetbenchkit.examples.independent_runner
python -m sheetbenchkit grade --suite suite --observations results/observations.json --output regraded
```

`generate` 要求输出目录尚不存在，不会覆盖已有测试套件。演示按声明的角色选取 22 个基础案例，生成 30 个案例和四组三元组。使用内置计算示例时，30 个案例与四组三元组应全部通过。示例会读取实际输入文件，按契约做独立精确计算，不调用模型。

`run` 保存 `observations.json`、`report.json` 和 `report.html`。`grade` 读取已保存的输出重新生成 JSON 和 HTML 报告，不会重新启动适配器。用浏览器打开 `results/report.html` 即可查看结果和证据。

### Windows PowerShell

```powershell
git clone https://github.com/xwj123456/sheetbenchkit.git
cd sheetbenchkit
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .

.\.venv\Scripts\python.exe -m sheetbenchkit validate --suite docs/examples/report-preview/suite
.\.venv\Scripts\python.exe -m sheetbenchkit grade --suite docs/examples/report-preview/suite --observations docs/examples/report-preview/observations.json --output ../sheetbenchkit-preview
$LASTEXITCODE
```

使用 Python 3.13 时，将 `py -3.12` 换成 `py -3.13`。这里直接使用虚拟环境中的 Python，无需先激活环境。

这组保存好的预览有意包含一个 PASS、一个 FAIL 和一个 NO_RESULT，**所以 `grade` 返回退出码 1 是预期结果**。打开 `../sheetbenchkit-preview/report.html` 可以查看三个状态。此过程不会启动适配器或调用模型。

Windows 支持安装、`generate`、`validate` 和 `grade`。**Windows 不支持 `run`，该命令会在启动适配器前返回退出码 2。** 需要执行适配器时使用 macOS/Linux；Windows 可直接对已有输出做离线评分。当前 CI 验证 Python 3.12 和 3.13，不代表已经验证所有更新版本。

### 安装下载的 wheel

从 [GitHub Releases](https://github.com/xwj123456/sheetbenchkit/releases) 下载 wheel，在已有的 Python 3.12/3.13 虚拟环境中执行：

```sh
python -m pip install ./sheetbenchkit-0.1.0-py3-none-any.whl
```

安装不依赖 PyPI 发布。wheel 包含内置的 30 个案例和示例适配器，但不会安装仓库中的 `docs/`。若要复现保存好的报告预览或在本地阅读文档，还需克隆仓库，或解压对应版本的源码归档。

## 看一份报告

![真实评分器根据构造的合成输出生成的 SheetBenchKit 报告](docs/assets/report-preview.png)

[报告预览说明](docs/report-preview.md) 列出了每个案例的来源和复现命令。预览输出是为演示构造的示例 JSON/文本，不是真实 AI 模型运行结果。用量为未知，费用也为未知，不能当作零费用或模型表现证据。

要把自己的 CSV/XLSX 做成测试案例，请阅读[自定义案例教程](docs/custom-cases.md)：先写清计算规则，再独立复核答案，冻结输入后验证整个套件。

## 四个命令

命令可写成 `sheetbenchkit COMMAND`，也可写成 `python -m sheetbenchkit COMMAND`。

| 命令 | 参数 | 作用 |
| --- | --- | --- |
| `validate` | `--suite DIR` | 检查契约、冻结文件、输入哈希和套件预算。 |
| `generate` | `(--base DIR \| --demo) --seed N --output DIR` | 从已确认套件或内置数据生成输入变体。 |
| `run` | `--suite DIR --results DIR [--repeats N] [--timeout N] [--config-id ID] -- ADAPTER ARG...` | 执行明确指定的可信命令，保存输出并评分。 |
| `grade` | `--suite DIR --observations FILE --output DIR` | 对已有输出评分，不启动适配器、不使用网络。 |

`run` 默认每个案例执行一次，每次超时为 60 秒。重复次数范围为 1–10，不做自动重试，也不挑选最佳结果。`--config-id` 只是调用方填写的标签，不认证模型身份。适配器可以封装 AI 系统，但当前版本没有在线模型提供商集成或真实模型表现结论。详见[适配器协议](docs/adapter-protocol.md)。

退出码：全部必需检查通过为 `0`；出现 FAIL、NO_RESULT 或必需的三元组检查未完成为 `1`；ERROR 或无效输入/报告为 `2`。ERROR 优先。缺失的输出仍计入计划分母。

## 支持范围与边界

0.1.0 支持 CSV/XLSX、明确的文件/工作表/表头/行范围选择、单字段文本相等筛选、求和、行数统计和两个聚合值的比率。计算使用精确算术，最后按 HALF_UP 四舍五入。它不计算公式、不连接多表、不转换单位、不跳过被选中的无效值，也不执行自定义评分代码。

`grade` 将保存的文本与冻结答案比较，不会修复输出。`run` 不通过 shell 执行命令，并限制 POSIX 进程组、时间和捕获输出。**它不是沙箱**：适配器仍能访问文件、网络、环境变量和模型服务。运行路径与日志可能含敏感信息，分享前需要检查。

继续阅读[评分语义与限制](docs/benchmark.md)、[架构说明](docs/architecture.md)、[贡献指南](CONTRIBUTING.md)、[安全说明](SECURITY.md)和[更新记录](CHANGELOG.md)。项目使用 [Apache-2.0](LICENSE) 协议。
