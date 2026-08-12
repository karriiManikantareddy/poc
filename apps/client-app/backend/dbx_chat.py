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

    When `obo_token` is set (the caller's own forwarded X-Forwarded-Access-Token,
    requires `serving.serving-endpoints` in this app's user_api_scopes — see
    databricks.yml), the call runs AS that employee, not as the app's own
    identity — so their own model-serving permissions/quotas apply, not the
    app's. Falls back to the app's own identity when no token is available
    (e.g. local dev with no reverse proxy in front)."""

    endpoint: str
    temperature: float = 0.2
    max_tokens: int = 1024
    obo_token: Optional[str] = None

    @property
    def _llm_type(self) -> str:
        return "databricks-serving-endpoint"

    def bind_tools(self, tools, *, tool_choice: Optional[str] = None, **kwargs: Any):
        formatted = [convert_to_openai_tool(t) for t in tools]
        return self.bind(tools=formatted, **kwargs)

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        # auth_type="pat" is required: without it, Config also picks up this
        # app's own ambient DATABRICKS_CLIENT_ID/SECRET env vars and refuses
        # to proceed with "more than one authorization method configured" —
        # hit this for real building the group-lookup path, fixed here too.
        cfg = Config(token=self.obo_token, auth_type="pat") if self.obo_token else Config()
        headers = cfg.authenticate()
        url = f"{cfg.host}/serving-endpoints/{self.endpoint}/invocations"

        payload: dict[str, Any] = {
            "messages": [_to_openai_message(m) for m in messages],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if kwargs.get("tools"):
            payload["tools"] = kwargs["tools"]

        resp = requests.post(url, headers=headers, json=payload, timeout=60)
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
