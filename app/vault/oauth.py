"""The vault's OAuth 2.1 authorization server, per vault ADR 0024.

Implements the MCP SDK's ten-method ``OAuthAuthorizationServerProvider`` so that
a client with no way to send a static header -- the claude.ai web connector --
can still reach the vault. Everything the SDK supplies is used as supplied:
``create_auth_routes`` provides ``/authorize``, ``/token``, ``/register`` and
``/revoke``, plus RFC 8414 and RFC 9728 metadata, and it verifies PKCE, the
``redirect_uri`` round trip, and code expiry before this module is consulted.
What is here is the part the SDK cannot know.

**An issued access token is a ``vault_agent_credentials`` row, and its string is
an ordinary ``hssv1_`` token.** That is the load-bearing choice and it is why
this module changes nothing downstream: ``principal.resolve_credential`` already
verifies exactly that shape, so the MCP mount, the REST routes, the scope
checks, the quota buckets and ``contributed_by`` all keep working through one
mechanism. There is no token table and no second identity type -- a parallel one
would duplicate scopes, revocation, quotas and attribution, and every duplicate
is somewhere the two paths could eventually disagree about what a caller may do.

**Refresh tokens rotate, and replay is detected.** ADR 0024's 2026-08-21
amendment: the access credential lives an hour and the client renews itself, so
expiry costs a machine round trip rather than an operator redoing a browser
flow. OAuth 2.1 requires a public client's refresh token to be sender-constrained
or rotated with replay detection; this is rotation, and ``load_refresh_token``
below is where the detection happens.

**``authorize`` authenticates nobody.** It stores the request against a nonce and
redirects to the vault's own login page, which is the SDK's own pattern for
handing off to a third party and works the same handing off to ourselves. The
page and its form live in ``oauth_routes.py``; this module owns everything that
is not HTTP.

**Registration is open, and grants nothing.** The specification expects a web
client to self-register, so anyone may. A registration is not an authorization:
what gates access is the operator personally approving one at the login page,
and the scopes the resulting credential carries -- capped at
``OAUTH_BASELINE_SCOPES`` by ``ClientRegistrationOptions``. The baseline includes
``vault:propose`` because proposals are inert workflow records; ``vault:update``,
``vault:delete`` and ``vault:review`` remain unreachable by request. ADR 0021's
defence therefore still holds: a web-authorized client can suggest an exact
replacement but cannot apply, alter, or delete established knowledge.
"""

import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from sqlalchemy import text as sql_text

from .auth import TOKEN_PREFIX, hash_secret, parse_token, secret_matches
from .constants import (
    ACCESS_TOKEN_TTL_SECONDS,
    OAUTH_BASELINE_SCOPES,
    OAUTH_CLIENT_LOCK_KEY,
    PENDING_AUTHORIZATION_TTL_SECONDS,
    REFRESH_TOKEN_TTL_SECONDS,
)
from .repository import (
    VaultAgentCredentialRepository,
    VaultAuditEventRepository,
    VaultOAuthAuthorizationCodeRepository,
    VaultOAuthClientRepository,
    VaultOAuthGrantRepository,
    VaultOAuthPendingAuthorizationRepository,
    VaultOAuthRefreshTokenRepository,
)
from .service import VaultTransactionService


logger = logging.getLogger(__name__)

# Where `authorize` sends the operator. Relative rather than absolute: a
# redirect Location may be, and one built from VAULT_PUBLIC_URL would break the
# flow whenever that variable disagreed with the host actually being addressed.
LOGIN_PATH = "/vault/login"

# The query parameter carrying the nonce to the login page. Short because it
# ends up in a URL the operator sees.
NONCE_PARAM = "req"

# Bytes of entropy for a nonce, an authorization code, a refresh token, and a
# CSRF token. 32 bytes is what `issue_vault_credential` uses for a credential
# secret, and none of these is longer-lived than one.
_SECRET_BYTES = 32

