# 开工卡入口：对三个人各说一句话就够了

| 谁 | 你说这句话 | 他打开的文件 |
|----|-----------|--------------|
| 甲 | 「你是流水线平台负责人，看 [docs/kickoff/甲.md](甲.md)，今天做完三件事报完成」 | [甲.md](甲.md)（Python/LLM） |
| 乙 | 「你是 Java 逆向攻坚，看 [docs/kickoff/乙.md](乙.md)，上午先冻结契约会签」 | [乙.md](乙.md)（Java） |
| 丙 | 「你是执行闭环负责人，看 [docs/kickoff/丙.md](丙.md)，先用假数据跑通 GEN」 | [丙.md](丙.md)（Python/pytest/前端） |

每张卡的内容：**今天做什么（3 件事）→ 怎么算做完（命令）→ 卡住了找谁**。完整任务、依赖约束、里程碑见 [docs/11 三人分工任务书](../11_三人分工任务书.md)。

## 启动前的唯一前置条件

- 仓库已 clone，`uv` / `pnpm` / JDK 21 + Maven 已装（[docs/09 开发规范](../09_开发规范说明.md) 第一章有安装命令）；
- Day 1 冻结物已就位：10 个 [schemas/](../../schemas/) + 3 个 [samples/](../../schemas/samples/)（假数据样例，均已通过 Schema 校验）+ [prompts/_template.md](../../prompts/_template.md)。
