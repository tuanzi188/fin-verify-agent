"""被测 Agent 与评测入口的分界。

`app/agent/` 只放被测对象：它不知道评测存在，也不 import `evaluation/`。
反向依赖（评测 import Agent）是单向的，这条边界由 P3 的 import 检查守住。
"""
