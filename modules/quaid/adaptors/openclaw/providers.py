"""OpenClaw-specific provider implementations."""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from lib.providers import LLMProvider, LLMResult

logger = logging.getLogger(__name__)


class GatewayLLMProvider(LLMProvider):
    """Routes LLM calls through the OpenClaw gateway HTTP endpoint.

    The gateway handles credential management (API keys, OAuth refresh).
    Quaid Python code never touches auth - it sends prompts to the gateway
    and gets responses back.
    """

    def __init__(
        self,
        port: int = 18789,
        token: str = "",
        *,
        deep_model: str = "",
        fast_model: str = "",
        default_provider: str = "anthropic",
        fast_reasoning_effort: str = "",
        deep_reasoning_effort: str = "",
    ):
        self._port = port
        self._token = (token or "").strip() or self._resolve_gateway_token()
        self._deep_model = str(deep_model or "").strip()
        self._fast_model = str(fast_model or "").strip()
        self._default_provider = str(default_provider or "anthropic").strip().lower() or "anthropic"
        self._fast_reasoning_effort = str(fast_reasoning_effort or "").strip().lower()
        self._deep_reasoning_effort = str(deep_reasoning_effort or "").strip().lower()

    @staticmethod
    def _resolve_gateway_token() -> str:
        env_token = str(os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")).strip()
        if env_token:
            return env_token
        cfg_path = Path.home() / ".openclaw" / "openclaw.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            gateway = cfg.get("gateway", {}) if isinstance(cfg, dict) else {}
            auth = gateway.get("auth", {}) if isinstance(gateway, dict) else {}
            mode = str(auth.get("mode", "")).strip().lower()
            token = str(auth.get("token", "")).strip()
            if mode == "token" and token:
                return token
        except Exception:
            pass
        return ""

    def _resolve_model_for_tier(self, model_tier: str) -> str:
        tier = "fast" if model_tier == "fast" else "deep"
        model = self._fast_model if tier == "fast" else self._deep_model
        if not model:
            raise RuntimeError(
                f"No model configured for tier '{tier}'. "
                "Set fastReasoning/deepReasoning in config/memory.json."
            )
        return model

    @staticmethod
    def _extract_openresponses_text(data: dict) -> str:
        if not isinstance(data, dict):
            return ""
        text = data.get("output_text")
        if isinstance(text, str) and text.strip():
            return text
        output = data.get("output")
        if isinstance(output, list):
            chunks = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("content"), list):
                    for content_item in item["content"]:
                        if isinstance(content_item, dict):
                            value = content_item.get("text")
                            if isinstance(value, str) and value:
                                chunks.append(value)
                elif isinstance(item.get("text"), str):
                    chunks.append(item["text"])
            if chunks:
                return "\n".join(chunks).strip()
        return ""

    def _llm_call_openresponses(
        self,
        *,
        system_prompt: str,
        user_message: str,
        model_tier: str,
        max_tokens: int,
        timeout: int,
        start_time: float,
    ) -> LLMResult:
        model = self._resolve_model_for_tier(model_tier)
        # v2026.3.28+: gateway /v1/responses only accepts "openclaw" as model name;
        # v2026.3.24+: per-request model selection moved to x-openclaw-model header.
        # Format: provider/model (e.g. anthropic/claude-haiku-4-5).
        provider_prefix = self._default_provider or "anthropic"
        oc_model = model if "/" in model else f"{provider_prefix}/{model}"
        effort = self._fast_reasoning_effort if model_tier == "fast" else self._deep_reasoning_effort
        body_dict: dict = {
            "model": "openclaw",
            "instructions": system_prompt,
            "input": user_message,
            "max_output_tokens": max_tokens,
        }
        if effort and effort != "none":
            body_dict["reasoning"] = {"effort": effort}
        body = json.dumps(body_dict).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "x-openclaw-scopes": "operator.write",
            "x-openclaw-model": oc_model,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/v1/responses",
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                raise RuntimeError(
                    f"Gateway OpenResponses returned non-object JSON payload: {type(data).__name__}"
                )
            usage = data.get("usage", {}) if isinstance(data.get("usage"), dict) else {}
            duration = time.time() - start_time
            return LLMResult(
                text=self._extract_openresponses_text(data),
                duration=duration,
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or 0),
                cache_read_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
                cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
                model=str(data.get("model", model) or model),
                truncated=bool(data.get("incomplete", False)),
            )

    def llm_call(self, messages, model_tier="deep",
                 max_tokens=4000, timeout=600):
        system_prompt = ""
        user_message = ""
        for m in messages:
            if m["role"] == "system":
                system_prompt = m["content"]
            elif m["role"] == "user":
                user_message = m["content"]

        return self._llm_call_openresponses(
            system_prompt=system_prompt,
            user_message=user_message,
            model_tier=model_tier,
            max_tokens=max_tokens,
            timeout=timeout,
            start_time=time.time(),
        )

    def get_profiles(self):
        return {
            "deep": {"model": "configured-via-gateway", "available": True},
            "fast": {"model": "configured-via-gateway", "available": True},
        }
