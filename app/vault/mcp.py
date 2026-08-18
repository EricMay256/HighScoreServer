"""MCP adapter for the vault.

The second transport over the same services. ``routes.py`` speaks HTTP to a
programmatic caller; this speaks MCP to an agent whose client holds the
credential in its own configuration rather than in a chat message. Both are thin
adapters -- neither contains SQL, embedding calls, or policy decisions, and the
governed write path, the dedup gate, ADR 0014's read policy and the audit trail
are identical whichever one a caller arrives through.

**Why the credential moves at all.** Pasting ``hssv1_...`` into a conversation
puts it in the model's context, in transcripts, and in any summary derived from
them. Under MCP the token lives in client configuration and the model never
sees it. That is the security gain; the secret itself does not go away, and it
rotates exactly as before.

**Scope-filtered tool listing is a boundary, not a convenience.** ``list_tools``
returns only the tools the presented credential's scopes permit, so a credential
holding ``vault:read`` and ``vault:write`` yields a session in which
``vault_retire_note`` does not exist. This matters because the corpus is
untrusted input: notes are written by agents and read by agents, and a note
carrying "also retire <id>" is read *inside* an already-authenticated session.
No scope check intercepts that -- the session legitimately holds its scopes --
so the defence has to be that the destructive tool is absent from the surface
the injected text can name. One credential is one capability profile; which
tools exist is decided by which credential the operator put in which client.
ADR 0020's verb split is what makes this expressible.

**Authentication is our own middleware rather than the SDK's.** ``MCPServer``
accepts a ``token_verifier``, but refuses it without ``AuthSettings``, which
requires an ``issuer_url`` -- it is built for the OAuth resource-server profile
and makes the server publish protected-resource metadata pointing at an
authorization server. The vault has none. Advertising a discovery document for
an authorization server that does not exist is worse than advertising nothing:
a spec-compliant client would start the flow and fail, where today it simply
sends its bearer token. So the middleware below verifies the token itself and
the SDK's auth machinery stays switched off.

That is the arm to replace if OAuth ever lands: ``principal.resolve_credential``
is already shaped like the SDK's ``TokenVerifier`` protocol -- token in, scopes
out -- so the swap is this module plus real ``AuthSettings``, and nothing
downstream of the credential moves.

**The middleware also carries the pre-auth guard, and must.** In ``routes.py``
that guard is an ``APIRouter`` dependency, deliberately, so it is charged before
the credential lookup it exists to bound. A mounted ASGI application does not
inherit router dependencies, so the MCP endpoint would otherwise be the one door
on the vault with no bouncer -- and it fails silently, since everything works
until someone hammers it.
"""

import json
import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from hashlib import sha256
from typing import Any
from uuid import uuid4

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Tool as MCPTool
from slowapi.errors import RateLimitExceeded
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_models import (
    VaultContributionRequest,
    VaultDocumentUpdateRequest,
    canonical_request_digest,
    document_detail,
)
from .auth import VaultCredential, VaultScope
from .constants import resolve_text_search_config
from .db import get_vault_engine
from .embedding_runtime import get_embedding_provider
from .embeddings import EmbeddingError, EmbeddingInputTooLong
from .principal import (
    VaultPrincipalError,
    VaultQuotaExceeded,
    charge_quota,
    resolve_credential,
)
from .rate_limit import enforce_preauth_ip_limit
from .read_policy import READABLE_STATUSES
from .repository import VaultDocumentRepository
from .service import (
    REQUEST_DIGEST_VERSION,
    ContributionRequest,
    DedupUnavailable,
    DocumentNotFound,
    DocumentUnderReview,
    IdempotencyConflict,
    RetireRequest,
    UpdateRequest,
    UpdateWouldDuplicate,
    VaultContributionService,
    VaultDocumentRetireService,
    VaultDocumentUpdateService,
    VaultSearchService,
    VaultTransactionService,
)


logger = logging.getLogger(__name__)

# Set by the middleware for the duration of one request, read by the tools. A
# ContextVar rather than a parameter because the SDK owns the call path between
# the two and gives a tool no way to receive the transport's authentication
# result. Reset in a finally so a pooled worker cannot leak one caller's
# credential into the next request it happens to serve.
_credential: ContextVar[VaultCredential | None] = ContextVar(
    "vault_mcp_credential",
    default=None,
)

