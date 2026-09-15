"""TC-014: '创建 /api/v1/users——boundary 用例'

interface: IF-REST-007
requirement: -
generated-by: LLM-AutoTest job job-demo-defect-0004（脚本由程序模板渲染，元数据来自 LLM 用例设计）
"""

PARAMS = {'name': 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', 'email': 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx', 'age': 200}
EXPECTED = '2xx 响应结构符合接口定义'


def test_tc_014(run_context):
    """TC-014：由 test-executor 提供运行上下文并按 EXPECTED 断言。"""
    response = run_context.invoke(PARAMS)
    run_context.assert_result(response, EXPECTED)
