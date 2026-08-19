"""The HTTP layer, and nothing else.

Split from ``web.py`` for two reasons, one architectural and one mechanical.

The architectural one: ``web.process()`` takes filenames and bytes and returns
a dataclass, so the reasoning about what a statement is and whether it can be
trusted stays testable without a client and portable off FastAPI. Everything
framework-shaped lives here, where it can be replaced without touching it.

The mechanical one, worth writing down because it will otherwise be
rediscovered: ``web.py`` uses ``from __future__ import annotations``, which
turns every annotation into a string. FastAPI resolves a handler's annotations
at import time against the *module's* globals, so a route annotated
``list[UploadFile]`` in a module using postponed evaluation fails unless
``UploadFile`` is a module-level name. This file therefore does not use the
future import and imports its FastAPI names at the top.
"""

import argparse
import os

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .licence import Licences, buy_url, from_environment, price
from .web import (
    MAX_BODY_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    SAMPLE_NAME,
    SAMPLE_STATEMENT,
    SOURCE_URL,
    STATIC,
    RateLimit,
    client_key,
    process,
)

#: Twelve statements a minute is far past what anyone reconciling by hand does,
#: and far below what it takes to monopolise a small instance.
UPLOADS_PER_MINUTE = 12


def create_app(
    limit: RateLimit | None = None, licences: Licences | None = None
) -> FastAPI:
    budget = limit or RateLimit(UPLOADS_PER_MINUTE, 60.0)
    gate = licences or from_environment(paid_files=MAX_FILES)
    app = FastAPI(
        title="moneytrail",
        description="Bank statements to a ledger that proves it is all of it.",
        # No interactive docs: this is a product surface, not an API console,
        # and an upload endpoint with a try-it button invites statements from
        # people who have not read what happens to them.
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def cap_body(request: Request, call_next):
        """Refuse an oversized body before anything downstream reads it.

        Starlette imposes no limit of its own, so without this a large POST is
        buffered in full on a 512MB instance before any of this code decides it
        was never wanted.
        """
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return JSONResponse({"error": "that request is too large"}, status_code=413)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/api/pricing")
    def pricing():
        """What the page needs to describe the tiers, without hardcoding them.

        The front-end asks rather than assuming, so a self-hosted instance with
        no product configured renders no paywall at all instead of advertising
        a purchase the operator is not selling.
        """
        from .licence import FREE_FILES

        amount, period = price()
        return {
            "selling": gate.selling,
            "free_files": FREE_FILES,
            "paid_files": gate.paid_files,
            "buy_url": buy_url(),
            "price": amount,
            "period": period,
            "source_url": SOURCE_URL,
        }

    @app.get("/api/sample")
    def sample():
        """A statement with a row deliberately removed, for the demo.

        Nobody uploads their bank statement to a site they have not decided to
        trust yet, so the page has to be able to prove the point with its own
        file. This is that file.
        """
        return {"filename": SAMPLE_NAME, "content": SAMPLE_STATEMENT}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.post("/api/export")
    async def export(
        request: Request,
        files: list[UploadFile] = File(...),
        password: str = Form(""),
        fmt: str = Form("xlsx"),
        licence: str = Form(""),
    ):
        who = client_key(
            request.headers.get("x-forwarded-for"),
            request.client.host if request.client else None,
        )
        if not budget.check(who):
            return JSONResponse(
                {"error": "too many uploads in a row -- wait a minute and retry"},
                status_code=429,
            )

        # Content-Length is checked here as well as by the middleware because a
        # chunked upload arrives without one; the per-file cap in `process` is
        # what actually holds in that case.
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return JSONResponse(
                {"error": "that request is too large"}, status_code=413
            )

        entitlement = gate.entitlement(licence)
        if len(files) > entitlement.files:
            # 402 rather than 413: the request is not too big, it is unpaid.
            # The page needs to tell those apart to show the right thing.
            return JSONResponse(
                {
                    "error": (
                        f"that is {len(files)} statements, and this key covers "
                        f"{entitlement.files} at a time"
                        if entitlement.licensed
                        else f"one statement at a time without a key -- "
                        f"a key raises that to {gate.paid_files}"
                    ),
                    "needs_licence": not entitlement.licensed,
                    "licence_problem": entitlement.problem,
                    "buy_url": buy_url(),
                },
                status_code=402 if not entitlement.licensed else 413,
            )

        uploads = []
        for upload in files:
            # Read one byte past the cap: that is enough to reject an oversized
            # file and stops a large upload being held in memory in full.
            blob = await upload.read(MAX_FILE_BYTES + 1)
            uploads.append((upload.filename or "statement", blob))

        try:
            result = process(uploads, password=password or None, fmt=fmt)
        except ValueError as error:
            return JSONResponse({"error": str(error)}, status_code=400)

        # 422 when nothing could be read at all: the request was well-formed
        # and the files were not, and the page needs to tell those apart.
        payload = result.as_json()
        payload["licence"] = {
            "licensed": entitlement.licensed,
            # A key that was offered and refused must say so even on a request
            # that otherwise succeeded -- a lapsed subscription silently
            # falling back to the free tier is how someone keeps paying for
            # nothing, or stops paying without noticing.
            "problem": entitlement.problem,
            "files": entitlement.files,
        }
        return JSONResponse(payload, status_code=200 if result.total else 422)

    return app


def main(argv: "list[str] | None" = None) -> int:
    """``python -m moneytrail.api`` -- run it locally, or on a host.

    The defaults follow the environment rather than the developer. A platform
    assigns the port through ``$PORT`` and expects the process to bind every
    interface; binding 127.0.0.1 there produces a container that starts, passes
    no health check, and gives no reason why. Locally, with neither variable
    set, it still comes up on 127.0.0.1:8000 and reaches nothing else.
    """
    import uvicorn

    from .llm import load_dotenv

    # Local runs pick the price and product id out of `.env`; a host sets them
    # as real environment variables and has no `.env` for this to find. Done
    # here rather than in `create_app` so the tests never inherit whatever
    # happens to be sitting in the developer's file.
    load_dotenv()

    hosted = "PORT" in os.environ
    parser = argparse.ArgumentParser(prog="moneytrail.api")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0" if hosted else "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = parser.parse_args(argv)

    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
