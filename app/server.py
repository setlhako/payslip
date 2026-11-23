"""The production entry point: python -m app.server   (systemd runs exactly this).

It starts ONE uvicorn process (workers=1, never --reload) listening only on 127.0.0.1, trusting X-Forwarded-* headers ONLY from
127.0.0.1 (that is Caddy, on the same machine), with the access log off (it would record query strings) and our own log setup.
It refuses to start if WEB_CONCURRENCY asks for more than one worker, or if the settings are unsafe (see config.py).
"""
import os
import sys

import uvicorn


def refuse_multiple_workers(environ=os.environ) -> None:
    raw = (environ.get("WEB_CONCURRENCY") or "1").strip() or "1"
    if not raw.isdigit() or int(raw) != 1:
        raise SystemExit("Refusing to start: WEB_CONCURRENCY must be 1 (exactly one app process; see the README 'Why exactly one process').")


def run_options(settings) -> dict:
    """The uvicorn options. Pure, so tests can check them."""
    return {
        "host": settings.app_host, "port": settings.app_port,
        "workers": 1,                                     # the send engine is in-process: never more than one
        "reload": False,
        "proxy_headers": True, "forwarded_allow_ips": "127.0.0.1",
        "access_log": False,                              # its lines contain full URLs with query strings
        "server_header": False,
        "log_config": None,                               # our logging_setup decides (structured, redacted)
    }


def main() -> None:
    refuse_multiple_workers()
    from pydantic import ValidationError

    from app.config import get_settings

    try:
        settings = get_settings()                          # raises (so the app refuses to start) when settings are unsafe
    except ValidationError as exc:
        # Plain words instead of a traceback. The messages name settings only, never their values.
        reasons = "; ".join(" ".join(str(e["msg"]).split()).removeprefix("Value error, ") for e in exc.errors())
        raise SystemExit(f"Refusing to start: {reasons}") from None
    uvicorn.run("app.main:app", **run_options(settings))


if __name__ == "__main__":
    sys.exit(main())
