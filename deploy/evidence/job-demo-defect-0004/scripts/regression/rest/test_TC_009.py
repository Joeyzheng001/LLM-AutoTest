"""TC-009: '查询 /api/v1/users/{id}——normal 用例'

interface: IF-REST-005
requirement: REQ-001
generated-by: LLM-AutoTest job job-demo-defect-0004（脚本由程序模板渲染，元数据来自 LLM 用例设计）
"""

PARAMS = {'id': 1}
EXPECTED = 'id=1;name=alice;age=28；按 ID 查询返回完整用户'


def test_tc_009(run_context):
    """TC-009：由 test-executor 提供运行上下文并按 EXPECTED 断言。"""
    response = run_context.invoke(PARAMS)
    run_context.assert_result(response, EXPECTED)
