"""Revalidate protected HTML after a browser restores its back-forward cache."""

import secrets
from collections.abc import Callable

from starlette.responses import HTMLResponse


def protected_page(
    body: str,
    headers: Callable[[str], dict[str, str]],
    *,
    status: int = 200,
    nonce: str | None = None,
) -> HTMLResponse:
    nonce = nonce or secrets.token_urlsafe(18)
    # Chrome can restore no-store documents from BFCache without another request.
    script = f"""<script nonce="{nonce}">
addEventListener("pagehide", event => {{
  if (event.persisted) document.querySelector("main")?.replaceChildren();
}});
addEventListener("pageshow", event => {{
  if (event.persisted) location.reload();
}});
</script>"""
    return HTMLResponse(
        body.replace("</body>", script + "</body>", 1),
        status_code=status,
        headers=headers(nonce),
    )
