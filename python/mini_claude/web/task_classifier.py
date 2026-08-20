"""Small deterministic classifier used to make orchestration visible."""

from __future__ import annotations


RULES = (
    ("故障修复", ("bug", "fix", "报错", "错误", "失败", "修复", "异常", "traceback")),
    ("功能开发", ("新增", "实现", "添加", "feature", "支持", "开发")),
    ("重构", ("重构", "refactor", "优化结构", "整理代码")),
    ("测试", ("测试", "pytest", "test", "覆盖率")),
    ("文档", ("文档", "readme", "说明", "注释")),
    ("代码解释", ("解释", "分析", "为什么", "讲一下", "怎么工作")),
)


def classify_task(message: str) -> dict:
    lowered = message.lower()
    scored = []
    for label, keywords in RULES:
        matches = [keyword for keyword in keywords if keyword in lowered]
        if matches:
            scored.append((len(matches), label, matches))
    if not scored:
        return {"category": "通用编程", "confidence": 0.55, "evidence": []}
    score, label, evidence = max(scored, key=lambda item: item[0])
    return {
        "category": label,
        "confidence": min(0.95, 0.62 + score * 0.09),
        "evidence": evidence[:5],
    }