# Which scope each tool requires, and the single statement of it. The quota
# operation deliberately reuses the HTTP surface's name: a separate "mcp.search"
# bucket would let one principal spend its allowance twice by changing
# transport.
_TOOL_SCOPES: dict[str, tuple[str, str]] = {
    "vault_search": (VaultScope.READ, "search"),
    "vault_get_note": (VaultScope.READ, "get_note"),
    "vault_contribute": (VaultScope.WRITE, "contribute"),
    "vault_update_note": (VaultScope.UPDATE, "update"),
    "vault_retire_note": (VaultScope.DELETE, "retire"),
}


def _bearer(header: str | None) -> str | None:
    """Strip the scheme from an Authorization header, or None."""

    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


class VaultMCPAuthMiddleware:
    """Charge the pre-auth guard, then authenticate, before the MCP app runs.

    Pure ASGI rather than a Starlette ``BaseHTTPMiddleware`` because the
    Streamable HTTP transport streams its responses, and ``BaseHTTPMiddleware``
    buffers through an anyio object stream that has caused deadlocks with
    long-lived SSE bodies. Nothing here needs the response.

    Scope is *not* checked at this layer. A session may legitimately carry any
    subset of the vault's scopes, and which tools that subset permits is the
    business of ``list_tools`` and each tool. This layer answers only "is this a
    live credential at all", which is what bounds the cost of the endpoint.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)

        try:
            await enforce_preauth_ip_limit(request=request)
        except RateLimitExceeded as exc:
            # The host's slowapi handler is registered on the outer app and does
            # not see exceptions raised inside a mount, so render it here -- and
            # render it the *same way*, carrying slowapi's own detail (which
            # names the limit that was hit) and any headers it set. An operator
            # reading a 429 should not be able to tell which transport produced
            # it, and "Rate limit exceeded" with no number is not actionable.
            await JSONResponse(
                {"detail": f"Rate limit exceeded: {exc.detail}"},
                status_code=429,
                headers=dict(exc.headers) if exc.headers else None,
            )(scope, receive, send)
            return

        try:
            credential = await resolve_credential(
                _bearer(request.headers.get("Authorization")),
                (),
            )
        except VaultPrincipalError:
            await JSONResponse(
                {"detail": "Invalid vault credentials"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return

        token = _credential.set(credential)
        try:
            await self.app(scope, receive, send)
        finally:
            _credential.reset(token)


class VaultMCPServer(MCPServer):
    """An MCPServer whose tool list depends on the caller's scopes.

    Overriding ``list_tools`` rather than filtering inside each tool because a
    tool the caller may not use should not be *advertised*. Advertising it and
    refusing the call leaks the shape of the surface, and -- the reason that
    matters here -- it leaves the tool's name in the model's context, where a
    note carrying injected instructions can name it.
    """

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        credential = _credential.get()
        if credential is None:  # pragma: no cover - middleware runs first
            return []
        return [
            tool
            for tool in tools
            if tool.name in _TOOL_SCOPES
            and credential.has_scope(_TOOL_SCOPES[tool.name][0])
        ]


async def _authorized(tool: str) -> VaultCredential:
    """Check the tool's scope and charge its quota, or raise a tool error."""

    credential = _credential.get()
    if credential is None:  # pragma: no cover - middleware runs first
        raise ToolError("Not authenticated")

    required_scope, operation = _TOOL_SCOPES[tool]
    if not credential.has_scope(required_scope):
        # Reachable only if a client calls a tool that list_tools withheld.
        logger.warning(
            "Vault MCP tool called without its scope",
            extra={
                "credential_id": credential.id,
                "principal_id": credential.principal_id,
                "tool": tool,
            },
        )
        raise ToolError(f"Credential lacks the {required_scope} scope")

    try:
        await charge_quota(credential, operation)
    except VaultQuotaExceeded as exc:
        raise ToolError(
            f"Rate limit exceeded; retry in {int(exc.retry_after + 0.999)}s"
        ) from exc
    return credential


