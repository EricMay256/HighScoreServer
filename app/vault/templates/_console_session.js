/* The console session: one OAuth client's whole lifecycle, shared by every
   vault console page.

   Included into a page's own <script>, so it is textually part of that script
   rather than a second one -- the declarations here and the page's rendering
   code sit in one scope, exactly as they did when this lived in review.html.

   Everything console-specific arrives through `CFG`, which the page renders
   into a JSON script tag:

     apiBase      where the vault's API is mounted
     scopes       what this console asks for, and the most it may hold
     consolePath  its own path, which is also its OAuth redirect URI
     clientName   what it calls itself at registration (unverified, display)
     storePrefix  namespace for its browser storage and its refresh lock

   `storePrefix` is what keeps two consoles apart. Sharing it would mean two
   pages writing one session record and presenting each other's refresh
   tokens -- which the authorization server reads as a captured credential and
   answers by burning the family (see `withRefreshLock`).

   The page provides `render()`, and markup carrying `#messages` and a `#who`
   element for the header. */

const CFG = JSON.parse(document.getElementById("cfg").textContent);
const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

/* ---------- OAuth: authorization code + PKCE against our own AS ---------- */
/* The access token stays in sessionStorage: it lasts an hour and is renewable
   from the persisted record, so there is nothing to gain by keeping it on
   disk. The record that *is* persisted -- registration plus refresh token --
   is described at `loadSession`. */
const STORE = {
  verifier: CFG.storePrefix + ".pkce", state: CFG.storePrefix + ".state",
  token: CFG.storePrefix + ".token", session: CFG.storePrefix + ".session",
};

/* The registration and the refresh token are one durable record, stored
   together because they are only valid together: refreshing presents both, and
   a client_id from one authorization cannot renew a token from another.
   Persisted rather than session-scoped so closing the tab does not destroy the
   family -- an operator-granted entitlement is keyed to the family (the
   reviewer console's vault:review is the case that forced this), so a lost
   session means re-running `grant-oauth`, and a per-tab session made that a
   constant chore rather than the monthly one the refresh lifetime implies.
   `obtained` bounds it: `prune_vault_oauth` deletes a registration once its
   refresh token expires, and the authorization server answers a deleted
   client_id with a direct 400 and no redirect, so nothing would tell us. We
   therefore discard the pair on our own schedule rather than waiting for an
   error that never arrives. */
/* Declared above the session helpers on purpose. `saveSession` stamps every
   record with `randomString`, and the record is built during module
   initialization -- so a `const` declared further down sits in its temporal
   dead zone and throws when the migration runs, which a broad catch then
   turns into a silent "no legacy session". */
const b64url = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const randomString = () => b64url(crypto.getRandomValues(new Uint8Array(32)));

const REFRESH_TTL_MS = 30 * 24 * 60 * 60 * 1000;

function loadSession() {
  try {
    const raw = localStorage.getItem(STORE.session);
    if (!raw) return null;
    const saved = JSON.parse(raw);
    if (!saved.client || !saved.obtained) return null;
    if (Date.now() - saved.obtained > REFRESH_TTL_MS) {
      localStorage.removeItem(STORE.session);
      return null;
    }
    return saved;
  } catch (err) { return null; }
}

/* Every write re-stamps. The stamp is how a tab tells "the record I have been
   using" from "a record another tab has since replaced" -- which is the
   difference between ending your own session and destroying someone else's. */
function saveSession(next) {
  next.stamp = randomString();
  try { localStorage.setItem(STORE.session, JSON.stringify(next)); }
  catch (err) { /* Private mode: the session simply will not persist. */ }
  return next;
}

/* One-time upgrade from the session-scoped format this replaced. The
   deployed reviewer console wrote `<storePrefix>.client_id` and
   `<storePrefix>.refresh` into sessionStorage, which is why the legacy keys
   are derived from the prefix rather than named: a console that never used
   that format finds nothing and moves on. Without this, the first 401 after
   a rollout finds no refresh token, ends the session, and re-authorizes into
   a new family -- which inherits no entitlement, so every live reviewer would
   have to run `grant-oauth` again. The legacy keys are removed only after the
   new record is written, so an interrupted upgrade repeats rather than
   loses. */
