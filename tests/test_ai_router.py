import unittest
from types import SimpleNamespace
from unittest.mock import patch

from trendradar.ai.client import AIClient
from trendradar.ai.router import apply_no_thinking, parse_routes
from trendradar.papers.arxiv import Paper
from trendradar.papers.pipeline import cache_key


def paper():
    return Paper(
        "arxiv:1",
        1,
        "title",
        "abstract",
        ["author"],
        ["cs.LG"],
        "2026-10-01T00:00:00Z",
        "2026-10-01T00:00:00Z",
        "https://arxiv.org/abs/1",
        [],
    )


class RouterTests(unittest.TestCase):
    def test_difficulty_order_uses_remaining_budget(self):
        table = parse_routes(
            {
                "MODEL": "openai/primary",
                "ENDPOINTS": [
                    {
                        "id": "busy",
                        "model": "openai/busy",
                        "difficulty": "hard",
                        "token_budget": 100,
                    },
                    {
                        "id": "open",
                        "model": "openai/open",
                        "difficulty": 3,
                        "token_budget": 100,
                    },
                ],
                "TASKS": {"paper": {"min_difficulty": "hard"}},
            }
        )
        self.assertEqual(
            [item.id for item in table.candidates("paper", {"busy": 80, "open": 5})],
            ["open", "busy"],
        )
        self.assertEqual(table.preferred("summary").id, "primary")

    def test_assignment_does_not_change_unrelated_cache_identity(self):
        base = {"MODEL": "openai/glm-4.7-flash", "API_BASE": "https://glm.example"}
        assigned = {
            **base,
            "ENDPOINTS": [
                {"id": "backup", "model": "openai/backup", "difficulty": "hard"}
            ],
            "TASKS": {"paper": "backup"},
        }
        cfg = {"research_question": "q", "keyword_prefilter": False}
        self.assertEqual(cache_key(paper(), cfg, base), cache_key(paper(), cfg, {**base, "ENDPOINTS": [], "TASKS": {}}))
        self.assertNotEqual(cache_key(paper(), cfg, base), cache_key(paper(), cfg, assigned))

    def test_client_forwards_when_the_first_api_is_limited(self):
        class RateLimitError(Exception):
            status_code = 429

        calls = []

        def complete(**params):
            calls.append(params["model"])
            if len(calls) == 1:
                raise RateLimitError()
            message = SimpleNamespace(content="forwarded")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message)],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
                _hidden_params={},
            )

        client = AIClient(
            {
                "MODEL": "openai/primary",
                "API_KEY": "primary",
                "ENDPOINTS": [
                    {"id": "glm", "model": "openai/glm", "api_key": "glm"},
                    {"id": "backup", "model": "openai/backup", "api_key": "backup"},
                ],
                "TASKS": {"analysis": {"endpoint": "glm", "fallbacks": ["backup"]}},
            }
        )
        with patch("trendradar.ai.client.completion", complete):
            self.assertEqual(client.chat([{"role": "user", "content": "hi"}], task="analysis"), "forwarded")
        self.assertEqual(calls, ["openai/glm", "openai/backup"])
        self.assertEqual(client.last_endpoint, "backup")
        self.assertEqual(client.last_usage, {"input_tokens": 3, "output_tokens": 4})

    def test_endpoint_request_params_override_caller_temperature(self):
        seen = {}

        def complete(**params):
            seen.update(params)
            message = SimpleNamespace(content="ok")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message)],
                usage=None,
                _hidden_params={},
            )

        client = AIClient(
            {
                "MODEL": "openai/primary",
                "API_KEY": "primary",
                "ENDPOINTS": [
                    {
                        "id": "kimi",
                        "model": "openai/kimi-k2.6",
                        "api_base": "https://api.moonshot.cn/v1",
                        "api_key": "cn",
                        "request_params": {
                            "temperature": 0.6,
                            "extra_body": {"thinking": {"type": "disabled"}},
                        },
                    }
                ],
                "TASKS": {"summary": "kimi"},
            }
        )
        with patch("trendradar.ai.client.completion", complete):
            client.chat([{"role": "user", "content": "hi"}], task="summary", temperature=0)
        self.assertEqual(seen["temperature"], 0.6)
        self.assertEqual(seen["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(seen["api_base"], "https://api.moonshot.cn/v1")

    def test_thinking_is_disabled_for_every_provider(self):
        cases = {
            "openai/kimi-k2.6": {"temperature": 0.6, "extra_body": {"thinking": {"type": "disabled"}}},
            "openai/glm-4.7-flash": {"extra_body": {"thinking": {"type": "disabled"}}},
            "deepseek/deepseek-flash": {"extra_body": {"thinking": {"type": "disabled"}}},
            "groq/qwen/qwen3.8-27b": {"reasoning_effort": "none"},
            "groq/openai/gpt-oss-20b": {
                "reasoning_effort": "low",
                "extra_body": {"include_reasoning": False},
            },
            "gemini/gemini-3.5-flash": {"reasoning_effort": "none"},
        }
        for model, expected in cases.items():
            params = apply_no_thinking({"model": model, "temperature": 0})
            for key, value in expected.items():
                self.assertEqual(params[key], value, model)


if __name__ == "__main__":
    unittest.main()