def derive_idempotency_key(payload: dict[str, Any]) -> str:
    """Mint a contribution's idempotency key from its own content.

    The HTTP contract requires the caller to supply this key, which is right for
    a programmatic client that knows whether it is retrying. An agent does not:
    asking a model to invent one produces a fresh value on every attempt, which
    turns a network timeout into a duplicate note -- the exact failure the key
    exists to prevent.

    Deriving it from the content instead makes a retry of the same contribution
    replay by construction. The consequence, worth stating plainly: two
    *deliberately* identical contributions also collapse into one. In a
    corpus whose whole purpose is deduplication that is the correct reading of
    the same content arriving twice, but it is a behaviour choice, not an
    accident.

    Hashes the same canonical form ``canonical_request_digest`` does -- sorted
    keys, tight separators -- so key derivation and conflict detection cannot
    disagree about what "the same request" means. It is a distinct value from
    the request digest, and must be: the digest is what a reused key is checked
    *against*.
    """

    # json.dumps with sort_keys sorts *recursively*, which repr() does not.
    # An earlier version hashed repr(sorted(payload.items())), which sorted only
    # the top level: two contributions differing solely in the key order of a
    # nested dict -- facets is the realistic case -- derived different keys and
    # so wrote two notes for one contribution, the exact failure this function
    # exists to prevent. Sorting must be as deep as the data.
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = sha256(canonical.encode("utf-8")).hexdigest()
    # Prefixed so an operator reading vault_write_requests can tell a derived
    # key from a client-chosen one. Hex plus a hyphen satisfies the contract's
    # ^[A-Za-z0-9._:-]+$ and 8..128 length.
    return f"mcp-{digest}"


def _content_payload(
    title: str,
    body: str,
    summary: str | None,
    tags: Sequence[str] | None,
    aliases: Sequence[str] | None,
    facets: dict[str, list[str]] | None,
    related_ids: Sequence[str] | None,
    source_ids: Sequence[str] | None,
    source_url: str | None,
) -> dict[str, Any]:
    """Collect only the fields the caller actually supplied.

    Omitting unset fields rather than passing their defaults keeps
    ``exclude_unset`` meaningful in ``canonical_request_digest``: a request that
    named three fields must hash as a three-field request whichever transport it
    arrived on, or the same contribution would digest differently over MCP than
    over HTTP.
    """

    payload: dict[str, Any] = {"title": title, "body": body}
    if summary is not None:
        payload["summary"] = summary
    if tags:
        payload["tags"] = list(tags)
    if aliases:
        payload["aliases"] = list(aliases)
    if facets:
        payload["facets"] = facets
    if related_ids:
        payload["related_ids"] = list(related_ids)
    if source_ids:
        payload["source_ids"] = list(source_ids)
    if source_url is not None:
        payload["source_url"] = source_url
    return payload


