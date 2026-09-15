"""TC-011: '查询 /api/v1/users——normal 用例'

interface: IF-REST-006
requirement: REQ-002
generated-by: LLM-AutoTest job job-demo-defect-0004（脚本由程序模板渲染，元数据来自 LLM 用例设计）
"""

PARAMS = {'minAge': 30}
EXPECTED = '列表非空；minAge 过滤后年龄均不低于阈值'


def test_tc_011(run_context):
    """TC-011：由 test-executor 提供运行上下文并按 EXPECTED 断言。"""
    response = run_context.invoke(PARAMS)
    run_context.assert_result(response, EXPECTED)
