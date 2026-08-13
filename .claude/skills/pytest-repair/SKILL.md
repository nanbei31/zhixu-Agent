---
name: pytest-repair
description: 分析 pytest 失败并进行最小、安全的生产代码修复
when-to-use: 当 pytest 命令失败且可能需要修改实现代码时使用
user-invocable: true
context: inline
allowed-tools: ["read_file", "list_files", "grep_search", "edit_file", "write_file"]
---

# Pytest 故障修复

调用方提供的失败上下文：

$ARGUMENTS

请按照以下流程执行：

1. 将失败分类为：测试收集、导入、Fixture、断言、超时、环境问题或未知问题。
2. 阅读失败的测试文件以及相关生产代码。
3. 根据 traceback 和源码形成有证据支持的根因假设。
4. 对生产代码进行能够解决根因的最小修改。
5. 不得仅为了通过测试而删除、跳过或削弱测试。
6. 不要自行运行测试，AutoCI-Fix Runner 会负责验证。
7. 不要提交代码、安装依赖或修改 CI 配置。
8. 修改前必须先阅读对应文件。
9. 优先修复生产代码；只有测试本身明确错误时，才能考虑修改测试。

完成后输出：

- 根本原因
- 修改的文件
- 为什么这是最小修改
- 仍然存在的风险
