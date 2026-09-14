"""Homework 2 checks: tool span attributes and endpoint authentication.

Everything here is offline. No Langfuse, no Docker, no model provider key:
tool spans go to an in-memory exporter, and the endpoint is driven directly.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent.agent import build_agent, prompt_version
from agent.auth import AuthContext, permission_denied
from observability.instrument import record_tool_result
from tests.eval.fake_model import FakeModel, text_message

SHOPPER = AuthContext(user_id=1, role="shopper")
MERCHANT = AuthContext(user_id=9002, role="merchant", store_id=2)


@pytest.fixture
def tool_span():
    """Record one tool span locally and return its attributes.

    A local TracerProvider, never the global one: setting the process-wide
    provider from a test would leak into the rest of the suite.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test.cartwheel")

    def run(ctx: AuthContext, result: dict) -> dict:
        with tracer.start_as_current_span("get_order"):
            record_tool_result(ctx, result)
        (span,) = exporter.get_finished_spans()
        return dict(span.attributes)

    return run


def test_tool_span_records_merchant_identity(tool_span) -> None:
    attrs = tool_span(MERCHANT, {"ok": True, "order": {"id": 4127}})
    assert attrs["cartwheel.user_role"] == "merchant"
    # Both are strings: an id is a label spelled with digits, not a number.
    assert attrs["cartwheel.user_id"] == "9002"
    assert attrs["cartwheel.store_id"] == "2"
    assert attrs["cartwheel.permission_denied"] is False
    assert "cartwheel.permission_denied.reason" not in attrs


def test_tool_span_omits_store_id_for_a_shopper(tool_span) -> None:
    attrs = tool_span(SHOPPER, {"ok": True, "orders": []})
    assert attrs["cartwheel.user_id"] == "1"
    assert "cartwheel.store_id" not in attrs


def test_tool_span_records_a_permission_denial_and_its_reason(tool_span) -> None:
    reason = "shoppers may only view their own orders"
    attrs = tool_span(SHOPPER, permission_denied(reason))
    assert attrs["cartwheel.permission_denied"] is True
    assert attrs["cartwheel.permission_denied.reason"] == reason


def test_other_tool_errors_are_not_permission_denials(tool_span) -> None:
    attrs = tool_span(
        SHOPPER, {"ok": False, "error": "not_found", "reason": "no such order"}
    )
    assert attrs["cartwheel.permission_denied"] is False
    assert "cartwheel.permission_denied.reason" not in attrs


def test_recording_is_a_noop_when_tracing_is_off() -> None:
    """With no active span OTel returns a non-recording span; this must not raise."""
    record_tool_result(SHOPPER, {"ok": True})


# ---------------------------------------------------------------------------
# Part C: the cartwheel.session_message root span.
# ---------------------------------------------------------------------------

REPLY = "Your order is out for delivery."


@pytest.fixture
def endpoint(world, tmp_path, monkeypatch):
    """Drive one real request through the endpoint with an offline model.

    Three substitutions, all reverted by monkeypatch: a local tracer so the
    root span lands in this test instead of the global provider, a temp
    conversation store so the repository's .sessions.db is left alone, and a
    FakeModel so nothing reaches a provider.
    """
    from server import app as server_app

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(server_app, "_tracer", provider.get_tracer("test.cartwheel"))
    monkeypatch.setattr(server_app, "SESSIONS_DB", tmp_path / "sessions.db")

    def offline_agent(ctx, model=None):
        agent = build_agent(ctx, model=model)
        agent.model = FakeModel()
        agent.model.set_next_output([text_message(REPLY)])
        return agent

    monkeypatch.setattr(server_app, "build_agent", offline_agent)

    def run(user_id: int = 1, role: str = "shopper", **body) -> tuple[dict, dict]:
        server_app._SESSIONS.clear()
        exporter.clear()  # a test may call run() more than once
        created = server_app.create_session(
            server_app.SessionCreate(user_id=user_id, role=role)
        )
        response = asyncio.run(
            server_app.post_message(
                created["session_id"],
                server_app.MessageIn(message="Where is my order?", **body),
                authorization=f"Bearer {created['token']}",
            )
        )
        spans = [
            s
            for s in exporter.get_finished_spans()
            if s.name == "cartwheel.session_message"
        ]
        assert len(spans) == 1, "one request must produce exactly one root span"
        return response, dict(spans[0].attributes)

    return run


def test_root_span_records_the_authenticated_identity(endpoint) -> None:
    response, attrs = endpoint()
    assert attrs["cartwheel.user_role"] == "shopper"
    assert attrs["cartwheel.user_id"] == "1"  # a string, not an int
    assert attrs["cartwheel.prompt_version"] == prompt_version()
    assert attrs["cartwheel.session_id"] == response["session_id"]
    assert response == {
        "session_id": response["session_id"],
        "reply": REPLY,
        "prompt_version": attrs["cartwheel.prompt_version"],
    }


