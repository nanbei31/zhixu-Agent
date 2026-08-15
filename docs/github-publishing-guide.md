# 项目发布到 GitHub 操作手册

本文用于将当前二开项目发布到自己的 GitHub 仓库，并配置 AutoCI Repair
工作流。示例以 Windows 项目目录为准，Linux 下 Git 和 GitHub 操作相同。

项目目录：

```text
D:\Learning-AI\claude-code-from-scratch-main
```

## 1. 发布原则

本项目基于 `Windy3f3f3f3f/claude-code-from-scratch` 二次开发。原项目使用
MIT License，因此可以修改、发布和再分发，但必须保留仓库中的 `LICENSE`
及原版权声明。

建议在 README 开头明确写明：

```markdown
本项目基于 [claude-code-from-scratch](https://github.com/Windy3f3f3f3f/claude-code-from-scratch)
进行二次开发，新增了安全工作区策略、Git Worktree 隔离、失败回滚、Skill
运行时、AutoCI-Fix、可观测性报告、故障 Benchmark、PyBugHive 适配器以及
GitHub Actions 自动修复与 Draft PR 工作流。
```

不要删除原作者版权，不要把整个原项目描述成完全独立原创。简历中可以清楚
区分“原项目能力”和“本人新增能力”。

## 2. 不应上传的内容

以下内容不得提交到 GitHub：

- `.env`、API Key、访问令牌和私钥；
- Conda/venv 环境目录；
- `.autoci/`、运行日志、SQLite 数据库和临时 Worktree；
- PyBugHive 克隆出的真实项目工作区；
- Benchmark/PyBugHive 批量运行结果，除非经过脱敏并有意作为实验结果发布；
- 本机绝对路径、账号、代理地址及包含凭据的配置。

当前 `.gitignore` 已忽略 `.env`、`.venv/`、`.autoci/`、缓存和构建目录。
PyBugHive 数据与工作区目前位于仓库外的 `D:\Learning-AI\benchmarks\`，保持
这种目录结构，不要将它们复制进项目仓库。

发布前检查 Git 实际追踪的文件：

```powershell
cd D:\Learning-AI\claude-code-from-scratch-main

git status --short
git diff --check
git ls-files | Select-String -Pattern '\.env$|\.pem$|\.key$|autoci\.db|pybughive-workspaces|pybughive-results'
git grep -n -I -E 'sk-ant-|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]+' -- .
```

最后两条命令没有输出才是理想结果。示例文档中的 `sk-xxx`、`test` 等占位符
不是真实凭据。如果真实密钥曾经进入 Git 历史，仅删除文件不够：先在服务商
后台撤销并重新生成密钥，再清理 Git 历史。

## 3. 本地发布前验证

激活安装了 Mini Claude 的环境并运行测试：

```powershell
cd D:\Learning-AI\claude-code-from-scratch-main

conda run -n agent python -m pytest -q python/tests
conda run -n agent mini-claude-py --benchmark --benchmark-validate
git diff --check
```

PyBugHive 的真实案例环境彼此独立，不属于项目单元测试。`black-2254` 可以额外
验证为只有目标回归失败：

```powershell
cd D:\Learning-AI\benchmarks\pybughive-workspaces\black-2254

conda run -n pybughive-black python -m pytest -q -- tests/test_format.py
```

预期基线为 `1 failed, 85 passed`。这里的失败是 Benchmark 输入，不代表 Mini
Claude 项目测试失败。

## 4. 提交当前二开代码

先检查所有改动，不要直接盲目执行 `git add .`：

```powershell
cd D:\Learning-AI\claude-code-from-scratch-main

git status --short
git diff -- python/README.md python/mini_claude/__main__.py
git diff --no-index -- NUL python/mini_claude/pybughive/catalog.py
git diff --no-index -- NUL python/mini_claude/pybughive/runner.py
git diff --no-index -- NUL python/tests/test_pybughive.py
```

确认无误后，只暂存这次需要发布的文件：

```powershell
git add -- `
  python/README.md `
  python/mini_claude/__main__.py `
  python/mini_claude/pybughive/__init__.py `
  python/mini_claude/pybughive/catalog.py `
  python/mini_claude/pybughive/runner.py `
  python/tests/test_pybughive.py `
  docs/github-publishing-guide.md

git diff --cached --check
git diff --cached --stat
git status --short
git commit -m "feat: add PyBugHive benchmark adapter"
```

