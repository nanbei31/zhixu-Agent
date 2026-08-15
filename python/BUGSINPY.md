# BugsInPy 真实故障评测

Mini Claude 可以直接读取本地克隆的
[BugsInPy](https://github.com/soarsmu/BugsInPy) 元数据，并复用现有
AutoCI-Fix、pytest-repair Skill、WorkspacePolicy、Git Worktree 与报告系统。
适配器不需要 MongoDB，也不要求 Docker；每个历史项目仍应使用兼容其 Python
版本和依赖的独立 Conda 环境。

当前适配器已用 BugsInPy 官方仓库 `11c5f1e` 快照验证，可识别 501 个可运行
案例。案例数量由 `--bugsinpy-list` 从本地元数据动态计算，不在代码中硬编码。

## 评测边界

BugsInPy 官方数据包含真实项目的 buggy/fixed commit、触发测试、Python 版本、
依赖和测试脚本。适配器只向 Agent 暴露 buggy fixture、触发测试与允许读取的
仓库内容。fixed commit和官方修改文件仅供准备及评分使用。

支持两种故障定位模式：

| 模式 | Agent 可写范围 | 是否提供官方目标文件 | 用途 |
|---|---|---|---|
| `end-to-end` | 自动发现的生产代码根 | 否 | 评价自主定位与修复，默认模式 |
| `oracle` | 官方补丁涉及的生产文件 | 是 | 单独评价已知故障位置后的补丁能力 |

两种结果必须分开报告，不能把 Oracle 分数描述成端到端修复能力。

## 下载数据

在项目外克隆官方仓库：

```bash
mkdir -p ~/benchmarks
cd ~/benchmarks
git clone https://github.com/soarsmu/BugsInPy.git
```

Windows 示例位置：

```text
D:\Learning-AI\benchmarks\BugsInPy
```

BugsInPy 数据和案例工作区不应提交进 Mini Claude 的 Git 仓库。

## 列出案例

列举元数据不调用模型，也不需要 API Key：

```bash
mini-claude-py \
  --bugsinpy \
  --bugsinpy-root ~/benchmarks/BugsInPy \
  --bugsinpy-list
```

输出包含案例 ID、官方 Python 版本和触发测试文件。案例 ID 使用
`<project>-<bug-id>`，例如 `black-1`。

## 只准备案例

先准备一个 End-to-End fixture：

```bash
mini-claude-py \
  --bugsinpy \
  --bugsinpy-root ~/benchmarks/BugsInPy \
  --bugsinpy-case black-1 \
  --bugsinpy-workspaces ~/benchmarks/bugsinpy-workspaces \
  --bugsinpy-localization end-to-end \
  --bugsinpy-prepare-only
```

准备过程执行以下操作：

1. 从官方 `project.info` 读取项目地址。
2. Checkout `buggy_commit_id`。
3. 从 fixed commit恢复官方触发测试。
4. 计算官方生产代码修改文件，仅留给评估器。
5. End-to-End 模式根据 buggy revision独立发现生产代码根。
6. 写入 WorkspacePolicy 与 pytest-repair Skill。
7. 提交一个干净 fixture，供 detached Worktree 使用。

不同模式使用不同目录，例如：

```text
black-1-end-to-end/
black-1-oracle/
```

## 建立独立环境

准备输出会显示官方 Python 版本，并在 fixture 中复制：

```text
.bugsinpy/requirements.txt
.bugsinpy/run_test.sh
.bugsinpy/setup.sh    # 仅部分案例存在
```

不要降级 Mini Claude 的 Agent 环境。为案例创建独立环境，例如：

```bash
conda create -n bugsinpy-black-1 python=3.8 pip -y
conda run -n bugsinpy-black-1 python -m pip install pytest
conda run -n bugsinpy-black-1 python -m pip install \
  -r ~/benchmarks/bugsinpy-workspaces/black-1-end-to-end/.bugsinpy/requirements.txt
```

具体项目可能还需要 editable 安装或执行经过人工检查的 `setup.sh`。依赖安装不能
把 fixture 弄脏；安装后使用 `git status --short` 检查。对于 `src/` 布局，测试
命令应把当前隔离 Worktree 的 `src` 放入 `PYTHONPATH`，避免 editable 安装继续
引用准备目录。

## 运行单案例

Linux 示例：

```bash
mini-claude-py \
  --bugsinpy \
  --bugsinpy-root ~/benchmarks/BugsInPy \
  --bugsinpy-case black-1 \
  --bugsinpy-workspaces ~/benchmarks/bugsinpy-workspaces \
  --bugsinpy-localization end-to-end \
  --bugsinpy-test-command \
    "PYTHONPATH=. conda run -n bugsinpy-black-1 python -m pytest -q <触发测试>" \
  --bugsinpy-full-test-command \
    "PYTHONPATH=. conda run -n bugsinpy-black-1 python -m pytest -q" \
  --bugsinpy-output ~/benchmarks/bugsinpy-results/black-1 \
  --max-fix-attempts 2 \
  --ci-timeout 900
```

如果不传 `--bugsinpy-test-command`，默认执行 fixture 中的官方脚本：

```text
bash .bugsinpy/run_test.sh
```

建议先手动确认测试命令在 buggy fixture 中稳定失败，再调用模型。全量回归可能
很慢，可在第一轮试跑时省略 `--bugsinpy-full-test-command`，但正式实验应尽量
提供。

Oracle 对照实验只改一个参数：

```bash
--bugsinpy-localization oracle
```

## 通过标准

一个案例只有同时满足以下条件才记为 `PASS`：

1. 初始触发测试确实失败。
2. AutoCI-Fix 在隔离 Worktree 中修复并通过触发测试。
3. `changes.patch` 能应用到全新的干净 fixture。
4. 独立 fixture 中的触发测试通过。
5. 提供全量回归命令时，全量测试通过。
6. 所有修改均位于当前定位模式的 WorkspacePolicy 可写范围内。
7. 测试、依赖、CI配置、凭据和其他敏感路径没有被修改。

主要指标包括：

```text
End-to-End Resolve Rate
Oracle-localized Resolve Rate
Success@1
Policy Compliance Rate
Patch Apply Rate
Trigger Test Pass Rate
Full Regression Pass Rate
平均 Token、估算费用、耗时和修复轮次
```

## 报告

每次运行沿用 AutoCI 产物，并新增：

```text
bugsinpy-report.json
report.json
changes.patch
events.jsonl
final-test.log
git-status.txt
metadata.json
```

`bugsinpy-report.json` 记录定位模式、官方目标文件（仅在运行结束后的评分报告中
出现）、独立补丁验证、触发测试、全量回归、权限合规、Token、费用和耗时。

## 实验建议

先选择约20个可复现案例，覆盖至少5个项目和不同故障类型。固定模型、Skill
版本、最大轮次、最大修复次数、测试命令和环境版本，每个案例至少重复三次。
环境错误和不可复现案例应单独统计，不能直接算作 Agent 修复失败，也不能静默
从分母移除。
