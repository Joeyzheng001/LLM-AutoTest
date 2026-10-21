---
version: 2.0
spec: F-E2E-GROUND
owner: QA
changelog:
  - 2.0 | 2026-10-20 | 完整页面分片证据逐页读取，定位信息保留页面和 frame 归属
sampling:
  temperature: 0.1
---
当前输入是一个业务场景与真实页面观察的一页。读取本页全部 entries，提取与该场景有关的事实，不生成或修改业务预期。
只输出 {"facts":[{"description":"事实","selector":"逐字的真实语义定位器，可空","frame":{"url":"真实 frame URL","name":"真实名称"},"source_page":"本页 page_id"}],"gaps":["仍缺少的观察"]}。
仅使用本页实际存在的文本、定位器、frame 与状态。不要猜 CSS、XPath、方法或成功消息。顶层页面使用 frame=null。
每页都是完整证据的一部分。未知时说明缺口，不把未出现视为页面不存在。
