# coding=utf-8
"""
AI 客户端模块

基于 LiteLLM 的统一 AI 模型接口
支持 100+ AI 提供商（OpenAI、DeepSeek、Gemini、Claude、国内模型等）
"""

import os
from typing import Any, Dict, List

from litellm import completion
from litellm.llms.vertex_ai.gemini.vertex_and_google_ai_studio_gemini import (
    VertexGeminiConfig,
)

from trendradar.ai.router import apply_no_thinking, parse_routes, should_forward

# Gemini 3.5 accepts a zero thinking budget. This LiteLLM build maps "none" to
# thinking level "low" for that model, which still spends reasoning tokens.
_map_gemini_thinking_level = VertexGeminiConfig._map_reasoning_effort_to_thinking_level


def _gemini_thinking_level(reasoning_effort, model=None):
    if reasoning_effort in ("none", "disable"):
        return {"thinkingBudget": 0, "includeThoughts": False}
    return _map_gemini_thinking_level(reasoning_effort, model)


VertexGeminiConfig._map_reasoning_effort_to_thinking_level = staticmethod(
    _gemini_thinking_level
)


class AIClient:
    """统一的 AI 客户端（基于 LiteLLM）"""

    def __init__(self, config: Dict[str, Any]):
        """
        初始化 AI 客户端

        Args:
            config: AI 配置字典
                - MODEL: 模型标识（格式: provider/model_name）
                - API_KEY: API 密钥
                - API_BASE: API 基础 URL（可选）
                - TEMPERATURE: 采样温度
                - MAX_TOKENS: 最大生成 token 数
                - TIMEOUT: 请求超时时间（秒）
                - NUM_RETRIES: 重试次数（可选）
                - FALLBACK_MODELS: 备用模型列表（可选）
                - ENDPOINTS / TASKS: 多接口分配；未分配的任务仍走 MODEL
        """
        self.model = config.get("MODEL", "deepseek/deepseek-chat")
        self.api_key = config.get("API_KEY") or os.environ.get("AI_API_KEY", "")
        self.api_base = config.get("API_BASE", "")
        self.temperature = config.get("TEMPERATURE", 1.0)
        self.max_tokens = config.get("MAX_TOKENS", 5000)
        self.timeout = config.get("TIMEOUT", 120)
        self.num_retries = config.get("NUM_RETRIES", 2)
        self.fallback_models = config.get("FALLBACK_MODELS", [])
        self.extra_params = config.get("EXTRA_PARAMS", {})
        self.routes = parse_routes({**config, "API_KEY": self.api_key})
        self._usage = {}
        self.last_usage = None
        self.last_cost = None
        self.last_endpoint = None

    def chat(self, messages: List[Dict[str, str]], task=None, endpoint_id=None, **kwargs) -> str:
        """
        调用 AI 模型进行对话

        Args:
            messages: 消息列表，格式: [{"role": "system/user/assistant", "content": "..."}]
            **kwargs: 额外参数，会覆盖默认配置

        Returns:
            str: AI 响应内容

        Raises:
            Exception: API 调用失败时抛出异常
        """
        if endpoint_id:
            return self._complete(self.routes.get(endpoint_id), messages, kwargs)
        last = None
        for endpoint in self.routes.candidates(task, self._usage):
            try:
                return self._complete(endpoint, messages, kwargs)
            except Exception as exc:
                last = exc
                if not should_forward(exc):
                    raise
        if last is None:
            raise RuntimeError("no_endpoint_available")
        raise last

    def _complete(self, endpoint, messages, kwargs):
        # 构建请求参数
        params = {
            "model": endpoint.model or self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "timeout": kwargs.get("timeout", self.timeout),
            "num_retries": kwargs.get("num_retries", self.num_retries),
        }

        params.update(self.extra_params)

        api_key = endpoint.api_key or self.api_key
        api_base = endpoint.api_base or (self.api_base if endpoint.id == "primary" else "")
        if api_key:
            params["api_key"] = api_key
        if api_base:
            params["api_base"] = api_base

        # 添加 max_tokens（如果配置了且不为 0）
        max_tokens = kwargs.get("max_tokens", self.max_tokens)
        if max_tokens and max_tokens > 0:
            params["max_tokens"] = max_tokens
        # Endpoint constraints override the caller, such as Kimi's fixed temperature.
        params.update(endpoint.request_params)

        # 单个 deployment 的失败由上面的候选列表转发，不再交给 LiteLLM 隐式换模型。
        # 只有主接口、且没有任务分配时，才保留原来的 fallback_models。
        if endpoint.id == "primary" and not self.routes.tasks and self.fallback_models:
            params["fallbacks"] = self.fallback_models

        # 合并其他额外参数
        for key, value in kwargs.items():
            if key not in params and key not in ("task", "endpoint_id"):
                params[key] = value
        apply_no_thinking(params)

        # 调用 LiteLLM
        self.last_usage = None
        self.last_cost = None
        self.last_endpoint = endpoint.id
        response = completion(**params)
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = {
                "input_tokens": getattr(usage, "prompt_tokens", None),
                "output_tokens": getattr(usage, "completion_tokens", None),
            }
            spent = (self.last_usage["input_tokens"] or 0) + (self.last_usage["output_tokens"] or 0)
            self._usage[endpoint.id] = self._usage.get(endpoint.id, 0) + spent
        # Only record a cost supplied by the provider/client; do not invent prices.
        self.last_cost = (getattr(response, "_hidden_params", None) or {}).get(
            "response_cost"
        )

        # 提取响应内容
        # 某些模型/提供商返回 list（内容块）而非 str，统一转为 str
        content = response.choices[0].message.content
        if isinstance(content, list):
            content = "\n".join(
                item.get("text", str(item)) if isinstance(item, dict) else str(item)
                for item in content
            )
        return content or ""

    def validate_config(self) -> tuple[bool, str]:
        """
        验证配置是否有效

        Returns:
            tuple: (是否有效, 错误信息)
        """
        if not self.model:
            return False, "未配置 AI 模型（model）"

        if not self.api_key:
            return (
                False,
                "未配置 AI API Key，请在 config.yaml 或环境变量 AI_API_KEY 中设置",
            )

        # 验证模型格式（应该包含 provider/model）
        if "/" not in self.model:
            return (
                False,
                f"模型格式错误: {self.model}，应为 'provider/model' 格式（如 'deepseek/deepseek-chat'）",
            )

        return True, ""
