"""JSON 规范化比较：忽略浮点尾数差异，判断两份可 JSON 序列化的结果是否业务一致。

重跑同一批快照时，SQLAlchemy 内存中的旧 dict 与新计算结果可能因浮点序列化往返
（如 298.99999999999994 vs 299.0、-0.0 vs 0.0）出现 != 误判。统一用 JSON 往返后的
字符串做确定性比较，保证「结果不变就不归档、不新增」的幂等口径。
"""
from __future__ import annotations

import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_equal(left: Any, right: Any) -> bool:
    return canonical_json(left) == canonical_json(right)
