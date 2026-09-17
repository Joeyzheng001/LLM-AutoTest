// C-16：跨页导航共享状态（总览页节点卡跳转 → 目标页携带 jobId）
// 模块级单例（项目无 vue-router，App.vue 按 activeKey 切换动态组件）
import { reactive } from 'vue'

export const navigation = reactive({
  view: '', // 目标视图 key（App.vue views 清单同源）
  jobId: '', // 跨页携带的 job 标识
  e2e: null as null | { generateCases: boolean; executeCases: boolean },
})

/** 总览页节点卡等入口调用：切换视图并携带 jobId */
export function navigateTo(view: string, jobId: string): void {
  navigation.jobId = jobId
  navigation.view = view
}
