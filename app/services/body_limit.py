"""Rejects oversized request bodies early (413) so big uploads can't land in temp files or fill memory.

Starlette keeps a multipart upload in memory only up to 1 MiB, then spills it to a temp file on disk.
The employee import limit is kept below that, so a file containing full Omang numbers never touches disk.
The payslip upload routes (/batches/new, /batches/<id>/upload) get the whole-batch limit (UPLOAD_PATHS).
"""
import re

from starlette.responses import PlainTextResponse

from app.config import get_settings

DEFAULT_LIMIT = 100_000
LIMITS = {
    "/employees/import": 1_000_000,
    "/webhook/whatsapp": 2_000_000,      # Meta batches up to 1000 status updates in one POST
}
# Payslip uploads (many PDFs or one ZIP): allowed up to the whole-batch limit plus form overhead.
# Starlette spools big uploads to a temp file while parsing; PDFs may do that (they go to private storage anyway).
UPLOAD_PATHS = (re.compile(r"^/batches/new$"), re.compile(r"^/batches/\d+/upload$"))
UPLOAD_OVERHEAD = 20 * 1024 * 1024


def limit_for(path: str, limits: dict[str, int], default: int) -> int:
    if path in limits:
        return limits[path]
    if any(p.match(path) for p in UPLOAD_PATHS):
        return get_settings().max_batch_total_mb * 1024 * 1024 + UPLOAD_OVERHEAD
    return default


class _TooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(self, app, limits: dict[str, int] | None = None, default: int = DEFAULT_LIMIT):
        self.app = app
        self.limits = LIMITS if limits is None else limits
        self.default = default

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)

        limit = limit_for(scope["path"], self.limits, self.default)
        length = dict(scope["headers"]).get(b"content-length")
        if length and length.isdigit() and int(length) > limit:
            return await self._reject(scope, receive, send)

        received = 0
        started = False

        async def counting_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except _TooLarge:
            if not started:
                await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope, receive, send):
        await PlainTextResponse("That upload is too large.", status_code=413)(scope, receive, send)
