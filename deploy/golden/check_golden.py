#!/usr/bin/env python3
"""G2 golden 基准比对（R-REQ-04）：req_parse 产物 vs 预置期望。

用法：
    python3 check_golden.py <requirement_items.json> [golden.expected.json]

比对口径（G2：预置基准，非现场人工判断）：
  1. 条目数一致（expected_item_count）；
  2. 每个关键条目按 ID 命中，且 title 含全部关键词、priority 一致；
  3. 产出明细与判定结果（逐项 PASS/FAIL + 总结论）。

退出码：0 通过 / 1 不通过（验收脚本可直接判定）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_GOLDEN = Path(__file__).resolve().parent / "golden.expected.json"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    items_path = Path(sys.argv[1])
    golden_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_GOLDEN

    items = json.loads(items_path.read_text(encoding="utf-8"))["items"]
    golden = json.loads(golden_path.read_text(encoding="utf-8"))

    failures: list[str] = []

    # 1) 条目数
    expected_count = golden["expected_item_count"]
    if len(items) != expected_count:
        failures.append(f"条目数不符：实际 {len(items)}，期望 {expected_count}")

    # 2) 关键条目逐项比对
    by_id = {item.get("id"): item for item in items}
    for key_item in golden["key_items"]:
        rid = key_item["id"]
        actual = by_id.get(rid)
        if actual is None:
            failures.append(f"{rid}：缺失（期望命中）")
            continue
        title = actual.get("title", "")
        for keyword in key_item["title_keywords"]:
            if keyword not in title:
                failures.append(f"{rid}：title 缺关键词「{keyword}」（实际：{title}）")
        if actual.get("priority") != key_item["priority"]:
            failures.append(
                f"{rid}：priority 不符（实际 {actual.get('priority')}，"
                f"期望 {key_item['priority']}）"
            )

    # 明细输出
    print(f"产物：{items_path}")
    print(f"基准：{golden_path}")
    for item in items:
        print(f"  - {item.get('id')} [{item.get('priority')}] {item.get('title')}")
    if failures:
        print("\n比对失败：")
        for failure in failures:
            print(f"  ✗ {failure}")
        return 1
    print(f"\n比对通过：{len(items)} 条目全部命中预置 golden 基准（G2 判据）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
