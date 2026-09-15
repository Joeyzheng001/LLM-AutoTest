"""TC-013: '创建 /api/v1/users——normal 用例'

interface: IF-REST-007
requirement: -
generated-by: LLM-AutoTest job job-demo-defect-0004（脚本由程序模板渲染，元数据来自 LLM 用例设计）
"""

PARAMS = {'name': 'stub-value', 'email': 'stub-value', 'age': 30}
EXPECTED = '返回系统分配的编号（id>0）；2xx'


def test_tc_013(run_context):
    """TC-013：由 test-executor 提供运行上下文并按 EXPECTED 断言。"""
    response = run_context.invoke(PARAMS)
    run_context.assert_result(response, EXPECTED)
