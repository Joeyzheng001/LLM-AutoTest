<!-- F-REQ-01~03：需求文档解析为可追溯的原子需求声明；ID 与来源目录由程序管理。 -->

---
version: 2.0
spec: F-REQ-01
owner: 甲
changelog:
  - 2.0 | 2026-09-22 | 输出原子声明与 source_id；程序生成 REQ/CLM ID 和兼容描述字段
  - 1.0 | 2026-09-15 | 初版（md/docx/xlsx 三格式统一文本化输入）
sampling:
  temperature: 0.1
  top_p: 0.8
  seed: null
---

## 目标

从本批文档片段中提取可核验的需求，不评估实现，不补写原文没有的业务规则。输入片段以
`[SRC-...]` 开头；该来源编号是系统生成的唯一引用。每条需求必须引用至少一个当前批次的
来源编号。不得生成、改写或猜测来源编号。

## 输出契约

只输出一个 JSON 对象，且仅包含 `items` 数组。不要输出 Markdown、解释、统计信息、Schema
字段或其他节点的产物。模型不负责生成 REQ/CLM ID。

```json
{
  "items": [
    {
      "title": "简短、贴近原文的需求标题",
      "priority": "P0",
      "source_refs": ["SRC-D01-P0001"],
      "claims": [
        {
          "kind": "normal",
          "statement": "一个独立、可验证的系统行为",
          "acceptance_criteria": ["原文明确给出的可观察结果"],
          "source_refs": ["SRC-D01-P0001"]
        }
      ]
    }
  ]
}
```

`priority` 仅在原文明确给出时填写，值为 `P0`、`P1` 或 `P2`。`claims[].kind` 只能使用：
`normal`、`validation`、`boundary`、`exception`、`authorization`、`state_transition`、
`data_consistency`、`non_functional`、`dependency`。

## 提取规则

1. 每个 claim 只表达一个可独立验证的行为、约束、状态变化或质量要求；复合句按独立行为拆分。
2. 保留触发条件、角色、输入约束、结果和异常行为。只有原文明确写出的验收条件才放入
   `acceptance_criteria`；不要把推测包装成验收标准。
3. 背景、目标、术语解释本身不作为需求。若它们包含明确约束或可观察行为，则提取该约束。
4. 把同一主题下的 claims 放入一个 item；不同业务主题不要强行合并。近义重复可以合并，
   但合并后的 `source_refs` 要包含全部对应来源。
5. 对矛盾、缺条件或无法确定的表述仍忠实提取。不要自行裁决；留给需求评审节点指出。
6. 长输入只处理当前批次。不得引用当前输入未出现的来源编号或其他批次内容。
7. 当前批次没有需求时返回 `{"items":[]}`。不为满足数量虚构需求。

## 提交前自检

- 每个 item 至少有一个 claim 和一个有效的 `source_refs`。
- 每个 claim 是单一可验证陈述，没有把正常、异常、边界条件揉成一个大段落。
- 没有生成 ID、代码实现结论、测试用例或原文未定义的规则。
- 输出可以直接被 JSON 解析，字段和值类型与契约一致。
