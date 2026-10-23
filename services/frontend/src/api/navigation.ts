// C-16：跨页导航共享状态（总览页节点卡跳转 → 目标页携带 jobId）
// 模块级单例（项目无 vue-router，App.vue 按 activeKey 切换动态组件）
import { reactive } from 'vue'

export const navigation = reactive({
  view: '', // 目标视图 key（App.vue views 清单同源）
  requestId: 0, // 同一视图再次打开时也触发导航
  jobId: '', // 跨页携带的 job 标识
  interfaceId: '', // 接口还原 → 接口逻辑 → 用例编排的当前接口
  runId: '', // 报告之间跳转时保留当前运行轮次
  e2e: null as null | { generateCases: boolean; executeCases: boolean },
})

/** 总览页节点卡等入口调用：切换视图并携带 jobId */
export function navigateTo(view: string, jobId: string, interfaceId = '', runId = ''): void {
  navigation.jobId = jobId
  navigation.interfaceId = interfaceId
  navigation.runId = runId
  navigation.view = view
  navigation.requestId += 1
}
