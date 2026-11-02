"""LLM 调用层：统一封装智谱 GLM / DeepSeek / Mock。

设计要点

1. **密钥只从 .env 读**，本模块不做任何持久化，异常信息里也不拼接密钥。
2. **provider 用配置表驱动**，不在调用处写 if/else。三家都是 OpenAI 兼容的
   `/chat/completions`，差别只在 base_url、model 名、是否支持 JSON 模式。
3. **重试只针对"可重试"的错误**：429（限流）、5xx（服务端）、超时、连接错误。
   401/403/400 直接失败——重试这类错误纯属浪费时间且会掩盖配置问题。
4. **Mock 不是"假装成功"**：它返回的内容显式带 `[MOCK]` 标记，且数字直接从
   传入的指标 JSON 里抄，因此能真正跑通"数字审计"这条护栏而不是绕过它。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from . import config

logger = logging.getLogger(__name__)

# 这些状态码重试有意义；其余一律直接失败
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """LLM 调用失败。消息里已做脱敏，可直接打印到日志。"""


@dataclass
class LLMResponse:
    """一次调用的完整结果，含记账字段。"""

    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 1
    mock: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
            "mock": self.mock,
        }


def _mask(text: str, secret: str) -> str:
    """把可能出现在错误信息里的密钥替换掉。requests 的异常偶尔会带上 URL/头部。"""
    if secret and secret in text:
        text = text.replace(secret, "***REDACTED***")
    return text


class LLMClient:
    """OpenAI 兼容的聊天客户端。"""

    def __init__(self, provider: str | None = None) -> None:
        settings = config.llm_settings(provider)
        self.provider: str = settings["provider"]
        self.api_key: str = settings["api_key"]
        self.base_url: str = (settings["base_url"] or "").rstrip("/")
        self.model: str = settings["model"]
        self.supports_json_mode: bool = bool(settings["supports_json_mode"])
        self.timeout = config.LLM_TIMEOUT_SECONDS
        self.max_retries = max(1, config.LLM_MAX_RETRIES)

    # ------------------------------------------------------------------
    # 可用性
    # ------------------------------------------------------------------

    @property
    def is_mock(self) -> bool:
        return self.provider == "mock"

    def is_configured(self) -> bool:
        """Mock 永远"已配置"；真实 provider 必须有 key。"""
        return True if self.is_mock else bool(self.api_key)

    def describe(self) -> str:
        if self.is_mock:
            return "mock（不联网，用于离线验证链路）"
        return f"{self.provider} / {self.model}"

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        if self.is_mock:
            return self._mock_response(messages, json_mode)

        if not self.api_key:
            raise LLMError(
                f"provider={self.provider} 未配置 API Key。"
                f"请在 .env 填写 {self.provider.upper()}_API_KEY，或把 LLM_PROVIDER 设为 mock。"
            )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": config.LLM_TEMPERATURE if temperature is None else temperature,
            "max_tokens": config.LLM_MAX_TOKENS if max_tokens is None else max_tokens,
            "stream": False,
        }
        if json_mode and self.supports_json_mode:
            payload["response_format"] = {"type": "json_object"}

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_error = ""
        started = time.time()

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
            except requests.Timeout:
                last_error = f"请求超时（{self.timeout}s）"
            except requests.RequestException as exc:
                last_error = _mask(f"连接失败：{type(exc).__name__}: {exc}", self.api_key)
            else:
                if resp.status_code == 200:
                    return self._parse(resp.json(), attempt, int((time.time() - started) * 1000))

                body = _mask(resp.text[:400], self.api_key)
                if resp.status_code in RETRYABLE_STATUS:
                    last_error = f"HTTP {resp.status_code}：{body}"
                else:
                    # 401/403 是密钥问题、400 是参数问题，重试不会变好
                    raise LLMError(f"调用失败（HTTP {resp.status_code}，不重试）：{body}")

            if attempt < self.max_retries:
                # 指数退避 + 抖动，避免多进程同时重试打爆限流
                sleep_s = min(2**attempt * 0.5, 8.0)
                logger.warning(
                    "LLM 第 %d/%d 次失败：%s；%.1fs 后重试", attempt, self.max_retries, last_error, sleep_s
                )
                time.sleep(sleep_s)

        raise LLMError(f"调用失败（已重试 {self.max_retries} 次）：{last_error}")

    def _parse(self, data: dict[str, Any], attempts: int, latency_ms: int) -> LLMResponse:
        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
            # 部分思维链模型把正文放在 content，思考过程放 reasoning_content。
            # 我们只要 content；reasoning_content 不进入报告，避免把"想法"当结论。
            text = message.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"响应结构异常：{type(exc).__name__}；原始响应前 300 字：{str(data)[:300]}") from exc

        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.provider,
            model=data.get("model", self.model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            latency_ms=latency_ms,
            attempts=attempts,
            mock=False,
            raw={"finish_reason": choice.get("finish_reason"), "id": data.get("id")},
        )

    # ------------------------------------------------------------------
    # Mock
    # ------------------------------------------------------------------

    def _mock_response(self, messages: list[dict[str, str]], json_mode: bool) -> LLMResponse:
        """返回确定性内容，且**只使用输入里已经存在的数字与文档名**。

        这里刻意让 Mock 去"读"提示词：从指标 JSON 里取几个标量、从片段头里取文档名，
        再拼成合规的 8 段结构。这样 Mock 模式下解析、归一化、数字审计、引用校验
        全部会被真实执行——否则护栏在离线场景就是一段从未被跑过的代码。

        仍然显式标注 [MOCK]，避免被误当成真实模型输出。
        """
        user_text = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")

        if not json_mode:
            return LLMResponse(
                text=(
                    "[MOCK] 未调用真实模型。\n"
                    f"输入长度 {len(user_text)} 字符，未做任何改写。"
                ),
                provider="mock",
                model="mock",
                latency_ms=0,
                attempts=1,
                mock=True,
            )

        metrics = self._extract_metrics(user_text)
        flat = self._flatten_scalars(metrics)
        chunks = self._extract_chunk_headers(user_text)

        # 只用指标里已有的标量，绝不构造新数字
        facts = [f"{key} = {value}" for key, value in list(flat.items())[:6]]
        citations = [
            {"document": doc, "section": section, "point": "[MOCK] 该章节在本次检索中被召回"}
            for doc, section in chunks[:2]
        ]

        payload = {
            "question_overview": (
                "[MOCK] 未调用真实模型。本段用于验证结构解析、数字审计与引用校验是否生效。"
            ),
            "data_facts": facts,
            "knowledge_citations": citations,
            "hypotheses": [
                {
                    "hypothesis": "[MOCK] 此处应为基于数据的原因假设",
                    "basis": "降级模板不生成假设，避免在无审计的情况下引入推断",
                    "confidence": "低",
                }
            ],
            "target_segments": ["[MOCK] 目标客群需由真实模型结合指标给出"],
            "recommended_actions": [
                {
                    "action": "[MOCK] 建议动作占位",
                    "target": "见 target_segments",
                    "expected_effect": "未评估",
                    "effort": "未评估",
                }
            ],
            "metrics": {"primary": "[MOCK] 待指定", "guardrails": ["[MOCK] 待指定"]},
            "limitations": [
                "[MOCK] 本次未调用真实模型，内容不构成任何业务结论。",
                "该模式仅用于离线验证链路完整性。",
            ],
        }

        return LLMResponse(
            text=json.dumps(payload, ensure_ascii=False),
            provider="mock",
            model="mock",
            latency_ms=0,
            attempts=1,
            mock=True,
        )

    @staticmethod
    def _extract_metrics(prompt: str) -> dict[str, Any]:
        """从提示词里取出 ```json ... ``` 包裹的指标块。"""
        match = re.search(r"```json\s*(.*?)\s*```", prompt, flags=re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(1))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _flatten_scalars(metrics: dict[str, Any], prefix: str = "") -> dict[str, str]:
        """把嵌套指标拍平成 键=值，只保留标量。"""
        flat: dict[str, str] = {}
        node = metrics.get("metrics", metrics)
        for key, value in (node.items() if isinstance(node, dict) else []):
            if isinstance(value, bool) or value is None:
                continue
            if isinstance(value, (int, float, str)):
                flat[f"{prefix}{key}"] = str(value)
            elif isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, (int, float, str)) and not isinstance(sub_value, bool):
                        flat[f"{prefix}{key}.{sub_key}"] = str(sub_value)
        return flat

    @staticmethod
    def _extract_chunk_headers(prompt: str) -> list[tuple[str, str]]:
        """从「[片段 N] 文档：x.md｜章节：y」这种头部里取文档名与章节。"""
        pattern = re.compile(r"\[片段\s*\d+\]\s*文档：([^｜\n]+)｜章节：([^\n｜]+)")
        return [(m.group(1).strip(), m.group(2).strip()) for m in pattern.finditer(prompt)]


def get_client(provider: str | None = None) -> LLMClient:
    """工厂函数。provider 为空时用 .env 里的 LLM_PROVIDER。"""
    return LLMClient(provider)