function migrateLegacySession() {
  try {
    const client = sessionStorage.getItem(CFG.storePrefix + ".client_id");
    if (!client) return null;
    const upgraded = {
      client,
      refresh: sessionStorage.getItem(CFG.storePrefix + ".refresh") || null,
      /* Dated now, not at the original authorization: the true obtained-at was
         never recorded. This can only over-estimate the remaining lifetime, and
         a refresh that fails is handled; a record discarded early would cost
         the entitlement this migration exists to keep. */
      obtained: Date.now(),
    };
    saveSession(upgraded);
    if (localStorage.getItem(STORE.session)) {
      sessionStorage.removeItem(CFG.storePrefix + ".client_id");
      sessionStorage.removeItem(CFG.storePrefix + ".refresh");
    }
    return upgraded;
  } catch (err) { return null; }
}

/* The whole point of persisting the record: a new tab has no access token in
   session storage, but the refresh token on disk can mint one. Without this a
   reopened tab shows "Sign in" while holding a usable credential, starts a
   fresh authorization, and lands in a family with no entitlement -- which is
   the exact outcome persistence was added to prevent. */
async function resumeSession() {
  if (TOKEN) return true;
  const stored = loadSession();
  if (!stored || !stored.refresh) return false;
  SESSION = stored;
  REFRESH = stored.refresh;
  try {
    return await refreshTokens();
  } catch (err) {
    /* Settle, never reject. Both callers branch on the boolean, and a
       rejection at startup escapes before `render` -- leaving the operator
       with the initial markup, where the sign-in button, the sign-out button
       and the queues are all hidden. An inert page with no control on it is a
       worse outcome than any renewal failure.

       `refreshTokens` reaches the metadata endpoint before it discards the
       stored token, so a failure there leaves the record intact and a reload
       can retry. A failure at the token endpoint itself has already spent it,
       deliberately: a request whose response was lost may have rotated, and
       presenting it again would burn the family. */
    PENDING_ERROR =
      "Could not renew this session: " + err.message
      + ". Reload to try again, or sign in.";
    return false;
  }
}

let SESSION = loadSession() || migrateLegacySession() || {};
let TOKEN = sessionStorage.getItem(STORE.token) || null;
let REFRESH = SESSION.refresh || null;
let META = null;
/* Survives the clearMessages() at the top of render(): a callback error is the
   one message that must outlive the re-render that follows it. */
let PENDING_ERROR = null;
/* What the server says this credential is, including the operator's label for
   the authorization. Not persisted: it belongs to whichever token is in hand,
   it is one cheap request to re-ask, and a stale name in localStorage would
   outlive the session it described. */
let IDENTITY = null;
/* One authorization attempt owns registration and PKCE state at a time. The
   promise is shared by every caller so a second trigger cannot overwrite the
   verifier/state pair while the first redirect is being prepared. */
let SIGN_IN_ATTEMPT = null;


async function metadata() {
  if (META) return META;
  const r = await fetch("/.well-known/oauth-authorization-server");
  if (!r.ok) throw new Error("This deployment is not serving OAuth metadata (is VAULT_PUBLIC_URL set?)");
  META = await r.json();
  return META;
}

/* Registers once per persisted session, not once per tab. The record outlives
   the tab deliberately -- see the note on `loadSession` -- because the
   entitlement is keyed to the OAuth family and a lost session costs a manual
   grant.

   What must never happen is a registration outliving its refresh token:
   `prune_vault_oauth` deletes a stale registration, and the authorization
   server answers a deleted client_id with a direct 400 and no redirect
   (mcp/server/auth/handlers/authorize.py, attempt_load_client=False), so no
   callback runs and nothing can clean up after the fact. `loadSession`
   therefore discards the record on its own schedule rather than waiting for an
   error that never arrives. */
async function clientId() {
  if (SESSION.client) return SESSION.client;
  const m = await metadata();
  const redirect = window.location.origin + CFG.consolePath;
  const r = await fetch(m.registration_endpoint, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      client_name: CFG.clientName,
      redirect_uris: [redirect],
      grant_types: ["authorization_code", "refresh_token"],
      response_types: ["code"],
      token_endpoint_auth_method: "none",
      scope: CFG.scopes,
    }),
  });
  if (!r.ok) throw new Error("Client registration failed: " + (await r.text()).slice(0, 200));
  const id = (await r.json()).client_id;
  SESSION = { client: id, refresh: null, obtained: Date.now() };
  saveSession(SESSION);
  return id;
}

async function startSignIn() {
  const m = await metadata();
  const id = await clientId();
  const verifier = randomString();
  const state = randomString();
  sessionStorage.setItem(STORE.verifier, verifier);
  sessionStorage.setItem(STORE.state, state);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  const url = new URL(m.authorization_endpoint);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", id);
  url.searchParams.set("redirect_uri", window.location.origin + CFG.consolePath);
  url.searchParams.set("scope", CFG.scopes);
  url.searchParams.set("state", state);
  url.searchParams.set("code_challenge", b64url(digest));
  url.searchParams.set("code_challenge_method", "S256");
  window.location.assign(url.toString());
}

