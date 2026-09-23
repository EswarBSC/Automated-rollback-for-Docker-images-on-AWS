"""
FastAPI demo application for the ECS rollback proof-of-concept.

The whole point of this app is to be *visibly* different between versions, so
that during a demo everyone in the room can see which version is live just by
looking at the page. Change the two constants below, commit, and the CI/CD
pipeline ships a new image; run the Rollback workflow and the old one comes
straight back.
"""

import os
import socket

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

# ---------------------------------------------------------------------------
# EDIT THESE FOR DEMOS
# ---------------------------------------------------------------------------
# APP_COLOR is the background colour of the big banner box on the home page.
# BANNER_MESSAGE is the text inside that box.
# Change them, commit, push -> a new image is built and deployed. The previous
# image stays in ECR untouched, which is what makes rollback instant.
APP_COLOR = "#dc2626"                    # red
BANNER_MESSAGE = "v2 — BROKEN RELEASE"


# ---------------------------------------------------------------------------

app = FastAPI(title="ECS Rollback Demo")


# Environment variables are read *inside* the request handlers, never at import
# time. That matters for two reasons:
#   1. Tests can flip a variable with monkeypatch and see the effect immediately.
#   2. ECS injects these values from the task definition, so a task definition
#      change alone (no rebuild) can alter behaviour.
def _git_sha() -> str:
    """The 7-char git SHA baked into the image at build time. 'local' outside CI."""
    return os.getenv("GIT_SHA", "local")


def _app_env() -> str:
    """Which environment this container thinks it is running in."""
    return os.getenv("APP_ENV", "local")


def _flag(name: str) -> bool:
    """Read a demo switch. Only the exact string 'true' turns it on."""
    return os.getenv(name, "false").lower() == "true"


def _page(color: str, message: str, version: str, env: str, host: str) -> str:
    """Render the home page. Plain inline CSS so the image needs no extra files."""
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ECS Rollback Demo</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #f8fafc;
      color: #0f172a;
    }}
    .banner {{
      background: {color};
      color: #ffffff;
      width: 100%;
      padding: 96px 24px;
      text-align: center;
      font-size: clamp(28px, 6vw, 56px);
      font-weight: 700;
      letter-spacing: -0.02em;
      line-height: 1.2;
    }}
    .facts {{
      max-width: 720px;
      margin: 40px auto;
      padding: 0 24px;
    }}
    table {{ width: 100%; border-collapse: collapse; font-size: 16px; }}
    th, td {{ text-align: left; padding: 14px 12px; border-bottom: 1px solid #e2e8f0; }}
    th {{ width: 40%; color: #475569; font-weight: 600; }}
    td {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .hint {{ margin-top: 28px; color: #64748b; font-size: 14px; }}
  </style>
</head>
<body>
  <div class="banner">{message}</div>
  <div class="facts">
    <table>
      <tr><th>Version (git SHA)</th><td>{version}</td></tr>
      <tr><th>Environment</th><td>{env}</td></tr>
      <tr><th>Container hostname</th><td>{host}</td></tr>
    </table>
    <p class="hint">
      This page is served by one image tagged with the git SHA above.
      A rollback swaps the running task definition revision — it never rebuilds.
    </p>
  </div>
</body>
</html>"""


def _error_page(version: str) -> str:
    """Shown when SIMULATE_ERRORS=true, to demo an alarm-triggered rollback."""
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>500 — Application Error</title>
<style>
  body {{ margin:0; font-family: -apple-system, "Segoe UI", Roboto, sans-serif; background:#450a0a; color:#fff; }}
  .box {{ width:100%; padding:96px 24px; text-align:center; }}
  h1 {{ font-size: clamp(28px, 6vw, 56px); margin:0 0 16px; }}
  p {{ font-family: ui-monospace, Menlo, monospace; opacity:.85; }}
</style></head>
<body>
  <div class="box">
    <h1>500 — Application Error</h1>
    <p>SIMULATE_ERRORS=true · version {version}</p>
  </div>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    """
    The page a human looks at.

    If SIMULATE_ERRORS is 'true' every request returns HTTP 500. That is how the
    demo drives a CloudWatch alarm on 5xx count without touching the health
    check, so ECS keeps the bad version running and a *human* must roll back.
    """
    version = _git_sha()
    if _flag("SIMULATE_ERRORS"):
        return HTMLResponse(content=_error_page(version), status_code=500)

    return HTMLResponse(
        content=_page(
            color=APP_COLOR,
            message=BANNER_MESSAGE,
            version=version,
            env=_app_env(),
            host=socket.gethostname(),
        ),
        status_code=200,
    )


@app.get("/health")
def health() -> JSONResponse:
    """
    Health endpoint used by three different things:
      * the container health check in the task definition,
      * the ALB target group (if you attach one),
      * the CI smoke test before the image is ever pushed to ECR.

    Setting FAIL_HEALTH=true makes it return 503. ECS then fails the deployment
    and its circuit breaker rolls back automatically — no human involved.
    """
    if _flag("FAIL_HEALTH"):
        return JSONResponse(content={"status": "unhealthy"}, status_code=503)
    return JSONResponse(content={"status": "ok", "version": _git_sha()}, status_code=200)


@app.get("/version")
def version() -> JSONResponse:
    """Machine-readable version info — handy for scripts and for proving a rollback."""
    return JSONResponse(
        content={
            "version": _git_sha(),
            "env": _app_env(),
            "color": APP_COLOR,
            "message": BANNER_MESSAGE,
        },
        status_code=200,
    )
