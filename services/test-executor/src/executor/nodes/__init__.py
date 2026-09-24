"""丙泳道节点实现：用例生成 / 执行 / 缺陷 / 回归（GEN / EXEC / DEF / REG）。

由 test-executor 服务承载；单体过渡期由 orchestrator 经本地 import 驱动，
服务拆分后随迁远程调用（docs/19）。
"""

from executor.nodes.auto_execute import run_auto_execute
from executor.nodes.defect_list import run_defect_list
from executor.nodes.retest_regression import run_retest_regression
from executor.nodes.testcase_generate import run_testcase_generate

__all__ = [
    "run_auto_execute",
    "run_defect_list",
    "run_retest_regression",
    "run_testcase_generate",
]
