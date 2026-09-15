import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import tseslint from 'typescript-eslint'

export default [
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/recommended'],
  {
    files: ['**/*.{ts,vue}'],
    languageOptions: {
      parserOptions: {
        ecmaVersion: 'latest',
        parser: tseslint.parser, // .vue 内 <script lang="ts"> 用 TS 解析器
        sourceType: 'module',
      },
    },
  },
  {
    // 格式交给 prettier：关闭与具冲突的 vue 格式化规则
    rules: {
      'vue/max-attributes-per-line': 'off',
      'vue/singleline-html-element-content-newline': 'off',
      'vue/html-self-closing': 'off',
    },
  },
  {
    ignores: ['dist/'],
  },
]
