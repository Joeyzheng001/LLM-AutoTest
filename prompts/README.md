# prompts/

全部 LLM Prompt 的版本化存放目录（红线见 [docs/09_开发规范说明.md](../docs/09_开发规范说明.md) 第五章）。

## 约定

1. **禁止在代码中内联 Prompt 字符串**——所有 Prompt 一律存放于本目录独立文件；
2. 文件命名：`<节点>-<用途>-v<版本>.md`（如 `req-parse-extract-v1.md`、`rev-reconstruct-v1.md`）；
3. 每个 Prompt 文件头部包含元信息块（**复制 [_template.md](_template.md) 起笔**）：版本号、对应规格编号、主笔人、变更记录、建议采样参数（temperature / top_p / seed）——加载器按此结构解析，字段名不得自造；
4. 变更必须升版本号并在文件内追加变更记录，禁止原地改语义。

## 与 schemas/ 的关系

Prompt 要求 LLM 输出的结构，必须在 [schemas/](../schemas/) 有对应 JSON Schema——LLM 输出经 Schema 校验成功后才能进入下游产物（校验失败按节点失败处理，禁止截取可用部分）。
