"""OpenClaw-specific provider implementations."""

import json
import sys
import time
import urllib.error
import urllib.request

from lib.providers import LLMProvider, LLMResult


class GatewayLLMProvider(LLMProvider):
    """Routes LLM calls through the OpenClaw gateway HTTP endpoint.

    The gateway handles credential management (API keys, OAuth refresh).
    Quaid Python code never touches auth - it sends prompts to the gateway
    and gets responses back.
    """

    def __init__(self, port: int = 18789, token: str = ""):
        self._port = port
        self._token = token

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

        start_time = time.time()
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
                err_msg = err_body.get("error", str(e))
            except Exception:
                err_msg = str(e)
            print(f"[providers] Gateway LLM proxy error ({e.code}): {err_msg}",
                  file=sys.stderr)
            if e.code == 503:
                raise RuntimeError(
                    f"No credential configured for selected model provider (HTTP {e.code}): {err_msg}"
                ) from e
            raise
        except Exception as e:
            print(f"[providers] Gateway LLM proxy error: {e}", file=sys.stderr)
            raise

    def get_profiles(self):
        return {
            "deep": {"model": "configured-via-gateway", "available": True},
            "fast": {"model": "configured-via-gateway", "available": True},
        }