def build_vault_mcp_server() -> VaultMCPServer:
    """Register the five tools over the existing services."""

    server = VaultMCPServer(
        name="hss-vault",
        title="HighScoreServer Knowledge Vault",
        instructions=(
            "A shared knowledge corpus of durable, reusable engineering notes. "
            "Search it before solving a problem, and contribute a note when you "
            "learn something worth outliving the current task. Always search "
            "first and read the nearest existing note in full before "
            "contributing: the corpus deduplicates on meaning, and a "
            "restatement of an existing note under a new title will be flagged "
            "rather than added."
        ),
    )

    @server.tool(name="vault_search")
    async def vault_search(query: str, limit: int = 10) -> dict[str, Any]:
        """Search the vault corpus by meaning and keyword.

        Returns notes ranked by a fusion of vector similarity and full-text
        search. Check `vector_status`: `used` means semantic matching was
        applied; `not_configured` means this deployment is lexical-only by
        choice; `failed` means the embedding provider errored and these results
        are degraded -- treat a `failed` result set as incomplete rather than
        as evidence that nothing matches.

        Args:
            query: What to look for, in natural language.
            limit: Maximum notes to return, 1 to 50.
        """

        await _authorized("vault_search")

        text = query.strip()
        if not text:
            raise ToolError("Search query must contain non-whitespace characters")
        if not 1 <= limit <= 50:
            raise ToolError("limit must be between 1 and 50")

        service = VaultSearchService(
            transactions=VaultTransactionService(get_vault_engine()),
            provider=get_embedding_provider(),
            text_search_config=resolve_text_search_config(),
        )
        outcome = await service.search(text, limit)
        return {
            "query": text,
            "profile_id": outcome.profile_id,
            "vector_status": outcome.vector_status,
            "hits": [
                {
                    **document_detail(result.document).model_dump(mode="json"),
                    "score": result.score,
                    "lexical_rank": result.lexical_rank,
                    "vector_rank": result.vector_rank,
                }
                for result in outcome.results
            ],
        }

    @server.tool(name="vault_get_note")
    async def vault_get_note(note_id: str) -> dict[str, Any]:
        """Fetch one vault note in full by its ID.

        Use the full 32-character ID returned by `vault_search`; a truncated ID
        does not resolve. Resolves active and archived notes -- an archived note
        is retired but legitimate history, so a `related_ids` reference to one
        still works.

        Args:
            note_id: The note's full ID.
        """

        await _authorized("vault_get_note")

        transactions = VaultTransactionService(get_vault_engine())
        documents = VaultDocumentRepository()
        async with transactions.transaction() as connection:
            document = await documents.get_by_id(
                connection,
                note_id,
                statuses=READABLE_STATUSES,
                readable_only=True,
            )
        if document is None:
            raise ToolError("Note not found")
        return document_detail(document).model_dump(mode="json")

    @server.tool(name="vault_contribute")
    async def vault_contribute(
        title: str,
        body: str,
        summary: str | None = None,
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
        facets: dict[str, list[str]] | None = None,
        related_ids: list[str] | None = None,
        source_ids: list[str] | None = None,
        source_url: str | None = None,
    ) -> dict[str, Any]:
        """Contribute a note through the governed write path.

        Search first and read the nearest existing note before calling this.
        The write is deduplicated against the corpus: a near-identical note is
        `flagged` for review rather than added, and `flagged` is a successful
        outcome, not an error to retry -- retrying it creates a second note that
        flags against the first.

        Retries are safe. The idempotency key is derived from the content, so
        re-sending the same contribution replays the original outcome instead of
        writing twice.

        Args:
            title: A declarative sentence stating the insight.
            body: The note in Markdown.
            summary: Optional one-line precis; contributes to matching.
            tags: Topic keywords. For what the note is *about*.
            aliases: Alternative titles someone might search for.
            facets: Classification as {name: [values]}, e.g.
                {"project": ["highscoreserver"]}. For which notes this one
                *belongs with*, not what it is about -- facets are excluded
                from matching on purpose.
            related_ids: Full IDs of related notes. Not existence-checked, so
                verify each one with vault_get_note before sending it.
            source_ids: Full IDs of notes this was derived from.
            source_url: Optional external source.
        """

        credential = await _authorized("vault_contribute")

        payload = _content_payload(
            title,
            body,
            summary,
            tags,
            aliases,
            facets,
            related_ids,
            source_ids,
            source_url,
        )
        payload["idempotency_key"] = derive_idempotency_key(payload)

        try:
            model = VaultContributionRequest.model_validate(payload)
        except ValueError as exc:
            raise ToolError(f"Invalid contribution: {exc}") from exc

        service = VaultContributionService(
            transactions=VaultTransactionService(get_vault_engine()),
            provider=get_embedding_provider(),
        )
        contribution = ContributionRequest(
            title=model.title,
            body=model.body,
            # The credential is the contributor. Taking it from the tool input
            # would let one principal write under another's name.
            contributed_by=f"agent:{credential.principal_id}",
            principal_id=credential.principal_id,
            idempotency_key=model.idempotency_key,
            request_sha256=canonical_request_digest(model),
            digest_version=REQUEST_DIGEST_VERSION,
            request_id=uuid4().hex,
            tags=tuple(model.tags),
            summary=model.summary,
            aliases=tuple(model.aliases),
            facets=model.facets,
            related_ids=tuple(model.related_ids),
            source_ids=tuple(model.source_ids),
            source_url=str(model.source_url) if model.source_url else None,
        )

        try:
            outcome = await service.contribute(contribution)
        except IdempotencyConflict as exc:
            # Derived keys make this near-unreachable: the same key implies the
            # same content. It survives as a guard against a digest-rule change.
            raise ToolError(
                "This contribution collides with a different stored request"
            ) from exc
        except DedupUnavailable as exc:
            logger.error("Refusing a vault contribution: no embedding provider")
            raise ToolError(
                "Contribution is unavailable: no embedding provider configured"
            ) from exc
        except EmbeddingInputTooLong as exc:
            raise ToolError(
                "Note exceeds the embedding model input limit; shorten it"
            ) from exc
        except EmbeddingError as exc:
            # Type only, never the message: an embedding exception can carry the
            # note body.
            logger.error(
                "Vault contribution failed to embed",
                extra={"error_type": type(exc).__name__},
            )
            raise ToolError("Contribution is temporarily unavailable") from exc

        if outcome.status == "invalid":
            raise ToolError(f"{outcome.message}: {'; '.join(outcome.errors)}")

        return {
            "status": outcome.status,
            "note_id": outcome.note_id,
            "message": outcome.message,
            "idempotent_replay": outcome.idempotent_replay,
            "similars": [
                {"note_id": s.note_id, "title": s.title, "score": s.score}
                for s in outcome.similars
            ],
            "errors": list(outcome.errors),
        }

    @server.tool(name="vault_update_note")
    async def vault_update_note(
        note_id: str,
        title: str,
        body: str,
        summary: str | None = None,
        tags: list[str] | None = None,
        aliases: list[str] | None = None,
        facets: dict[str, list[str]] | None = None,
        related_ids: list[str] | None = None,
        source_ids: list[str] | None = None,
        source_url: str | None = None,
    ) -> dict[str, Any]:
        """Replace one note's content in full.

        A replacement, not a patch: the arguments state what the note should now
        be, and an omitted list means an empty list. Fetch the note first and
        resend every field you intend to keep.

        Refuses with an error if the replacement would duplicate a different
        existing note; nothing is written in that case.

        Args:
            note_id: Full ID of the note to replace.
            title: The note's new title.
            body: The note's new Markdown body.
            summary: Optional one-line precis.
            tags: Topic keywords, replacing the existing set.
            aliases: Alternative titles, replacing the existing set.
            facets: Classification, replacing the existing set.
            related_ids: Related note IDs, replacing the existing set.
            source_ids: Source note IDs, replacing the existing set.
            source_url: Optional external source.
        """

        credential = await _authorized("vault_update_note")

        payload = _content_payload(
            title,
            body,
            summary,
            tags,
            aliases,
            facets,
            related_ids,
            source_ids,
            source_url,
        )
        try:
            model = VaultDocumentUpdateRequest.model_validate(payload)
        except ValueError as exc:
            raise ToolError(f"Invalid update: {exc}") from exc

        service = VaultDocumentUpdateService(
            transactions=VaultTransactionService(get_vault_engine()),
            provider=get_embedding_provider(),
        )
        update = UpdateRequest(
            document_id=note_id,
            title=model.title,
            body=model.body,
            principal_id=credential.principal_id,
            request_id=uuid4().hex,
            summary=model.summary,
            tags=tuple(model.tags),
            aliases=tuple(model.aliases),
            facets=model.facets,
            related_ids=tuple(model.related_ids),
            source_ids=tuple(model.source_ids),
            source_url=str(model.source_url) if model.source_url else None,
        )

        try:
            outcome = await service.update(update)
        except DocumentNotFound as exc:
            raise ToolError("Note not found") from exc
        except UpdateWouldDuplicate as exc:
            collisions = ", ".join(
                f"{s.note_id} ({s.score:.3f})" for s in exc.similars
            )
            raise ToolError(
                f"Replacement would duplicate an existing note: {collisions}. "
                "Nothing was written."
            ) from exc
        except DedupUnavailable as exc:
            logger.error("Refusing a vault update: no embedding provider")
            raise ToolError(
                "Update is unavailable: no embedding provider configured"
            ) from exc
        except EmbeddingInputTooLong as exc:
            raise ToolError(
                "Note exceeds the embedding model input limit; shorten it"
            ) from exc
        except EmbeddingError as exc:
            logger.error(
                "Vault update failed to embed",
                extra={"error_type": type(exc).__name__},
            )
            raise ToolError("Update is temporarily unavailable") from exc

        if outcome.errors:
            raise ToolError(f"{outcome.message}: {'; '.join(outcome.errors)}")

        return {
            "note_id": outcome.note_id,
            "message": outcome.message,
            "re_embedded": outcome.re_embedded,
        }

    @server.tool(name="vault_retire_note")
    async def vault_retire_note(note_id: str) -> dict[str, Any]:
        """Permanently remove a note from the vault.

        This deletes. It does not archive, and there is no undo -- no row is
        left behind that anyone can resolve afterwards. Use it only for content
        that is *wrong*, never for content that is merely superseded.

        Refuses while the note is evidence in an open review case.

        Args:
            note_id: Full ID of the note to remove.
        """

        credential = await _authorized("vault_retire_note")

        service = VaultDocumentRetireService(
            transactions=VaultTransactionService(get_vault_engine()),
        )
        try:
            await service.retire(
                RetireRequest(
                    document_id=note_id,
                    principal_id=credential.principal_id,
                    request_id=uuid4().hex,
                )
            )
        except DocumentNotFound as exc:
            raise ToolError("Note not found") from exc
        except DocumentUnderReview as exc:
            raise ToolError(f"Cannot retire a note under review: {exc}") from exc

        return {"note_id": note_id, "retired": True}

    return server