如 `git status` 还显示其他未提交文件，应先确认它们是否属于本次版本，不能为了
获得干净工作区而随意删除或还原。

## 5. 创建 GitHub 仓库

在 GitHub 网页右上角选择 **New repository**，建议设置：

```text
Repository name: mini-claude-autoci（也可使用自己的名称）
Visibility: Public
Initialize this repository with a README: 不勾选
Add .gitignore: 不选择
Choose a license: 不选择（本地已有 MIT LICENSE）
```

公开仓库更方便放在简历中展示。创建空仓库后，不要使用 GitHub 自动生成的
README，否则首次推送时会多一次无意义的历史合并。

## 6. 配置远程并首次推送

当前本地分支是 `master`，而 AutoCI Actions 默认使用 `main`。发布前统一为
`main`：

```powershell
cd D:\Learning-AI\claude-code-from-scratch-main

git branch -M main
git remote add origin https://github.com/<你的GitHub用户名>/<你的仓库名>.git
git remote add upstream https://github.com/Windy3f3f3f3f/claude-code-from-scratch.git
git remote -v
git push -u origin main
```

这里：

- `origin` 是你自己的二开仓库，用于正常推送；
- `upstream` 是原始开源仓库，只用于查看或同步上游变化；
- 不要向 `upstream` 直接推送。

如果使用 SSH，把 `origin` 地址改成：

```text
git@github.com:<你的GitHub用户名>/<你的仓库名>.git
```

如果 `origin` 已存在，使用以下命令修改，不要重复添加：

```powershell
git remote set-url origin https://github.com/<你的GitHub用户名>/<你的仓库名>.git
```

## 7. 完善 GitHub 项目主页

首次推送后，建议完成以下设置：

1. 在仓库 **About** 中填写一句清晰描述，例如：
   `Policy-constrained coding agent with isolated CI repair and real-world benchmarks.`
2. 添加 Topics：`coding-agent`、`llm-agent`、`python`、`pytest`、`benchmark`、
   `github-actions`、`agentic-ai`。
3. 在 README 顶部说明二开关系、核心新增能力和安全边界。
4. 放一张真实终端修复截图和一份脱敏后的 Benchmark 汇总结果。
5. 确认 `LICENSE` 在 GitHub 页面上可识别为 MIT。

推荐 README 按以下结构组织：

```text
项目定位
二开来源与许可证
核心功能
系统架构图
AutoCI 修复流程
WorkspacePolicy 安全设计
Benchmark 与 PyBugHive 评估结果
快速开始
报告示例
GitHub Actions 自动修复
测试结果
已知限制
```

报告示例应删除绝对路径、用户名、API 地址、Prompt 中的私有源码和任何可能的
凭据。费用字段是静态价格表估算值，应标注“Estimated”，不能描述为账单金额。

## 8. 配置 GitHub Actions 密钥

打开：

```text
Repository > Settings > Secrets and variables > Actions
```

选择一种模型后端配置 Repository secrets。

Anthropic 后端：

```text
ANTHROPIC_API_KEY       必填
ANTHROPIC_BASE_URL      使用代理时填写
```

OpenAI 兼容后端：

```text
OPENAI_API_KEY          必填
OPENAI_BASE_URL         非默认兼容端点时填写
```

可在 **Variables** 中增加：

```text
MINI_CLAUDE_MODEL
```

不要把密钥写进 `.claude/settings.json`、workflow YAML、README、Issue、Actions
输入参数或运行日志。Repository secret 的名称必须与
`.github/workflows/autoci-repair.yml` 中使用的名称完全一致。

## 9. 配置 Actions 与分支保护

打开：

```text
Settings > Actions > General > Workflow permissions
```

启用：

```text
Read and write permissions
Allow GitHub Actions to create and approve pull requests
```