def test_the_prompt_version_is_the_same_for_every_caller(endpoint) -> None:
    """One prompt, one hash.

    The version identifies the *template*, not the rendered prompt, so two
    roles running the same prompt must agree. Hashing the rendered text
    instead would make the version a fingerprint of "prompt plus who asked",
    and Module 2 could not group traces by the prompt that produced them.
    """
    _, shopper = endpoint()
    _, merchant = endpoint(user_id=9002, role="merchant")
    assert merchant["cartwheel.user_role"] == "merchant"
    assert merchant["cartwheel.prompt_version"] == shopper["cartwheel.prompt_version"]
    assert merchant["cartwheel.prompt_version"] == prompt_version()


def test_each_session_is_recorded_on_its_own_root_span(endpoint) -> None:
    """The session id is what joins a trace back to a conversation."""
    first, first_attrs = endpoint()
    second, second_attrs = endpoint()
    assert first_attrs["cartwheel.session_id"] == first["session_id"]
    assert second_attrs["cartwheel.session_id"] == second["session_id"]
    assert first_attrs["cartwheel.session_id"] != second_attrs["cartwheel.session_id"]


def test_scenario_id_is_recorded_only_when_one_is_supplied(endpoint) -> None:
    _, without = endpoint()
    assert "cartwheel.scenario_id" not in without
    _, blank = endpoint(scenario_id="")
    assert "cartwheel.scenario_id" not in blank  # nonempty values only
    _, with_id = endpoint(scenario_id="refund-01")
    assert with_id["cartwheel.scenario_id"] == "refund-01"


def test_message_content_is_withheld_unless_capture_is_enabled(
    endpoint, monkeypatch
) -> None:
    monkeypatch.delenv("TRACELOOP_TRACE_CONTENT", raising=False)
    _, attrs = endpoint()
    assert "gen_ai.input.messages" not in attrs
    assert "gen_ai.output.messages" not in attrs


def test_message_content_uses_the_genai_format_when_enabled(
    endpoint, monkeypatch
) -> None:
    monkeypatch.setenv("TRACELOOP_TRACE_CONTENT", "true")
    _, attrs = endpoint()
    assert json.loads(attrs["gen_ai.input.messages"]) == [
        {"role": "user", "parts": [{"type": "text", "content": "Where is my order?"}]}
    ]
    assert json.loads(attrs["gen_ai.output.messages"]) == [
        {"role": "assistant", "parts": [{"type": "text", "content": REPLY}]}
    ]


# ---------------------------------------------------------------------------
# Part D: authentication. The server decides who you are, not the conversation.
# ---------------------------------------------------------------------------


@pytest.fixture
def server(world, tmp_path, monkeypatch):
    """The endpoint module with a temp conversation store and no sessions."""
    from server import app as server_app

    monkeypatch.setattr(server_app, "SESSIONS_DB", tmp_path / "sessions.db")
    server_app._SESSIONS.clear()
    return server_app


def _new_session(server, user_id: int, role: str) -> dict:
    return server.create_session(server.SessionCreate(user_id=user_id, role=role))


def test_session_creation_rejects_a_role_the_database_does_not_agree_with(
    server,
) -> None:
    """User 1 is a shopper. Asking to be a merchant is refused, not granted."""
    with pytest.raises(HTTPException) as refusal:
        _new_session(server, user_id=1, role="merchant")
    assert refusal.value.status_code == 403
    assert not server._SESSIONS  # nothing was created


def test_a_token_cannot_authorize_a_different_session(server) -> None:
    """Two sessions for the same user; the first token is still not a passkey."""
    first = _new_session(server, user_id=1, role="shopper")
    second = _new_session(server, user_id=1, role="shopper")
    assert first["session_id"] != second["session_id"]

    with pytest.raises(HTTPException) as refusal:
        asyncio.run(
            server.post_message(
                second["session_id"],
                server.MessageIn(message="Show my recent orders."),
                authorization=f"Bearer {first['token']}",
            )
        )
    assert refusal.value.status_code == 403


def test_a_tampered_token_is_rejected(server) -> None:
    """Change one character of the payload and the signature no longer matches."""
    created = _new_session(server, user_id=1, role="shopper")
    body, signature = created["token"].rsplit(".", 1)
    forged = f"{body[:-1]}{'A' if body[-1] != 'A' else 'B'}.{signature}"

    with pytest.raises(HTTPException) as refusal:
        asyncio.run(
            server.post_message(
                created["session_id"],
                server.MessageIn(message="Show my recent orders."),
                authorization=f"Bearer {forged}",
            )
        )
    assert refusal.value.status_code == 401


@pytest.mark.parametrize(
    "user_id, role, status",
    [
        (1, "wizard", 400),  # no such role: the request itself is malformed
        (999_999, "shopper", 404),  # no such user: nothing to verify against
    ],
)
def test_session_creation_refuses_unverifiable_requests(
    server, user_id, role, status
) -> None:
    with pytest.raises(HTTPException) as refusal:
        _new_session(server, user_id=user_id, role=role)
    assert refusal.value.status_code == status
