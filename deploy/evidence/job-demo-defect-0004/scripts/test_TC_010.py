"""TC-010: '查询 /api/v1/users/{id}——boundary 用例'

interface: IF-REST-005
requirement: REQ-001
generated-by: LLM-AutoTest job job-demo-defect-0004（脚本由程序模板渲染，元数据来自 LLM 用例设计）
"""

PARAMS = {'id': 999999}
EXPECTED = 'status=400；不存在资源返回业务错误码'


def test_tc_010(run_context):
    """TC-010：由 test-executor 提供运行上下文并按 EXPECTED 断言。"""
    response = run_context.invoke(PARAMS)
    run_context.assert_result(response, EXPECTED)
