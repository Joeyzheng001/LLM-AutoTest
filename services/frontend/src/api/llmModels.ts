/**
 * 部门内部 LLM 中转站允许选择的模型别名。
 *
 * 后端仍接受任意非空模型名，避免中转站新增模型时旧客户端无法运行；此目录只负责
 * 给配置页和重跑页提供一致、可理解的选择项。图像生成模型可被选择以适配中转站能力，
 * 但当前流水线需要 chat/completions 的文本 JSON 输出，页面会明确提示不建议使用。
 */

export type LlmModelCapability = 'text' | 'multimodal' | 'image'
export type LlmProtocol = 'openai-chat' | 'openai-responses' | 'anthropic-messages'

export interface LlmModelOption {
  value: string
  label: string
  capability: LlmModelCapability
}

export interface LlmModelGroup {
  label: string
  options: LlmModelOption[]
}

export const DEFAULT_RELAY_MODEL = 'deepseek-reasoner-code'

export const LLM_PROTOCOL_OPTIONS: Array<{ value: LlmProtocol; label: string; hint: string }> = [
  { value: 'openai-chat', label: 'OpenAI · Chat Completions', hint: 'POST /v1/chat/completions · Bearer Key' },
  { value: 'openai-responses', label: 'OpenAI · Responses', hint: 'POST /v1/responses · Bearer Key' },
  { value: 'anthropic-messages', label: 'Anthropic · Messages', hint: 'POST /v1/messages · x-api-key' },
]

export const LLM_MODEL_GROUPS: LlmModelGroup[] = [
  {
    label: '推理与代码（推荐）',
    options: [
      { value: 'deepseek-reasoner-code', label: 'DeepSeek Reasoner Code', capability: 'text' },
      { value: 'deepseek-reasoner', label: 'DeepSeek Reasoner', capability: 'text' },
      { value: 'deepseek-v4-pro', label: 'DeepSeek V4 Pro', capability: 'text' },
      { value: 'cloud/Kimi-K2.7-Code', label: 'Kimi K2.7 Code（中转站）', capability: 'text' },
      { value: 'cloud/Kimi-K3', label: 'Kimi K3（中转站）', capability: 'text' },
      { value: 'cloud/glm-5.2', label: 'GLM 5.2（中转站）', capability: 'text' },
      { value: 'GLM-5.2', label: 'GLM 5.2', capability: 'text' },
      { value: 'cloud/claude-5-opus', label: 'Claude 5 Opus（中转站）', capability: 'text' },
      { value: 'cloud/claude-5-fable', label: 'Claude 5 Fable（中转站）', capability: 'text' },
      { value: 'cloud/gpt-5.6-sol', label: 'GPT 5.6 Sol（中转站）', capability: 'text' },
      { value: 'cloud/gemini-3.1-pro-preview', label: 'Gemini 3.1 Pro Preview（中转站）', capability: 'multimodal' },
    ],
  },
  {
    label: '通用与多模态',
    options: [
      { value: 'deepseek-chat', label: 'DeepSeek Chat', capability: 'text' },
      { value: 'qwen-chat', label: 'Qwen Chat', capability: 'text' },
      {
        value: 'Qwen2.5-VL-72B-Instruct-8k',
        label: 'Qwen 2.5 VL 72B Instruct 8k',
        capability: 'multimodal',
      },
    ],
  },
  {
    label: '图像生成（当前文本流水线不推荐）',
    options: [
      { value: 'cloud/gpt-image-2', label: 'GPT Image 2（中转站）', capability: 'image' },
      {
        value: 'cloud/gemini-3.1-flash-image-preview',
        label: 'Gemini 3.1 Flash Image Preview（中转站）',
        capability: 'image',
      },
      {
        value: 'cloud/gemini-3.0-pro-image-preview',
        label: 'Gemini 3.0 Pro Image Preview（中转站）',
        capability: 'image',
      },
    ],
  },
  {
    label: '历史任务兼容',
    options: [
      { value: 'MiniMax-M3', label: 'MiniMax M3', capability: 'text' },
      { value: 'MiniMax-M2.7', label: 'MiniMax M2.7', capability: 'text' },
      { value: 'MiniMax-M2.5', label: 'MiniMax M2.5', capability: 'text' },
    ],
  },
]

const modelByValue = new Map(
  LLM_MODEL_GROUPS.flatMap((group) => group.options).map((option) => [option.value, option]),
)

export function llmModelWarning(model: string): string | null {
  const option = modelByValue.get(model)
  if (option?.capability === 'image') {
    return '该模型以图像生成为主。当前需求解析、Code Review 与用例生成需要文本 JSON 输出，不建议用于流水线。'
  }
  if (option?.capability === 'multimodal') {
    return '该模型为多模态模型。请先确认内部中转站的 chat/completions 响应包含稳定的文本 JSON。'
  }
  return null
}

export function isKnownLlmModel(model: string): boolean {
  return modelByValue.has(model)
}