function signIn() {
  if (SIGN_IN_ATTEMPT) return SIGN_IN_ATTEMPT;
  const attempt = startSignIn();
  SIGN_IN_ATTEMPT = attempt;
  /* Observe both outcomes so cleanup creates no rejected side-promise. Keep
     the identity check: a future attempt must not be cleared by an older one. */
  attempt.then(
    () => { if (SIGN_IN_ATTEMPT === attempt) SIGN_IN_ATTEMPT = null; },
    () => { if (SIGN_IN_ATTEMPT === attempt) SIGN_IN_ATTEMPT = null; },
  );
  return attempt;
}

async function completeSignIn(code, state) {
  if (state !== sessionStorage.getItem(STORE.state)) {
    throw new Error("Authorization state did not match. Start again.");
  }
  const m = await metadata();
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: window.location.origin + CFG.consolePath,
    client_id: await clientId(),
    code_verifier: sessionStorage.getItem(STORE.verifier) || "",
  });
  const r = await fetch(m.token_endpoint, {
    method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body,
  });
  if (!r.ok) {
    const detail = (await r.text()).slice(0, 200);
    /* A registration deleted by `prune_vault_oauth` answers invalid_client
       forever, and the id is cached, so every later attempt repeats it. Drop
       it and the next sign-in registers afresh. */
    if (detail.includes("invalid_client")) {
      localStorage.removeItem(STORE.session);
      SESSION = {};
    }
    throw new Error("Token exchange failed: " + detail);
  }
  storeTokens(await r.json());
  sessionStorage.removeItem(STORE.verifier);
  sessionStorage.removeItem(STORE.state);
  window.history.replaceState({}, "", CFG.consolePath);
}

function storeTokens(payload) {
  TOKEN = payload.access_token;
  sessionStorage.setItem(STORE.token, TOKEN);
  /* Keeping the refresh token is what makes "grant the entitlement once"
     true. The access token lasts an hour; without a refresh, expiry would
     send the operator back through authorization, and a new authorization
     creates a new family that inherits no privileged scopes -- so the
     entitlement would have to be re-granted every hour, and abandoned
     families would pile up behind it. */
  if (payload.refresh_token) {
    REFRESH = payload.refresh_token;
    SESSION.refresh = REFRESH;
    /* Not restamped on rotation: `obtained` bounds the *registration*, which
       is what pruning removes, and rotating a token does not renew that. */
    SESSION.obtained = SESSION.obtained || Date.now();
    saveSession(SESSION);
  }
}

/* Rotating: the server mints a new refresh token and revokes the presented
   one, so the response must be stored or the chain is broken. A replayed
   refresh token revokes the whole family, which is why this never retries. */
/* Rotation must be serialized across tabs, and the reason is severe rather
   than tidy. Every tab copies the refresh token into memory, so two tabs both
   holding R1 will each present it: the second presentation is of a *consumed*
   token, which `VaultOAuthProvider.load_refresh_token` treats as a captured
   credential and answers by burning the entire family -- every credential ever
   minted in the chain. That is the correct response to theft and a catastrophic
   one to a second tab: any operator entitlement dies with the family and has to
   be granted again by hand, and a console holding only baseline scopes still
   loses its session mid-task.
   A Web Lock plus a re-read inside it means whichever tab wins presents the
   current token and the loser presents its replacement, never its predecessor.

   Two tabs still take each other's access tokens in turn, because rotation
   revokes the credential it replaces. That is chatty, and it is *not* harmless
   on its own: a tab whose token is revoked twice in a row must not conclude the
   session is over and delete the record the other tab is using. `api` retries
   while refreshes succeed and `endSession` clears only a record this tab still
   owns; those two together are what make the alternation survivable. */
async function withRefreshLock(work) {
  if (!navigator.locks || !navigator.locks.request) return work();
  return navigator.locks.request(CFG.storePrefix + ".refresh", work);
}

