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

from .web import (
    MAX_BODY_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    STATIC,
    RateLimit,
    client_key,
    process,
)

#: Twelve statements a minute is far past what anyone reconciling by hand does,
#: and far below what it takes to monopolise a small instance.
UPLOADS_PER_MINUTE = 12


def create_app(limit: RateLimit | None = None) -> FastAPI:
    budget = limit or RateLimit(UPLOADS_PER_MINUTE, 60.0)
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

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.post("/api/export")
    async def export(
        request: Request,
        files: list[UploadFile] = File(...),
        password: str = Form(""),
        fmt: str = Form("xlsx"),
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

        if len(files) > MAX_FILES:
            return JSONResponse(
                {"error": f"too many files at once -- the limit is {MAX_FILES}"},
                status_code=413,
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
        return JSONResponse(result.as_json(), status_code=200 if result.total else 422)

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

    hosted = "PORT" in os.environ
    parser = argparse.ArgumentParser(prog="moneytrail.api")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0" if hosted else "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = parser.parse_args(argv)

    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
