"""Task routing across multiple LiteLLM providers.

LiteLLM's Router sends one model-group alias to several deployments and, on a
rate limit, walks a fallback list. This module is that selection step for our
own task names. Calling stays on ``litellm.completion`` so each deployment keeps
its own key, base URL and cooldown. No task is reassigned until ``tasks`` says so.
"""

import os
import re

DIFFICULTY = {"routine": 1, "standard": 2, "hard": 3}
_ID = re.compile(r"[A-Za-z0-9_-]+")


class Endpoint:
    def __init__(self, id, model, api_base, api_key, difficulty, token_budget, request_params=None):
        self.id = id
        self.model = model
        self.api_base = api_base or ""
        self.api_key = api_key or ""
        self.difficulty = difficulty
        self.token_budget = token_budget
        self.request_params = request_params or {}


class RouteTable:
    def __init__(self, primary, endpoints, tasks):
        self.primary = primary
        self.endpoints = endpoints
        self.tasks = tasks

    def get(self, endpoint_id):
        if endpoint_id == self.primary.id:
            return self.primary
        try:
            return self.endpoints[endpoint_id]
        except KeyError:
            raise ValueError("unknown_endpoint:" + endpoint_id) from None

    def preferred(self, task):
        return self.candidates(task, {})[0]

    def candidates(self, task, usage):
        """Endpoints to try, first choice first. Unassigned tasks stay on primary."""
        spec = self.tasks.get(task)
        if not spec:
            return [self.primary]
        if spec.get("endpoint"):
            ordered = [self.get(spec["endpoint"])]
            ordered.extend(self.get(item) for item in spec.get("fallbacks", ()))
            return _unique(ordered)
        need = spec["min_difficulty"]
        pool = [item for item in self.endpoints.values() if item.difficulty >= need]
        if not pool:
            raise ValueError("no_endpoint_for_task:" + str(task))
        pool.sort(key=lambda item: (-_headroom(item, usage), item.id))
        return pool


def parse_routes(ai):
    ai = ai or {}
    primary = Endpoint(
        "primary",
        ai.get("MODEL") or "",
        ai.get("API_BASE") or "",
        ai.get("API_KEY") or "",
        1,
        None,
    )
    endpoints = {}
    for raw in ai.get("ENDPOINTS") or []:
        if not isinstance(raw, dict):
            raise ValueError("endpoint must be a mapping")
        endpoint_id = raw.get("id")
        if not isinstance(endpoint_id, str) or not _ID.fullmatch(endpoint_id):
            raise ValueError("endpoint id must contain only letters, numbers, _ and -")
        if endpoint_id in endpoints or endpoint_id == "primary":
            raise ValueError("duplicate_endpoint:" + endpoint_id)
        model = raw.get("model")
        if not isinstance(model, str) or "/" not in model:
            raise ValueError("endpoint model must use provider/model form")
        request_params = raw.get("request_params") or {}
        if not isinstance(request_params, dict):
            raise ValueError("request_params must be a mapping")
        endpoints[endpoint_id] = Endpoint(
            endpoint_id,
            model,
            raw.get("api_base") or "",
            _key(raw, ai.get("API_KEY") or ""),
            _difficulty(raw.get("difficulty", "routine")),
            _budget(raw.get("token_budget")),
            request_params,
        )
    tasks = {}
    for name, spec in (ai.get("TASKS") or {}).items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("task name must be a nonempty string")
        tasks[name] = _task(name, spec, endpoints)
    return RouteTable(primary, endpoints, tasks)


def is_rate_limit(exc):
    body = getattr(exc, "body", None)
    code = ""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        code = str(body.get("error", {}).get("code", ""))
    return (
        getattr(exc, "status_code", None) == 429
        or type(exc).__name__ == "RateLimitError"
        or code in ("1302", "1305")
    )


def apply_no_thinking(params):
    """Turn thinking off. Models that cannot disable it get their lowest setting."""
    model = str(params.get("model") or "").lower()
    extra = dict(params.get("extra_body") or {})
    if "kimi" in model:
        # Non-thinking Kimi only accepts temperature 0.6.
        extra["thinking"] = {"type": "disabled"}
        params["temperature"] = 0.6
    elif "glm" in model or "deepseek" in model:
        extra["thinking"] = {"type": "disabled"}
    elif model.startswith("groq/") and "qwen" in model:
        params["reasoning_effort"] = "none"
    elif model.startswith("groq/") and "gpt-oss" in model:
        # Groq rejects "none" for gpt-oss. "low" is the smallest effort it allows.
        params["reasoning_effort"] = "low"
        extra["include_reasoning"] = False
    elif model.startswith("gemini/"):
        # Gemini 3 has no full off switch. LiteLLM maps this to its lowest level.
        params["reasoning_effort"] = "none"
    if extra:
        params["extra_body"] = extra
    return params


def should_forward(exc):
    """Provider pressure or an unreachable deployment, not a bad prompt."""
    status = getattr(exc, "status_code", None) or 0
    name = type(exc).__name__
    return (
        is_rate_limit(exc)
        or "Timeout" in name
        or "Connection" in name
        or status >= 500
    )


def _key(raw, primary_key):
    if raw.get("api_key"):
        return raw["api_key"]
    env_name = raw.get("api_key_env") or "AI_API_KEY"
    if not isinstance(env_name, str) or not env_name.strip():
        raise ValueError("api_key_env must be an environment variable name")
    found = os.environ.get(env_name, "")
    if found:
        return found
    return primary_key if env_name == "AI_API_KEY" else ""


def _difficulty(value):
    if isinstance(value, bool) or (
        not isinstance(value, int) and value not in DIFFICULTY
    ):
        raise ValueError("difficulty must be routine, standard, hard, or a positive integer")
    if isinstance(value, int):
        if value < 1:
            raise ValueError("difficulty must be a positive integer")
        return value
    return DIFFICULTY[value]


def _budget(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("token_budget must be a positive integer")
    return value


def _task(name, spec, endpoints):
    if isinstance(spec, str):
        spec = {"endpoint": spec}
    if not isinstance(spec, dict):
        raise ValueError("task assignment must name an endpoint or a difficulty")
    endpoint = spec.get("endpoint")
    fallbacks = spec.get("fallbacks") or []
    minimum = spec.get("min_difficulty")
    if endpoint is not None and minimum is not None:
        raise ValueError("task cannot both pin an endpoint and set min_difficulty")
    if not isinstance(fallbacks, (list, tuple)) or any(not isinstance(item, str) for item in fallbacks):
        raise ValueError("fallbacks must be a list of endpoint ids")
    if endpoint is None and minimum is None:
        raise ValueError("task " + name + " needs endpoint or min_difficulty")
    if endpoint is not None:
        _known(endpoint, endpoints)
    for item in fallbacks:
        _known(item, endpoints)
    if endpoint is not None and endpoint in fallbacks:
        raise ValueError("fallback repeats the assigned endpoint")
    return {
        "endpoint": endpoint,
        "fallbacks": tuple(fallbacks),
        "min_difficulty": None if minimum is None else _difficulty(minimum),
    }


def _known(endpoint_id, endpoints):
    if endpoint_id not in endpoints:
        raise ValueError("unknown_endpoint:" + endpoint_id)


def _headroom(endpoint, usage):
    if not endpoint.token_budget:
        return 0
    return endpoint.token_budget - usage.get(endpoint.id, 0)


def _unique(items):
    found = []
    seen = set()
    for item in items:
        if item.id not in seen:
            seen.add(item.id)
            found.append(item)
    return found