async function refreshTokens() {
  return withRefreshLock(async () => {
    /* Inside the lock, storage is the truth: another tab may have rotated
       while this one waited, and its own REFRESH is then a consumed token. */
    const current = loadSession();
    if (current && current.client) SESSION = current;
    REFRESH = SESSION.refresh || null;
    if (!REFRESH) return false;

    const m = await metadata();
    const presented = REFRESH;
    REFRESH = null;
    SESSION.refresh = null;
    saveSession(SESSION);
    const r = await fetch(m.token_endpoint, {
      method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "refresh_token",
        refresh_token: presented,
        client_id: SESSION.client,
      }),
    });
    if (!r.ok) return false;
    storeTokens(await r.json());
    return true;
  });
}

/* Ends the local session and repaints. `message` is carried through render()
   by PENDING_ERROR, so the reason survives the clearMessages() that render
   begins with. */
async function endSession(message, force) {
  TOKEN = null;
  REFRESH = null;
  IDENTITY = null;
  sessionStorage.removeItem(STORE.token);
  /* Only discard the shared record if this tab still owns it. Rotation revokes
     the access token it replaces, so a second tab's rotation can 401 this one
     twice over -- and clearing unconditionally would delete the refresh token
     that other tab is holding, ending a session that was alive and costing the
     entitlement with it. `force` is sign-out, where the family really is dead
     for everyone. */
  /* `refreshTokens` clears the stored token *before* presenting it, so that a
     lost response can never cause a replay -- which means a record whose
     refresh is null has already been spent and is dead for every tab. The
     record worth protecting is the opposite: one carrying a usable token,
     written by a tab that is not this one. */
  const stored = loadSession();
  const someoneElseAdvancedIt =
    stored && stored.refresh && SESSION.stamp && stored.stamp !== SESSION.stamp;
  if (!force && someoneElseAdvancedIt) {
    SESSION = stored;
    /* Their record is not just spared, it is used: they left a usable token,
       so this tab renews from it rather than signing out beside a live
       session. */
    if (await resumeSession()) {
      render();
      return;
    }
    if (message) PENDING_ERROR = message;
    render();
    return;
  }
  /* Forget the registration too. `prune_vault_oauth` deletes a registration
     once it is stale, and a registration is never stale while it holds a live
     refresh token -- so ending a session is exactly what makes ours eligible
     for deletion. A browser holding a deleted client_id cannot authorize
     again, and would have no way to notice: the id is cached indefinitely and
     every future attempt reuses it. Registering afresh costs one row, and the
     entitlement does not survive re-authorization anyway. */
  localStorage.removeItem(STORE.session);
  SESSION = {};
  if (message) PENDING_ERROR = message;
  render();
}

async function signOut() {
  /* Retire the family rather than abandoning it. A dropped refresh token stays
     valid until it expires, and the entitlement rides on the family. */
  const token = REFRESH || TOKEN;
  const client = SESSION.client;
  TOKEN = null;
  REFRESH = null;
  IDENTITY = null;
  sessionStorage.removeItem(STORE.token);
  try {
    const m = await metadata();
    /* The captured id, never `clientId()` -- that would register a brand new
       client here purely to name the one being revoked. */
    if (m.revocation_endpoint && token && client) {
      const revoked = await fetch(m.revocation_endpoint, {
        method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
        /* `client_secret` is empty and still has to be sent. The SDK's
           `RevocationRequest` declares it without a default, so a form that
           omits it fails validation with 400 before any token is loaded --
           and this console is a public client with no secret to put there.
           Without it, every sign-out since this endpoint existed answered 400
           and revoked nothing: the family was abandoned rather than retired,
           and its refresh token stayed live for its full thirty days. */
        body: new URLSearchParams({ token, client_id: client, client_secret: "" }),
      });
      /* Reported rather than ignored. The failure above was invisible for
         months precisely because nothing said anything, and a sign-out that
         silently leaves a live family is worth a line in the console even
         though there is nothing the operator can do about it here. */
      if (!revoked.ok) {
        console.warn("Vault sign-out could not revoke this session:", revoked.status);
      }
    }
  } catch (err) { /* Signing out locally must succeed even if revocation cannot. */ }
  await endSession(null, true);
}

/* `hssv1_<credential-id>_<secret>`. The console shows the id so the operator
   can name this exact family when granting the entitlement -- otherwise the
   command below is a scavenger hunt through `issue_vault_credential list`.

   `GET /authorization` states the id outright, so prefer it and parse only
   until it has loaded. The parse splits from the *right*, as the server and
   AGENTS.md do: a credential id may contain `_` while the secret is hex, so
   the last `_` is the unambiguous separator. Splitting from the left works on
   every id minted today and stops working the first time one carries an
   underscore -- silently, on the string the operator is told to copy. */
