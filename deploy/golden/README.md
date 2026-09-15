# G2 golden 基准（R-REQ-04 验收物料）

| 文件 | 角色 |
|---|---|
| `requirements.md` / `.docx` / `.xlsx` | 三格式需求样例（同一内容：5 条需求——用户查询/列表过滤/创建/更新删除/订单查询） |
| `golden.expected.json` | 人工预标注期望：条目数 5 + 关键条目清单（ID/关键词/优先级） |
| `check_golden.py` | 比对脚本：`requirement_items.json` vs 预置基准，退出码 0/1 可直接判定 |

## 验收用法（G2：比对预置基准，非现场人工判断）

```bash
# 三格式各跑一次 req_parse（换 --doc 即可），产物逐份比对：
python3 deploy/golden/check_golden.py /tmp/llm-autotest/<job>/requirement_items.json
```

判据（脚本内置）：条目数一致 + 每个关键条目按 ID 命中且 title 含全部关键词、
priority 一致。

三格式样例已验证可被 doc_loader 正确文本化（关键词命中 6/6）。
