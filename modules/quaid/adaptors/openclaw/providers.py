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

    def __init__(self, port: int = 18789, token: str = ""):
        self._port = port
        self._token = token
        self._fallback_models = {
            "fast": "claude-haiku-4-5",
            "deep": "claude-opus-4-6",
        }

    def _resolve_model_for_tier(self, model_tier: str) -> str:
        tier = "fast" if model_tier == "fast" else "deep"
        workspace_root = (
            os.environ.get("QUAID_HOME")
            or os.environ.get("CLAWDBOT_WORKSPACE")
            or ""
        ).strip()
        if workspace_root:
            cfg_path = Path(workspace_root) / "config" / "memory.json"
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                models = cfg.get("models", {}) if isinstance(cfg, dict) else {}
                candidate = models.get("fastReasoning" if tier == "fast" else "deepReasoning")
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            except Exception:
                pass
        return self._fallback_models[tier]

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
        body = json.dumps({
            "model": model,
            "instructions": system_prompt,
            "input": user_message,
            "max_output_tokens": max_tokens,
        }).encode("utf-8")
        headers = {"Content-Type": "application/json"}
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

        body = json.dumps({
            "system_prompt": system_prompt,
            "user_message": user_message,
            "model_tier": model_tier,
            "max_tokens": max_tokens,
        }).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        url = f"http://127.0.0.1:{self._port}/plugins/quaid/llm"
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")

        retries = 1
        start_time = time.time()
        last_error = None
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if not isinstance(data, dict):
                        raise RuntimeError(
                            f"Gateway LLM proxy returned non-object JSON payload: {type(data).__name__}"
                        )
                    duration = time.time() - start_time
                    return LLMResult(
                        text=data.get("text"),
                        duration=duration,
                        input_tokens=data.get("input_tokens", 0),
                        output_tokens=data.get("output_tokens", 0),
                        cache_read_tokens=data.get("cache_read_tokens", 0),
                        cache_creation_tokens=data.get("cache_creation_tokens", 0),
                        model=data.get("model", ""),
                        truncated=data.get("truncated", False),
                    )
            except urllib.error.HTTPError as e:
                try:
                    err_body = json.loads(e.read().decode("utf-8"))
                    if isinstance(err_body, dict):
                        err_msg = err_body.get("error", str(e))
                    else:
                        err_msg = str(e)
                except Exception:
                    err_msg = str(e)
                logger.warning("Gateway LLM proxy HTTP error (%s): %s", e.code, err_msg)
                # Fallback: some gateway builds do not mount /plugins/quaid/llm.
                # In that case, call OpenResponses directly through the gateway.
                if e.code in {404, 405}:
                    try:
                        return self._llm_call_openresponses(
                            system_prompt=system_prompt,
                            user_message=user_message,
                            model_tier=model_tier,
                            max_tokens=max_tokens,
                            timeout=timeout,
                            start_time=start_time,
                        )
                    except Exception as fallback_err:
                        logger.warning("Gateway OpenResponses fallback failed: %s", fallback_err)
                        raise fallback_err
                if e.code == 503:
                    raise RuntimeError(
                        f"No credential configured for selected model provider (HTTP {e.code}): {err_msg}"
                    ) from e
                retryable = e.code in {429, 500, 502, 503, 504}
                last_error = e
                if retryable and attempt < retries:
                    time.sleep(0.25 * (2 ** attempt))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                logger.warning("Gateway LLM proxy transient error: %s", e)
                last_error = e
                if attempt < retries:
                    time.sleep(0.25 * (2 ** attempt))
                    continue
                raise
            except Exception as e:
                logger.error("Gateway LLM proxy error: %s", e)
                raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("Gateway LLM proxy call failed without error detail")

    def get_profiles(self):
        return {
            "deep": {"model": "configured-via-gateway", "available": True},
            "fast": {"model": "configured-via-gateway", "available": True},
        }