# Bytes for a credential id. Matches scripts/issue_vault_credential.py, and the
# hex expansion has to satisfy vault_agent_credentials_id_format.
_ID_BYTES = 8

# How a principal is named for an OAuth client: the prefix plus the *registration
# id*, which the SDK generates as a uuid4 per `/register` call.
#
# This was `slugify(client_name)` until 2026-08-23, and that was wrong for the
# reason the consent screen was wrong -- it read an unverified name as identity.
# Registration is open, so `client_name` is free text: two clients calling
# themselves "Claude", or "CLAUDE" and "Claude!", or anything at all past the
# slug length limit, collapsed to one principal. The old comment called that
# deliberate on the grounds that they are "the same logical client", which is
# only true if the name means something, and nothing checks that it does.
#
# The collision was not cosmetic. `principal_id` is the actor across three
# isolation boundaries: `vault_write_requests` keys idempotency on
# (principal_id, idempotency_key), the token buckets key quota on
# (principal_id, operation), and `contributed_by` and the audit trail record it.
# Two separately approved clients therefore shared an idempotency namespace and
# a quota, and their writes were indistinguishable afterwards.
#
# The cost is that `contributed_by` reads `agent:oauth-<uuid>` rather than
# `agent:oauth-claude`. That is the right trade and the readable name is not
# lost: `vault_agent_credentials.display_name` carries it, one join away. It is
# also the more honest arrangement, because a client may re-register under a new
# name -- so a principal derived from the name would be derived from something
# mutable, while the credentials already issued kept the old spelling.
#
# A client that re-registers gets a new registration id and therefore a new
# principal: a fresh quota bucket and a fresh idempotency namespace. Correct --
# a reinstalled client remembers neither.
PRINCIPAL_PREFIX = "oauth-"


def _expiry(ttl_seconds: int) -> datetime:
    """An absolute expiry ``ttl_seconds`` from now, in UTC.

    Computed in Python rather than as ``now() + interval`` because it is passed
    to a repository shared with the operator tooling, which takes a datetime.
    The difference is a round trip's worth of clock skew on a column measured in
    hours.
    """

    return datetime.now(UTC) + timedelta(seconds=ttl_seconds)


def new_secret() -> str:
    """A fresh high-entropy secret: a nonce, a code, a refresh token, a CSRF token.

    One function for all four because they are the same kind of thing -- machine
    generated, full entropy, stored only as a digest (ADR 0015). Public because
    ``oauth_routes`` mints authorization codes and needs the same construction;
    two generators would eventually disagree about how much entropy is enough.
    """

    return secrets.token_hex(_SECRET_BYTES)


def principal_for_client_id(client_id: str) -> str:
    """The principal id for a registration, given only its id.

    The revocation path has a token and a family but no client object, and it
    was recording the bare ``client_id`` -- so an audit query for a principal
    found its registration, its issuance, and its refreshes, but not its
    revocations. Off by exactly the prefix, which is the kind of near-miss that
    reads as correct until someone needs the trail.
    """

    return f"{PRINCIPAL_PREFIX}{client_id}"


def principal_for_client(client: OAuthClientInformationFull) -> str:
    """The principal id an OAuth client's credentials carry.

    The registration id, never the client's declared name. See
    ``PRINCIPAL_PREFIX`` for why: the name is unverified free text on an open
    registration endpoint, and a principal built from it collides across
    separately approved clients that share an idempotency namespace and a quota
    as a result.

    Nothing is slugified because nothing needs to be -- a uuid4 from the SDK's
    registration handler is already safe in an identifier, which is the second
    reason to prefer it over caller-supplied text.
    """

    return principal_for_client_id(client.client_id)


def access_token_string(credential_id: str, secret: str) -> str:
    """The bearer token an OAuth client presents.

    Deliberately the same ``hssv1_<id>_<secret>`` shape an operator-issued
    credential has, because it *is* one. That is what lets the entire resource
    server -- the MCP mount, the REST routes, scope checks, quotas -- stay
    untouched by this feature.
    """

    return f"{TOKEN_PREFIX}_{credential_id}_{secret}"


class VaultAuthorizationProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """The ten methods, over the stores migration 0013 and 0014 created."""

    def __init__(
        self,
        transactions: Callable[[], VaultTransactionService],
    ) -> None:
        # A factory, not an instance. Routes are built while the application is
        # constructed and the vault engine is not created until the lifespan
        # runs, so resolving it here would raise at import-time assembly. Every
        # method opens its own transaction on demand, which is also what
        # `routes.py` does.
        self._transactions_for = transactions
        self._clients = VaultOAuthClientRepository()
        self._grants = VaultOAuthGrantRepository()
        self._pending = VaultOAuthPendingAuthorizationRepository()
        self._codes = VaultOAuthAuthorizationCodeRepository()
        self._refresh = VaultOAuthRefreshTokenRepository()
        self._credentials = VaultAgentCredentialRepository()
        self._audit = VaultAuditEventRepository()

    # ------------------------------------------------------------ clients ----

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Load a registration, or None.

        None rather than an exception: the SDK renders it as the specification's
        ``invalid_client``, where raising would surface as a 500.
        """

        async with self._transactions_for().transaction() as connection:
            stored = await self._clients.get(connection, client_id)
        if stored is None:
            return None
        return OAuthClientInformationFull.model_validate(stored.client_info)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with self._transactions_for().transaction() as connection:
            await self._clients.upsert(
                connection,
                client_id=client_info.client_id,
                client_info=client_info.model_dump(mode="json"),
            )
        # **No audit event here, deliberately.** `/register` is public and
        # unauthenticated, and an audit row is permanent -- nothing prunes
        # `vault_audit_events`, by ADR 0002's design, because it is the durable
        # record of what was done to the corpus. Writing one per anonymous
        # registration made an unauthenticated caller the author of unbounded
        # permanent rows, with only a rate limit between them and the disk. A
        # limit slows that; it does not bound it.
        #
        # Nothing durable is lost. A registration is not an action on the vault:
        # it grants nothing until an operator approves an authorization, and
        # *that* is audited. The fact itself lives on the row this just wrote --
        # `vault_oauth_clients.registered_at` -- for as long as the registration
        # does, and the two are pruned together rather than one outliving the
        # other forever. What remains is the structured log below, which is
        # bounded operationally rather than by a table.
        logger.info(
            "vault oauth client registered",
            extra={"client_id": client_info.client_id},
        )

    # ------------------------------------------------------ authorization ----

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Park the request against a nonce and redirect to the login page.

        No authentication happens here and none can: the protocol hands this
        method no request and no way to prompt. What it does is make the
        request survive a round trip through a browser -- which, on a
        multi-worker deployment, means Postgres and not memory. The spike
        proved that rather than assuming it: registration arrives
        server-to-server from the vendor's backend while ``/authorize`` is a
        browser navigation, so the two halves land on different workers.

        The CSRF token is minted here, with the nonce, because this is the only
        moment both halves of the pair can be written atomically. Its digest
        goes in the row; the plaintext is handed to the form by the login GET,
        which reads it back out of nothing -- see ``oauth_routes``.

        **Serialized against stale-client pruning**, which is why the lock is
        here. Pruning spares a client with an authorization in flight, but that
        is a fact about rows that exist: the SDK loads the client in one
        transaction and calls this method in another, so a sweep can read "no
        pending authorization, no code, no live token", delete the registration,
        and commit in the window before the row below is written. The insert
        then violates its foreign key and the operator gets a 500 for a flow
        that was valid when it started. Under the reverse interleaving the
        delete blocks on the foreign key's row lock and then removes the pending
        row it was waiting for, by cascade, which is quieter and no better.

        With the lock held, the sweep either has not started -- and will then
        see the pending row -- or has finished, and the re-read below finds the
        registration gone and says so in the protocol's own vocabulary.
        """

        nonce = new_secret()
        csrf_token = new_secret()
        async with self._transactions_for().transaction() as connection:
            await connection.execute(
                sql_text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": OAUTH_CLIENT_LOCK_KEY},
            )
            # Pruned between the SDK's lookup and this transaction? The check
            # and the insert are both under the lock, so the answer cannot go
            # stale between them.
            registered = (
                await self._clients.get(connection, client.client_id) is not None
            )
            if registered:
                await self._pending.create(
                    connection,
                    nonce=nonce,
                    client_id=client.client_id,
                    params=params.model_dump(mode="json"),
                    csrf_token=csrf_token,
                    ttl_seconds=PENDING_AUTHORIZATION_TTL_SECONDS,
                )

        if not registered:
            # Raised *outside* the transaction deliberately. `AuthorizeError` is
            # a frozen dataclass, and unwinding through an `@asynccontextmanager`
            # runs `exc.__traceback__ = tb` in contextlib, which a frozen
            # instance refuses -- turning a clean protocol error into a
            # FrozenInstanceError from the exit path. Any AuthorizeError raised
            # in this module has to leave the transaction block first.
            #
            # `unauthorized_client` and not `invalid_request`: the request was
            # well formed and the registration behind it is simply gone, and
            # registering again is the useful response.
            raise AuthorizeError(
                error="unauthorized_client",
                error_description=(
                    "This client registration is no longer valid. Register again."
                ),
            )
        # The CSRF plaintext travels with the nonce so the form can carry it
        # without a second lookup. Both are in a URL the operator's browser
        # holds, which is the same trust boundary the nonce already sits on.
        return f"{LOGIN_PATH}?{NONCE_PARAM}={nonce}&csrf={csrf_token}"

    # --------------------------------------------------- authorization code --

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        """Look up a code without consuming it.

        The SDK splits load from exchange and does real work between them --
        PKCE verification, the ``redirect_uri`` round-trip check, expiry. A
        consuming load would destroy a code whenever any of those failed, when
        the client is still entitled to retry.
        """

        async with self._transactions_for().transaction() as connection:
            stored = await self._codes.get(connection, authorization_code)
        if stored is None or stored.client_id != client.client_id:
            # A code belonging to a different client is treated as absent
            # rather than as an error naming the real owner.
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=list(stored.scopes),
            expires_at=stored.expires_at.timestamp(),
            client_id=stored.client_id,
            code_challenge=stored.code_challenge,
            redirect_uri=stored.redirect_uri,  # type: ignore[arg-type]
            redirect_uri_provided_explicitly=stored.redirect_uri_provided_explicitly,
            resource=stored.resource,
            subject=stored.subject,
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        """Redeem the code and mint the credential it stands for.

        One transaction: the code is consumed, the credential row is inserted,
        and the first refresh token of a new family is written together. A
        partial commit here would either strand a code nobody can spend or
        issue a credential no ledger records.

        The refusal is raised **after** the block, for the reason recorded at
        every other protocol error in this module: ``TokenError`` is a frozen
        dataclass and unwinding through an ``@asynccontextmanager`` runs
        ``exc.__traceback__ = tb`` in contextlib, which a frozen instance
        refuses. Raised inside, a lost redemption race returned
        ``FrozenInstanceError`` instead of ``invalid_grant``.

        Unreachable sequentially, which is why it survived two passes over this
        file: ``load_authorization_code`` answers None for a spent code, so the
        SDK refuses before reaching here. Only two exchanges that both load
        before either redeems arrive at the branch below.
        """

        async with self._transactions_for().transaction() as connection:
            redeemed = await self._codes.redeem(connection, authorization_code.code)
            if redeemed is not None:
                family_id = uuid4()
                await self._grants.create(
                    connection,
                    family_id=family_id,
                    client_id=client.client_id,
                    authorized_scopes=redeemed.scopes,
                )
                return await self._issue(
                    connection,
                    client=client,
                    scopes=redeemed.scopes,
                    subject=redeemed.subject,
                    family_id=family_id,
                    operation="vault.oauth.authorize",
                )

        # Expired, already spent, or racing another exchange. RFC 6749 calls a
        # reused code invalid_grant, and the caller is not told which of the
        # three it was.
        raise TokenError(
            error="invalid_grant",
            error_description="authorization code is not redeemable",
        )

    # -------------------------------------------------------- refresh flow ---

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        """Load a refresh token, and treat a consumed one as an attack.

        **This is the replay detection half of rotation.** A token that exists
        but is already consumed cannot be an honest client's: rotation gave that
        client a replacement, so presenting the old one is evidence it was
        captured -- either the attacker is using it, or the legitimate client is
        and the attacker holds the replacement. Which of the two is unknowable
        from here, so the response is to burn the whole family: every credential
        ever minted in that chain is revoked and every unconsumed token in it is
        consumed. The legitimate client re-authorizes; the attacker gets
        nothing.

        Doing that inside a method named "load" is not tidy, and it is where it
        has to go: the SDK offers no hook between recognising a refresh token
        and refusing it, and refusing without responding would leave a captured
        token useful until it expired.
        """

        async with self._transactions_for().transaction() as connection:
            stored = await self._refresh.get(connection, refresh_token)
            if stored is None or stored.client_id != client.client_id:
                return None
            if stored.consumed_at is not None:
                revoked = await self._credentials.revoke(
                    connection,
                    await self._refresh.credential_ids_in_family(
                        connection, stored.family_id
                    ),
                )
                await self._refresh.consume_family(connection, stored.family_id)
                await self._audit.record(
                    connection,
                    operation="vault.oauth.refresh",
                    outcome="replay_detected",
                    request_id=uuid4().hex,
                    principal_id=principal_for_client(client),
                    target_type="oauth_refresh_family",
                    target_id=str(stored.family_id),
                )
                logger.warning(
                    "vault oauth refresh token replayed; family revoked",
                    extra={
                        "client_id": client.client_id,
                        "family_id": str(stored.family_id),
                        "credentials_revoked": revoked,
                    },
                )
                return None

        return RefreshToken(
            token=refresh_token,
            client_id=stored.client_id,
            scopes=list(stored.scopes),
            expires_at=int(stored.expires_at.timestamp()),
            subject=stored.subject,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate: consume this token, revoke its credential, mint the next pair.

        The new token stays in the same ``family_id``, which is what makes the
        chain revocable as a unit when one of its members is replayed.

        Narrowing is allowed and widening is not -- the SDK has already checked
        that ``scopes`` is a subset of what the token carries. Narrowing is
        honoured rather than ignored so a client that asks for less gets less.
        """

        async with self._transactions_for().transaction() as connection:
            consumed = await self._refresh.consume(connection, refresh_token.token)
            if consumed is not None:
                await self._credentials.revoke(
                    connection, [consumed.credential_id]
                )
                return await self._issue(
                    connection,
                    client=client,
                    scopes=scopes or list(consumed.scopes),
                    subject=consumed.subject,
                    family_id=consumed.family_id,
                    operation="vault.oauth.refresh",
                )

            # **A miss here can still be a replay, and was not treated as one.**
            # ``load_refresh_token`` checks ``consumed_at``, but the SDK loads
            # and exchanges in separate calls, so two concurrent requests both
            # pass that check while the row is unconsumed. One wins this
            # conditional UPDATE and rotates; the other lands here -- which is
            # exactly the shape of a captured token being spent by two parties
            # at once, and precisely what rotation-with-detection exists to
            # catch.
            #
            # So the reason matters. Re-read the row: consumed means somebody
            # else spent it and the family burns. Absent or expired is an
            # ordinary bad request.
            stored = await self._refresh.get(connection, refresh_token.token)
            replayed = stored is not None and stored.consumed_at is not None
            if replayed:
                await self._burn_family(
                    connection,
                    client=client,
                    family_id=stored.family_id,
                    outcome="replay_detected_on_exchange",
                )

        # **Raised after the transaction commits, never inside it.** This block
        # is the security response -- every credential in the family revoked,
        # every unconsumed token in it spent, and the audit event that says why.
        # Raising inside the `async with` would roll all three back on the way
        # out, leaving the caller a correct `invalid_grant` and the attacker a
        # working replacement token: the detection would run, report itself, and
        # then undo itself. `load_refresh_token`'s burn returns normally and so
        # never had this problem, which is why a sequential replay test could
        # not see it.
        raise TokenError(
            error="invalid_grant",
            error_description="refresh token is not redeemable",
        )

    # -------------------------------------------------------- access tokens --

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Resolve a bearer token to its credential.

        Present so the SDK's revocation endpoint can act on an access token.
        The vault's own surfaces do **not** come through here: they call
        ``principal.resolve_credential``, which does the same verification with
        the timing-equalised comparison and the ``last_used_at`` bookkeeping
        this does not need.

        **``client_id`` is the registered OAuth client id, not the principal**,
        and that distinction is load-bearing rather than cosmetic. The SDK's
        revocation handler calls ``revoke_token`` only when the loaded token's
        ``client_id`` equals the authenticated client's. Returning
        ``principal_id`` -- which is ``oauth-<slug>`` -- never matched a
        registration uuid, so ``/revoke`` answered 200 and revoked nothing. The
        refresh path happened to work because a refresh token already carried
        the real client id, which is why the shipped test passed over a broken
        access-token path.

        **None for an operator-issued credential**, which belongs to no client.
        That is the right answer rather than a gap: an ``hssv1_`` credential
        minted by ``issue_vault_credential`` must not be revocable through an
        endpoint any self-registered client may call.
        """

        parsed = parse_token(token)
        if parsed is None:
            return None
        async with self._transactions_for().transaction() as connection:
            credential = await self._credentials.get(connection, parsed.credential_id)
            if credential is None:
                return None
            owner = await self._refresh.client_and_family_for_credential(
                connection, parsed.credential_id
            )
        if owner is None:
            return None
        # secret_matches, not `!=`: this path serves the SDK's /revoke, and the
        # rest of the resource server already compares digests in constant time.
        if not secret_matches(credential, parsed.secret):
            return None
        if not credential.is_active():
            return None
        client_id, _family_id = owner
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=list(credential.scopes),
            expires_at=(
                int(credential.expires_at.timestamp())
                if credential.expires_at is not None
                else None
            ),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke one token, and everything it can still produce.

        RFC 7009 asks that revoking a refresh token also invalidate the access
        tokens issued from it, and there is no reason to be narrower in the
        other direction either: an operator revoking anything in a chain means
        the chain. So both cases resolve to a family where one exists.
        """

        async with self._transactions_for().transaction() as connection:
            if isinstance(token, RefreshToken):
                stored = await self._refresh.get(connection, token.token)
                if stored is None:
                    return
                family_id = stored.family_id
            else:
                # **An access token revokes its whole family too.** Revoking
                # only the presented credential leaves its refresh token able to
                # mint a replacement moments later, which is not revocation --
                # the caller asked for the grant to stop working and would have
                # no way to learn that it had not.
                parsed = parse_token(token.token)
                if parsed is None:
                    return
                owner = await self._refresh.client_and_family_for_credential(
                    connection, parsed.credential_id
                )
                if owner is None:
                    # No family: an operator-issued credential, which this
                    # endpoint does not govern. ``load_access_token`` already
                    # refuses to return one; this is the second layer.
                    return
                _client_id, family_id = owner

            await self._burn_family(
                connection,
                client=None,
                family_id=family_id,
                outcome="revoked",
                principal_id=principal_for_client_id(token.client_id),
            )

    async def _burn_family(
        self,
        connection,
        *,
        client: OAuthClientInformationFull | None,
        family_id: UUID,
        outcome: str,
        principal_id: str | None = None,
    ) -> int:
        """Revoke every credential in a rotation chain and consume its tokens.

        One implementation for the two paths that must do it -- an explicit
        revocation and a detected replay -- because they have to agree about
        what "the whole family" means. Revoking the credentials without
        consuming the tokens would leave one able to mint a fresh credential;
        consuming without revoking would leave the current access token live for
        the rest of its hour.
        """

        revoked = await self._credentials.revoke(
            connection,
            await self._refresh.credential_ids_in_family(connection, family_id),
        )
        await self._refresh.consume_family(connection, family_id)
        await self._audit.record(
            connection,
            operation="vault.oauth.revoke",
            outcome=outcome,
            request_id=uuid4().hex,
            principal_id=(
                principal_id
                if principal_id is not None
                else (principal_for_client(client) if client is not None else None)
            ),
            target_type="oauth_refresh_family",
            target_id=str(family_id),
        )
        logger.info(
            "vault oauth family burned",
            extra={
                "family_id": str(family_id),
                "outcome": outcome,
                "credentials_revoked": revoked,
            },
        )
        return revoked

    async def exchange_identity_assertion(
        self,
        client: OAuthClientInformationFull,
        params: Any,
    ) -> OAuthToken:
        """Unreachable: ``identity_assertion_enabled`` is off.

        Raising rather than quietly returning a token, because reaching this
        would mean the SDK was configured to advertise a grant profile nobody
        decided to support -- the same reasoning ADR 0004's ``Merge`` branch
        uses.
        """

        raise NotImplementedError(
            "identity assertion is not enabled for the vault authorization server"
        )

    # -------------------------------------------------------------- minting --

    async def _issue(
        self,
        connection,
        *,
        client: OAuthClientInformationFull,
        scopes: list[str] | tuple[str, ...],
        subject: str | None,
        family_id: UUID,
        operation: str,
    ) -> OAuthToken:
        """Mint one credential and one refresh token, inside the caller's transaction.

        Both halves or neither: a credential with no refresh token strands the
        client at expiry, and a refresh token naming a credential that was never
        inserted is a foreign key violation at best.

        The credential's secret exists only in this frame and in the response.
        Only its SHA-256 is written, so a leaked database yields hashes rather
        than tokens (ADR 0015), and the token is unrecoverable afterwards --
        which is why refresh has to mint a new credential rather than re-key
        this one.
        """

        authorized = sorted(set(scopes) & set(OAUTH_BASELINE_SCOPES))
        grant = await self._grants.set_authorized_scopes(
            connection,
            family_id,
            authorized,
        )
        if grant is None:
            raise RuntimeError("OAuth refresh family has no durable grant")

        credential_id = secrets.token_hex(_ID_BYTES)
        secret = new_secret()
        granted = sorted(set(grant.authorized_scopes) | set(grant.entitled_scopes))
        principal_id = principal_for_client(client)

        await self._credentials.create(
            connection,
            credential_id=credential_id,
            principal_id=principal_id,
            display_name=(client.client_name or client.client_id)[:200],
            secret_sha256=hash_secret(secret),
            scopes=granted,
            expires_at=_expiry(ACCESS_TOKEN_TTL_SECONDS),
        )
        refresh_token = new_secret()
        await self._refresh.create(
            connection,
            token=refresh_token,
            family_id=family_id,
            client_id=client.client_id,
            credential_id=credential_id,
            scopes=granted,
            subject=subject,
            ttl_seconds=REFRESH_TOKEN_TTL_SECONDS,
        )
        await self._audit.record(
            connection,
            operation=operation,
            outcome="issued",
            request_id=uuid4().hex,
            principal_id=principal_id,
            target_type="credential",
            target_id=credential_id,
        )
        logger.info(
            "vault oauth credential issued",
            extra={
                "client_id": client.client_id,
                "credential_id": credential_id,
                "scopes": granted,
            },
        )
        return OAuthToken(
            access_token=access_token_string(credential_id, secret),
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            scope=" ".join(granted),
            refresh_token=refresh_token,
        )


def baseline_scopes() -> list[str]:
    """What a self-registering client receives and the most it may request.

    A list because ``ClientRegistrationOptions`` wants one; the tuple in
    ``constants`` is the statement of record.
    """

    return list(OAUTH_BASELINE_SCOPES)
