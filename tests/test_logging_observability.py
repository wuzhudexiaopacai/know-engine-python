"""统一日志服务单元测试：脱敏、请求上下文、审计入队、ES 降级查询。"""

import asyncio
import json

import httpx
from fastapi import FastAPI

from app.core import request_context
from app.core.logging import redact_sensitive, _service_context
from app.core.request_context import RequestContextMiddleware, bind_identity


def _event_dict(**kwargs):
    return dict(kwargs)


class TestRedaction:
    def test_sensitive_keys_are_masked(self):
        out = redact_sensitive(None, "info", _event_dict(
            password="p@ss", api_key="sk-123", token="tok", authorization="Bearer abc",
            nested={"secret_key": "x", "keep": "ok"}, items=[{"cookie": "c"}],
            message="fine",
        ))
        assert out["password"] == "***"
        assert out["api_key"] == "***"
        assert out["token"] == "***"
        assert out["authorization"] == "***"
        assert out["nested"] == {"secret_key": "***", "keep": "ok"}
        assert out["items"] == [{"cookie": "***"}]
        assert out["message"] == "fine"

    def test_bearer_token_in_message_is_masked(self):
        out = redact_sensitive(None, "info", _event_dict(
            event="call failed with Authorization: Bearer sk-abcdef123456",
        ))
        assert "sk-abcdef123456" not in out["event"]
        assert "Bearer ***" in out["event"]

    def test_service_context_fields(self):
        out = _service_context(None, "info", _event_dict(event="x"))
        assert out["service"]
        assert out["env"]
        assert out["host"]
        assert isinstance(out["pid"], int)
        assert ":" in out["instance"]


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestRequestContext:
    def test_request_id_propagates(self):
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)

        @app.get("/api/ping")
        async def ping():
            return {"rid": request_context.current_request_id()}

        async def run():
            async with _client(app) as client:
                resp = await client.get("/api/ping", headers={"X-Request-ID": "abc-123"})
                return resp

        resp = asyncio.run(run())
        assert resp.status_code == 200
        assert resp.headers["X-Request-ID"] == "abc-123"
        assert resp.json()["rid"] == "abc-123"

    def test_request_id_generated_when_absent(self):
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)

        @app.get("/api/ping")
        async def ping():
            return {"ok": True}

        async def run():
            async with _client(app) as client:
                return await client.get("/api/ping")

        resp = asyncio.run(run())
        assert len(resp.headers["X-Request-ID"]) == 20

    def test_identity_binding_reaches_request_context(self):
        app = FastAPI()
        app.add_middleware(RequestContextMiddleware)

        @app.get("/api/who")
        async def who():
            bind_identity(7, "1001", "user", "张三")
            return request_context.current_identity()

        async def run():
            async with _client(app) as client:
                return await client.get("/api/who")

        data = asyncio.run(run()).json()
        assert data["tenant_id"] == 7
        assert data["account_id"] == "1001"
        assert data["account_name"] == "张三"

    def test_audit_scope_rules(self):
        assert request_context._should_audit("POST", "/api/admin/users")
        assert request_context._should_audit("DELETE", "/api/document/1")
        assert not request_context._should_audit("GET", "/api/admin/users")
        assert not request_context._should_audit("POST", "/api/chat/messages")
        assert not request_context._should_audit("POST", "/api/documents/upload/chunk")

    def test_action_and_module_derivation(self):
        assert request_context._module_of("/api/admin/users") == "admin"
        assert request_context._action_of("POST", "/api/admin/users/12/roles") == "post:roles"
        assert request_context._action_of("DELETE", "/api/document/9") == "delete:document"


class TestAudit:
    def test_record_writes_when_worker_not_started(self, monkeypatch):
        from app.modules.audit import service as audit

        captured = []

        async def fake_write(entries):
            captured.extend(entries)

        async def run():
            monkeypatch.setattr(audit, "_write_batch", fake_write)
            monkeypatch.setattr(audit, "_queue", None)
            await audit.record(module="auth", action="login", status="SUCCESS",
                               account_id=1, account_name="张三", tenant_id=3,
                               detail={"login_type": "PHONE"})
            return request_context._explicit_audited_var.get()

        explicit = asyncio.run(run())
        assert explicit is True
        assert captured[0]["module"] == "auth"
        assert captured[0]["action"] == "login"
        assert captured[0]["tenant_id"] == 3
        assert json.dumps(captured[0]["detail"], ensure_ascii=False) == '{"login_type": "PHONE"}'


class TestEsLogQuery:
    def test_degraded_when_es_disabled(self, monkeypatch):
        from app.infra import log_store

        monkeypatch.setattr(log_store, "es_logging_enabled", lambda: False)

        async def run():
            return await log_store.search_system_logs(page=1, size=10)

        data = asyncio.run(run())
        assert data["degraded"] is True
        assert data["backend"] == "file"
        assert data["records"] == []

    def test_query_contains_filters(self):
        from app.infra.log_store import _build_query

        body = _build_query("登录失败", "ERROR", "rid-1", 5, "admin",
                            "2026-01-01 00:00:00", "2026-01-02 00:00:00")
        query = body["query"]["bool"]
        assert query["must"][0]["multi_match"]["query"] == "登录失败"
        filters = query["filter"]
        assert {"term": {"level": "ERROR"}} in filters
        assert {"term": {"request_id": "rid-1"}} in filters
        assert {"term": {"tenant_id": 5}} in filters
        assert any("range" in f for f in filters)
