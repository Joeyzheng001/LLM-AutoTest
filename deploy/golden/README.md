# G2 golden 基准（R-REQ-04 验收物料）

| 文件 | 角色 |
|---|---|
| `requirements.md` / `.docx` / `.xlsx` | 三格式需求样例（同一内容：5 条需求——用户查询/列表过滤/创建/更新删除/订单查询） |
| `golden.expected.json` | 人工预标注期望：条目数 5 + 关键条目清单（ID/关键词/优先级） |
| `check_golden.py` | 比对脚本：`requirement_items.json` vs 预置基准，退出码 0/1 可直接判定 |
| `golden.quality.json` | 语义质量阈值：需求覆盖、来源回溯、无证据误判、重复率 |
| `evaluate_job.py` | 离线评估脚本：区分结构合规与语义质量，输出可归档 JSON 报告 |

## 验收用法（G2：比对预置基准，非现场人工判断）

```bash
# 三格式各跑一次 req_parse（换 --doc 即可），产物逐份比对：
python3 deploy/golden/check_golden.py /tmp/llm-autotest/<job>/requirement_items.json
```

判据（脚本内置）：条目数一致 + 每个关键条目按 ID 命中且 title 含全部关键词、
priority 一致。

三格式样例已验证可被 doc_loader 正确文本化（关键词命中 6/6）。

## 语义质量评估

```bash
python3 deploy/golden/evaluate_job.py /tmp/llm-autotest/<job>/
```

报告分为两部分：

- `structural.passed`：必需产物存在且数组结构正确；
- `semantic`：需求覆盖率、来源可追溯率、无证据判定率、重复率和人工采纳率。

人工采纳率只有在产物目录存在 `review_decisions.json`，或 manifest 显式传入
`human_decisions` 时才计算；没有人工记录时返回 `null`，不把缺数据当成通过。
阈值只在 `golden.quality.json` 中声明，模型/Prompt 变更前后使用同一 manifest 比较。