def _transport_security() -> TransportSecuritySettings:
    """Host and Origin policy for the Streamable HTTP transport.

    The SDK enables DNS-rebinding protection by default and validates the
    ``Host`` header against ``127.0.0.1``. That default is written for the
    common case -- an MCP server bound to loopback on a developer's machine,
    where a malicious page resolving a hostname to 127.0.0.1 could otherwise
    reach it from the browser. **This server is neither loopback nor
    browser-reachable**: it is public, it is behind Heroku's router, and it
    authenticates with a bearer token a browser cannot attach cross-origin.

    Left at the default, every request in production is rejected with 421
    Misdirected Request, because the Host header is the application's real
    hostname and not 127.0.0.1 -- a total outage with a confusing symptom.

    So the protection is off unless an operator opts in by naming the hosts.
    Setting ``VAULT_MCP_ALLOWED_HOSTS`` to a comma-separated list turns it back
    on, restricted to those values, which is the right configuration for a
    deployment that wants defence in depth against a misrouted proxy.
    """

    allowed = os.environ.get("VAULT_MCP_ALLOWED_HOSTS", "").strip()
    if not allowed:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = [host.strip() for host in allowed.split(",") if host.strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=[f"https://{host}" for host in hosts],
    )


def build_vault_mcp_app(path: str = "/") -> Starlette:
    """The mounted ASGI application, authenticated and guarded.

    ``stateless_http`` because Heroku has no sticky sessions: a session-bound
    transport works on one dyno and breaks on the second, in a way that only
    appears under scale. Stateless costs nothing here -- every tool call is
    self-contained, and no vault tool streams progress or holds server-side
    state between calls.
    """

    server = build_vault_mcp_server()
    app = server.streamable_http_app(
        streamable_http_path=path,
        stateless_http=True,
        transport_security=_transport_security(),
    )
    app.add_middleware(VaultMCPAuthMiddleware)
    return app


@asynccontextmanager
async def vault_mcp_lifespan(app: Starlette) -> AsyncIterator[None]:
    """Run the Streamable HTTP session manager for the mounted app.

    **A mount does not run the mounted application's lifespan.** Starlette only
    dispatches lifespan to the outermost app, so the transport's session manager
    -- which the SDK starts in that lifespan -- never starts, and every tool call
    fails on a task group that was never entered. The host therefore has to
    enter this alongside its own startup. The symptom if it is skipped is not a
    missing route but a mounted endpoint that answers and then errors on use.

    Takes the application rather than reaching for a module-level one, and that
    is load-bearing: ``StreamableHTTPSessionManager.run()`` refuses a second
    call on the same instance. A cached app shared between two ``create_app()``
    results therefore starts once and then fails for the second -- which is
    every test that builds an app, and any process that builds two.
    """

    async with app.router.lifespan_context(app):
        yield