function credentialId() {
  if (IDENTITY && IDENTITY.credential_id) return IDENTITY.credential_id;
  if (!TOKEN) return null;
  const cut = TOKEN.lastIndexOf("_");
  if (cut < 0) return null;
  const head = TOKEN.slice(0, cut);
  const sep = head.indexOf("_");
  return sep < 0 ? null : head.slice(sep + 1) || null;
}

/* The label in place of the id, once there is one. `oauth-<uuid4>` and a hex
   credential id are exact and unreadable; the label is the operator's own name
   for this authorization (ADR 0040). The id remains in the entitlement command
   below, which is where it is actually copied from.

   Operator text, and unverified -- assigned through `textContent`, never
   `innerHTML`. */
function whoText() {
  if (!TOKEN) return "";
  if (IDENTITY && IDENTITY.label) return IDENTITY.label;
  return "credential " + (credentialId() || "?");
}

function paintWho() { $("who").textContent = whoText(); }

/* The header alone, so a failure here must not cost the operator their queue:
   it repaints what it learned and otherwise leaves the fallback standing. Not
   awaited by `loadAll` for the same reason -- the queue should not wait on a
   name. */
async function refreshIdentity() {
  try {
    IDENTITY = await api("/authorization");
  } catch (err) {
    /* `err.sessionEnded` included, deliberately. `endSession` has already
       dropped the token and repainted by the time it reaches here, and nothing
       follows this call to abandon -- while rethrowing out of a promise nobody
       awaits would only be an unhandled rejection. The header then paints as
       signed out, which is what it is. */
    IDENTITY = null;
  }
  paintWho();
}

/* ---------- API ---------- */
const MAX_REFRESH_RETRIES = 3;

async function api(path, options, attempt) {
  const tries = attempt || 0;
  const opts = Object.assign({ headers: {} }, options || {});
  opts.headers = Object.assign({ Authorization: "Bearer " + TOKEN }, opts.headers);
  const r = await fetch(CFG.apiBase + path, opts);
  if (r.status === 401) {
    /* Retry while a refresh actually succeeds, rather than once. A 401 is
       refused before the handler runs, so nothing was applied and the retry is
       safe -- and with two tabs open this tab's freshly-minted token can be
       revoked again by the other's rotation, so one attempt is not enough to
       distinguish "the session is over" from "the other tab moved first".
       Bounded, so a genuinely dead session still terminates. */
    if (tries < MAX_REFRESH_RETRIES && await refreshTokens()) {
      return api(path, options, tries + 1);
    }
    /* Repaint. Clearing the token without re-rendering left the page looking
       signed in -- app panel up, Sign in button hidden -- while telling the
       operator to sign in again, with no control to do it. This is the
       ordinary end of a thirty-day refresh token, not an edge case. */
    await endSession("Your session ended and could not be renewed. Sign in again.");
    const ended = new Error("session ended");
    ended.sessionEnded = true;
    throw ended;
  }
  if (r.status === 403) { const e = new Error("forbidden"); e.forbidden = true; throw e; }
  if (!r.ok) throw new Error((await r.text()).slice(0, 300));
  return r.json();
}

/* ---------- Messages ---------- */
function say(kind, node) {
  const box = el("div", kind === "danger" ? "notice danger" : "notice");
  box.appendChild(node);
  $("messages").appendChild(box);
}
function clearMessages() { $("messages").textContent = ""; }

/* ---------- Startup ---------- */
/* Named rather than inlined in the IIFE so the startup sequence is reachable
   as itself. Every bug found in this file so far hid in the difference between
   a function and the moment it runs, and an anonymous boot body can only be
   approximated by a test, never executed by one. */
async function boot() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("error")) {
    /* Any authorization failure casts doubt on the cached registration, and
       keeping a bad one is what makes the failure permanent. Re-registering is
       one row; being unable to sign in is a dead console. */
    localStorage.removeItem(STORE.session);
    SESSION = {};
    PENDING_ERROR = "Authorization failed: " + params.get("error")
      + ". The cached client registration was cleared; signing in will register again.";
    window.history.replaceState({}, "", CFG.consolePath);
  } else if (params.get("code")) {
    try { await completeSignIn(params.get("code"), params.get("state")); }
    catch (err) { PENDING_ERROR = err.message; }
  }
  /* Before the first paint, or the page flashes "Sign in" at an operator who
     is still signed in. In a `finally` because rendering is what gives them a
     control to act with; nothing above is worth a blank page. */
  try {
    await resumeSession();
  } finally {
    render();
  }
}
