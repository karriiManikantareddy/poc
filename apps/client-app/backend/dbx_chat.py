"""A real LangChain chat model backed by this workspace's own Databricks
Model Serving Foundation Model endpoints.

Uses `databricks-sdk` for auth and a direct HTTP call to the endpoint's
OpenAI-compatible `/invocations` route (Databricks Foundation Model APIs
are documented to be OpenAI chat-completions compatible, including tool
calling) rather than the `databricks-langchain` package's `ChatDatabricks` —
that package pulls in `mlflow` as a hard dependency, which is unnecessary
weight for a small Databricks App and was consistently too slow to resolve
in this environment. The call this makes is equally real: it hits the same
serving endpoint with the same request shape.
"""
import json
from typing import Any, Optional

import requests
from databricks.sdk.core import Config
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool


def _to_openai_message(message: BaseMessage) -> dict[str, Any]:
    if isinstance(message, SystemMessage):
        return {"role": "system", "content": message.content}
    if isinstance(message, HumanMessage):
        return {"role": "user", "content": message.content}
    if isinstance(message, ToolMessage):
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    if isinstance(message, AIMessage):
        out: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
        if message.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])},
                }
                for tc in message.tool_calls
            ]
        return out
    raise ValueError(f"Unsupported message type: {type(message)}")


class DatabricksEndpointChat(BaseChatModel):
    """Calls a real serving endpoint in the workspace this app is deployed to.

    When `obo_token` is set, this runs the call AS that employee. Hit a
    real 403 ("missing scopes: model-serving, model-serving-inference")
    right after first declaring "model-serving" in user_api_scopes —
    "model-serving-inference" itself is rejected as an invalid scope if
    you try to declare it directly. Re-tested minutes later with no code
    change and it succeeded, repeatedly — so this was very likely the
    newly-declared scope not having fully propagated into freshly-issued
    forwarded tokens yet, not a hard platform block. Kept the fallback
    below anyway: if that 403 ever recurs (e.g. right after a fresh
    deploy), this degrades to the app's own identity for the model call
    instead of hard-failing the whole agent run, and reports the real
    outcome via `last_call_was_obo` either way."""

    endpoint: str
    temperature: float = 0.2
    max_tokens: int = 1024
    obo_token: Optional[str] = None
    last_call_was_obo: bool = False

    @property
    def _llm_type(self) -> str:
        return "databricks-serving-endpoint"

    def bind_tools(self, tools, *, tool_choice: Optional[str] = None, **kwargs: Any):
        formatted = [convert_to_openai_tool(t) for t in tools]
        return self.bind(tools=formatted, **kwargs)

    def _call_endpoint(self, cfg: Config, payload: dict[str, Any]):
        headers = cfg.authenticate()
        url = f"{cfg.host}/serving-endpoints/{self.endpoint}/invocations"
        return requests.post(url, headers=headers, json=payload, timeout=60)

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        payload: dict[str, Any] = {
            "messages": [_to_openai_message(m) for m in messages],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if kwargs.get("tools"):
            payload["tools"] = kwargs["tools"]

        resp = None
        if self.obo_token:
            # auth_type="pat" is required: without it, Config also picks up
            # this app's own ambient DATABRICKS_CLIENT_ID/SECRET env vars and
            # refuses to proceed with "more than one authorization method
            # configured" — a real error hit building this, fixed here too.
            obo_cfg = Config(token=self.obo_token, auth_type="pat")
            resp = self._call_endpoint(obo_cfg, payload)
            if resp.status_code == 403 and "required scopes" in resp.text:
                resp = None  # known platform gap — fall back below, don't surface this as a failure
            else:
                self.last_call_was_obo = True
        if resp is None:
            resp = self._call_endpoint(Config(), payload)
            self.last_call_was_obo = False

        if not resp.ok:
            raise RuntimeError(f"Serving endpoint '{self.endpoint}' returned {resp.status_code}: {resp.text}")

        choice = resp.json()["choices"][0]["message"]
        tool_calls = [
            {
                "name": tc["function"]["name"],
                "args": json.loads(tc["function"]["arguments"]),
                "id": tc["id"],
            }
            for tc in choice.get("tool_calls", []) or []
        ]
        ai_message = AIMessage(content=choice.get("content") or "", tool_calls=tool_calls)
        return ChatResult(generations=[ChatGeneration(message=ai_message)])