工作流内部仍按 Job 使用最小权限：Repair 只有只读权限，Verify 无模型密钥，
只有 Publish Job 拥有写入代码和创建 PR 的权限。

然后在默认分支保护规则中至少启用：

- 合并前必须通过 Pull Request；
- 要求至少一次人工 Review；
- 要求常规测试工作流通过；
- 禁止强制推送和删除分支；
- 将 `.github/`、`.claude/` 和 `python/mini_claude/github/` 视为安全敏感目录。

仓库 Settings 中的 Default branch 应确认为 `main`。

## 10. 首次 Actions Dry Run

`AutoCI Repair` 目前只支持维护者手动触发，且 `dry_run` 默认为 `true`。第一次
运行不要创建 PR，先验证报告、补丁和权限链路。

在 GitHub 中打开：

```text
Actions > AutoCI Repair > Run workflow
```

填写一个在目标分支上确实失败的测试。也可以使用 GitHub CLI：

```powershell
gh auth login

gh workflow run autoci-repair.yml `
  -f base_ref=main `
  -f setup_command='python -m pip install ./python pytest' `
  -f test_command='python -m pytest -q python/tests' `
  -f target='python/mini_claude' `
  -f max_attempts=2 `
  -f dry_run=true

gh run list --workflow autoci-repair.yml --limit 5
gh run watch
```

注意：如果 `python/tests` 当前全部通过，AutoCI 会正常结束且不会产生修复补丁。
要演示自动修复，应在专门的演示分支提交一个可复现故障和对应失败测试，然后把
`base_ref` 指向该分支，不要故意破坏受保护的 `main`。

运行结束后，在 Actions Run 页面下载：

```text
autoci-repair-<run-id>-<attempt>
autoci-verified-<run-id>-<attempt>
```

重点查看：

```text
report.json            修复轮次、测试、Token、费用和耗时
changes.patch          Agent 生成的补丁
events.jsonl           结构化事件流水
final-test.log         最终测试日志
validation.json        独立安全校验结果
verification.log      无模型密钥环境中的复验日志
```

Dry Run 成功后，将同一组参数的 `dry_run` 改为 `false`，工作流会创建 Draft PR：

```powershell
gh workflow run autoci-repair.yml `
  -f base_ref=demo/autoci-failure `
  -f setup_command='python -m pip install ./python pytest' `
  -f test_command='python -m pytest -q <失败测试路径>' `
  -f target='<允许修复的生产代码路径>' `
  -f max_attempts=2 `
  -f dry_run=false
```

Draft PR 仍需人工查看 Diff、测试证据和权限报告后才能合并。

## 11. 日常开发与同步上游

后续开发不要直接在 `main` 上堆积所有修改：

```powershell
git switch main
git pull --ff-only origin main
git switch -c feature/<功能名称>

# 修改并测试
git add <明确的文件路径>
git commit -m "feat: describe the change"
git push -u origin feature/<功能名称>
```

然后在 GitHub 上创建 Pull Request。同步原项目更新前先查看差异：

```powershell
git fetch upstream
git log --oneline --left-right main...upstream/main
```

上游可能改动同一批 Agent 核心文件，不建议未经检查直接合并。应在独立分支中
执行 merge 或 rebase，运行全部测试后再合入自己的 `main`。

## 12. 发布检查清单

- [ ] 保留 MIT `LICENSE` 和原作者版权声明；
- [ ] README 明确二开来源和本人新增内容；
- [ ] 没有提交 API Key、令牌、私钥、`.env` 或本机配置；
- [ ] 没有提交 Conda 环境、PyBugHive 工作区和未脱敏报告；
- [ ] `python/tests` 全部通过；
- [ ] 内置 40 案例数据集校验通过；
- [ ] `git diff --check` 通过；
- [ ] 默认分支、workflow 的 `base_ref` 均为 `main`；
- [ ] GitHub Secrets 和 Actions 权限配置完成；
- [ ] 首次 AutoCI 使用 `dry_run=true`；
- [ ] Draft PR 经人工检查后再合并；
- [ ] GitHub About、Topics、README 截图和评估结果已经补全。

