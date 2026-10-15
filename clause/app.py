"""FastAPI app: the API the UI talks to, the live event stream, and the static UI.

Run:  python -m uvicorn clause.app:app --port 8000
Route handlers in feature modules get the context with `request.app.state.ctx`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from clause.config import load_settings
from clause.context import Ctx, build_ctx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("clause.app")


def check_llm(ctx: Ctx) -> None:
    """A rejected Anthropic key must be loud, not a 401 in the middle of the demo."""
    client = getattr(ctx.llm, "client", None)
    if client is None:
        return
    import anthropic

    from clause.llm import FakeLLM

    try:
        client.models.retrieve(ctx.llm.model)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.BadRequestError) as exc:
        log.error("ANTHROPIC_API_KEY was rejected (%s). Put a working key in .env. Falling back to FakeLLM.", exc)
        ctx.llm = FakeLLM(name="fake (Anthropic key rejected)")
    except Exception as exc:
        log.warning("could not verify the Anthropic key/model %s: %s", ctx.llm.model, exc)


def create_app(ctx: Ctx, *, verify_keys: bool = True) -> FastAPI:
    settings = ctx.settings

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        ctx.loop = asyncio.get_running_loop()
        if verify_keys:
            await asyncio.to_thread(check_llm, ctx)
        for name, mod in ctx.modules.items():
            if hasattr(mod, "startup"):
                try:
                    await asyncio.to_thread(mod.startup, ctx)
                except Exception:
                    log.exception("startup of %s failed", name)
        log.info("Clause up: stripe=%s llm=%s memory=%s mail=%s", type(ctx.gateway).__name__, ctx.llm.name,
                 ctx.memory.backend, getattr(ctx.mailer, "mode", "?"))
        yield
        ctx.loop = None

    app = FastAPI(title="Clause", lifespan=lifespan)
    app.state.ctx = ctx

    for name in ("clause.billing", "clause.mail"):
        mod = ctx.modules.get(name)
        if mod is not None and hasattr(mod, "router"):
            app.include_router(mod.router)

    def desk():
        mod = ctx.modules.get("clause.desk")
        if mod is None:
            raise HTTPException(503, "desk not loaded")
        return mod

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        decisions = ctx.store.all("decision")
        return {
            "business": settings.business_name,
            "clock_date": ctx.clock_date().isoformat(),
            "scenario": ctx.store.meta("scenario") or "full",
            "modes": {
                "stripe": "test" if type(ctx.gateway).__name__ == "StripeGateway" else "fake",
                "llm": ctx.llm.name,
                "memory": ctx.memory.backend,
                "mail": getattr(ctx.mailer, "mode", "outbox"),
            },
            "customers": ctx.store.all("customer"),
            "invoices": ctx.store.all("invoice"),
            "decisions": decisions[-100:],
            "approvals": [d for d in decisions if d.get("status") == "pending_approval"],
            "emails": ctx.store.all("email")[-50:],
            "outbox": ctx.store.all("outbox")[-50:],
            "memory_events": ctx.memory_log[-100:],
        }

    @app.get("/api/events")
    async def events(request: Request) -> EventSourceResponse:
        queue = ctx.bus.subscribe()

        async def stream():
            try:
                while not await request.is_disconnected():
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        continue
                    yield {"event": item["event"], "data": json.dumps(item["data"], default=str)}
            finally:
                ctx.bus.unsubscribe(queue)

        return EventSourceResponse(stream(), ping=10)

    @app.post("/api/chat")
    def chat(body: dict = Body(...)) -> dict[str, Any]:
        message = (body.get("message") or "").strip()
        if not message:
            raise HTTPException(400, "message is required")
        return desk().chat(ctx, message, body.get("mode", "text"))

    @app.post("/api/approvals/{decision_id}")
    def approvals(decision_id: str, body: dict = Body(default={})) -> dict[str, Any]:
        try:
            return desk().decide_approval(ctx, decision_id, bool(body.get("approve")), body.get("note", ""))
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/why")
    def why(body: dict = Body(...)) -> dict[str, Any]:
        question = (body.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "question is required")
        return desk().answer(ctx, question, body.get("customer"))

    if settings.web_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
    else:
        @app.get("/", response_class=HTMLResponse)
        def placeholder() -> str:
            return "<h1>Clause</h1><p>The UI (web/) has not landed yet. API: <a href='/docs'>/docs</a></p>"

    return app


def __getattr__(name: str) -> Any:
    """`clause.app:app` builds the real app on first use, so importing this module (tests) has no side effects."""
    if name == "app":
        global app
        app = create_app(build_ctx(load_settings()))
        return app
    raise AttributeError(name)
