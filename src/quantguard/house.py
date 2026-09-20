"""The only network-capable component. The organizer injects the trusted route.

There is no fallback endpoint, model, SDK retry, redirect, or vendor tool. The
request reservation counts even if a connection fails or a response is lost.
"""
from __future__ import annotations
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from .errors import BudgetExceeded, ConfigurationError, ModelError


@dataclass
class Budget:
    max_calls: int = 25
    max_input_tokens: int = 1_000_000
    max_output_tokens: int = 4000
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    deadline: float = float("inf")

    def __post_init__(self):
        if not 1 <= self.max_calls <= 25:
            raise ConfigurationError("max_calls must be between 1 and 25")
        if not 1 <= self.max_input_tokens <= 1_000_000:
            raise ConfigurationError("input token cap exceeds House allowance")
        if not 1 <= self.max_output_tokens <= 4000:
            raise ConfigurationError("output token cap exceeds House allowance")

    def reserve(self, messages: list[dict]) -> int:
        # A deliberately conservative UTF-8 byte bound plus template overhead;
        # no unapproved tokenizer checkpoint is downloaded or loaded.
        reservation = 2 * len(json.dumps(messages, ensure_ascii=False).encode()) + 4096
        if time.monotonic() >= self.deadline or self.calls >= self.max_calls:
            raise BudgetExceeded("request or wall-clock allowance exhausted")
        if self.input_tokens + reservation > self.max_input_tokens:
            raise BudgetExceeded("input token allowance exhausted")
        self.calls += 1
        self.input_tokens += reservation
        return reservation

    def reconcile(self, reservation: int, usage: dict):
        prompt = usage.get("prompt_tokens", 0)
        completion = usage.get("completion_tokens", 0)
        if not isinstance(prompt, int) or not isinstance(completion, int) or min(prompt, completion) < 0:
            raise ModelError("invalid usage metadata")
        # Never refund estimates: retries and missing usage remain charged.
        self.input_tokens += max(0, prompt - reservation)
        self.output_tokens += completion
        if self.input_tokens > self.max_input_tokens or completion > self.max_output_tokens:
            raise BudgetExceeded("House reported usage beyond the configured cap")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ModelError("House redirect refused")


class HouseClient:
    def __init__(self, budget: Budget, timeout: float = 120, environ=None):
        env = os.environ if environ is None else environ
        missing = [key for key in ("MODEL_ENDPOINT", "MODEL_NAME", "MODEL_TOKEN") if not env.get(key)]
        if missing:
            raise ConfigurationError("missing House configuration: " + ", ".join(missing))
        endpoint = env["MODEL_ENDPOINT"].rstrip("/")
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ConfigurationError("MODEL_ENDPOINT must be an injected origin without path or credentials")
        try:
            parsed.port
        except ValueError as exc:
            raise ConfigurationError("invalid House route port") from exc
        self.url = endpoint + "/v1/chat/completions"
        self.model = env["MODEL_NAME"]
        self._token = env["MODEL_TOKEN"]
        self.budget = budget
        self.timeout = timeout
        # ProxyHandler reads the organizer's HTTP(S)_PROXY unchanged. No route
        # is read from task content; generated programs never get these values.
        self._opener = urllib.request.build_opener(_NoRedirect())

    def generate(self, messages: list[dict], *, seed: int = 0) -> str:
        reserved = self.budget.reserve(messages)
        payload = {"model": self.model, "messages": messages, "max_tokens": self.budget.max_output_tokens,
                   "temperature": 0, "seed": seed, "n": 1, "stream": False,
                   "chat_template_kwargs": {"enable_thinking": False}}
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), method="POST",
                 headers={"Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        timeout = min(self.timeout, max(0.001, self.budget.deadline - time.monotonic()))
        request_deadline = min(self.budget.deadline, time.monotonic() + timeout)
        try:
            with self._opener.open(request, timeout=timeout) as response:
                chunks = []
                size = 0
                while size <= 1_000_000:
                    remaining = request_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("House response exceeded deadline")
                    # read1 returns available bytes instead of accumulating a
                    # large requested body while a slow peer keeps dribbling.
                    try:
                        response.fp.raw._sock.settimeout(remaining)
                    except AttributeError:
                        pass
                    block = response.read1(min(16384, 1_000_001 - size))
                    if not block:
                        break
                    chunks.append(block)
                    size += len(block)
                raw = b"".join(chunks)
            if len(raw) > 1_000_000:
                raise ModelError("House response exceeded size limit")
            data = json.loads(raw)
            self.budget.reconcile(reserved, data.get("usage", {}))
            choices = data.get("choices", [])
            if len(choices) != 1:
                raise ModelError("House must return exactly one candidate")
            if choices[0].get("finish_reason") == "length":
                raise ModelError("House response was truncated")
            content = choices[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ModelError("House returned no program")
            return content
        except urllib.error.HTTPError as exc:
            raise ModelError(f"House HTTP {exc.code}; request counted, no automatic retry") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ModelError("House connection failed; request counted, no automatic retry") from None
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            raise ModelError("House returned an invalid response") from None
