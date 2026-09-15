/// <reference types="vite/client" />

// Vue SFC 模块类型声明（未安装依赖前 IDE 也能解析）
declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<object, object, unknown>
  export default component
}
