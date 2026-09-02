"""What every vault console page has in common: its config and its headers.

There are two consoles now (ADR 0039) and they differ in three things -- the
path they live at, the scopes they ask for, and the storage namespace they keep
a session in. Everything else about serving one is identical, and the identical
part includes the response headers.

**The headers are why this module exists rather than a second copy.** A console
that can be framed is a decision nobody made, and a Content-Security-Policy
that drifts between two pages drifts silently: the weaker one keeps working,
which is exactly what makes it hard to notice. Session handling was extracted
for the same reason and one file over; this is the smaller half of the same
argument.

What is *not* here is the page. Each console owns its own template, its own
scopes, and the docstring explaining why those scopes are what they are -- the
reviewer's `vault:read` alone is a separation-of-duties rule, and the browser's
`vault:read vault:propose` is a baseline authorization that needs no operator
grant. Those are decisions, not configuration, and they belong beside the
console they describe.
"""

import secrets

from starlette.responses import HTMLResponse

from .templating import render


# The API every console talks to. Same origin, so no page needs an issuer URL:
# the authorization server, the resource server, and the pages are one
# deployment.
API_BASE = "/api/v1/vault"

# Applied to every console page. A console renders unverified text -- note
# bodies written by agents, an operator's label -- through `textContent`, and
# this is the second layer under that: no third-party script, no framing, no
# form posting anywhere, and nothing cached.
#
# The CSP is not here because it carries a per-response nonce; see `_csp`.
CONSOLE_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# 16 bytes, which is comfortably above the 128 bits CSP asks for.
_NONCE_BYTES = 16


def _csp(nonce: str) -> str:
    """The console policy, bound to one response's script nonce.

    `script-src` names a nonce rather than 'unsafe-inline'. The pages inline
    their script, so *something* has to permit it, and 'unsafe-inline' permits
    every inline script including one an injection manages to introduce --
    which is the case the directive exists for. A nonce permits exactly the
    blocks this render emitted.

    Defence in depth, not a fix for a known hole: the consoles build their DOM
    with `textContent` and never interpolate corpus text into markup. This is
    the layer under that, for the same reason the header set exists at all.

    A per-response nonce is only safe because these responses are `no-store`.
    A cached page would serve a nonce its header no longer names, and the page
    would silently stop working.

    `style-src` deliberately keeps 'unsafe-inline'. A nonce there would not
    help: CSP ignores 'unsafe-inline' entirely once a nonce is present in a
    directive, and that would break the `style="..."` attributes in the
    markup, which nonces cannot cover.
    """

    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'none'"
    )


def vault_page(template: str, **context: object) -> HTMLResponse:
    """Render any first-party vault page with the shared browser policy.

    Every template gets `csp_nonce`, whether or not it has a script to put it
    on -- a page that grows one should not also have to discover that it needs
    plumbing to make it run.
    """

    nonce = secrets.token_urlsafe(_NONCE_BYTES)
    return HTMLResponse(
        render(template, csp_nonce=nonce, **context),
        headers={**CONSOLE_HEADERS, "Content-Security-Policy": _csp(nonce)},
    )


def console_page(
    template: str,
    *,
    console_path: str,
    scopes: str,
    client_name: str,
    store_prefix: str,
) -> HTMLResponse:
    """Render one console shell, with the config its session module reads.

    The five values are the whole interface between a page and
    ``_console_session.js``. Passing them here rather than letting each
    console assemble its own context is what keeps a new console from
    forgetting one -- a missing `storePrefix` renders as `undefined` in a
    storage key, which fails at sign-in rather than at render.
    """

    return vault_page(
        template,
        api_base=API_BASE,
        scopes=scopes,
        console_path=console_path,
        client_name=client_name,
        store_prefix=store_prefix,
    )
