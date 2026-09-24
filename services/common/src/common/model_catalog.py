"""内部 LLM 中转站的默认模型口径。

中转站会持续新增模型，因此服务端不以白名单拒绝模型别名；调用时由中转站决定
该别名是否可用。这里只维护未显式配置时的安全默认值，避免 UI、API 与 Settings
各自散落旧的 MiniMax 默认模型。
"""

DEFAULT_RELAY_MODEL = "deepseek-reasoner-code"
