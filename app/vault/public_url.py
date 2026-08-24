"""Whether ``VAULT_PUBLIC_URL`` still matches the host requests arrive on.

``VAULT_PUBLIC_URL`` is configuration rather than something derived from the
request, for three reasons that are worth restating here because this module
exists precisely at the seam:

1. **The routes are built before any request exists.** ``create_auth_routes``
   bakes ``issuer_url`` into the RFC 8414 and RFC 9728 documents at application
   assembly, so there is no request to read a host from at that moment.
2. **Deriving it would mean trusting the ``Host`` header**, on the one endpoint
   where that is least acceptable. Discovery metadata tells a client where to
   send credentials; an attacker who could set ``Host`` could point
   ``authorization_endpoint`` and ``token_endpoint`` at a host they control.
3. **Its absence is the feature's off switch** (vault ADR 0024).

What the configuration cannot do is notice when it goes stale. A value that no
longer matches the deployment publishes discovery documents pointing somewhere
wrong, and the symptom surfaces at the *client* as "this server does not
support OAuth" -- the same confusing shape the `.../mcp` versus `.../mcp/`
metadata bug had, and just as far from its cause.

So this observes the host and **reports**. It never returns a value anything
builds a URL from: the configured value stays authoritative precisely because
the observed one is untrusted. Reading ``Host`` to write a log line is safe in a
way that reading it to publish an issuer is not.

Reported once per process, at the first OAuth request. A per-request log would
be noise, and the fact is a deployment property that does not change between
requests.
"""

import logging
import os

from starlette.requests import Request


logger = logging.getLogger(__name__)

# Whether this process has already said its piece. A plain bool rather than a
# lock: the loop is single-threaded, and the worst case under any race is the
# same line logged twice.
_reported = False


def configured_public_url() -> str:
    """``VAULT_PUBLIC_URL`` with any trailing slash removed, or ``""``."""

    return (os.environ.get("VAULT_PUBLIC_URL") or "").rstrip("/")


def observed_origin(request: Request) -> str | None:
    """The origin this request appears to have arrived on, or None.

    ``X-Forwarded-Proto`` because the application sits behind Heroku's router
    and would otherwise report ``http`` for every request. Both this and
    ``Host`` are client-supplied and therefore forgeable -- which is exactly why
    the only thing done with the result is compare it and log.
    """

    forwarded = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded.split(",")[0].strip() or request.url.scheme
    host = request.headers.get("host", "").strip()
    return f"{scheme.lower()}://{host.lower()}" if host else None


def report_public_url_drift(request: Request) -> None:
    """Log the configured origin against the observed one, once."""

    global _reported
    if _reported:
        return

    configured = configured_public_url()
    if not configured:
        # These routes do not exist without it, so this is unreachable from the
        # OAuth surface. Guarded anyway: the alternative is a warning claiming a
        # mismatch against the empty string.
        return

    observed = observed_origin(request)
    _reported = True

    if observed is None:
        logger.info(
            "vault oauth public url configured; request carried no Host header",
            extra={"vault_public_url": configured},
        )
        return

    if observed == configured.lower():
        logger.info(
            "vault oauth public url matches the host requests arrive on",
            extra={"vault_public_url": configured, "vault_observed_origin": observed},
        )
        return

    logger.warning(
        "vault oauth public url does not match the host requests arrive on; "
        "discovery metadata advertises %s while requests arrive on %s. The "
        "configured value is still what is published -- clients will be sent "
        "to it -- so a custom domain, a proxy, or a renamed app needs "
        "VAULT_PUBLIC_URL updated to match.",
        configured,
        observed,
        extra={"vault_public_url": configured, "vault_observed_origin": observed},
    )


def reset_public_url_report() -> None:
    """Forget that this process reported. For tests."""

    global _reported
    _reported = False
