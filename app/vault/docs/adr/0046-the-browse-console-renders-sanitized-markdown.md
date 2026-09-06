# 0046. The browse console renders sanitized Markdown from pinned local assets

Date: 2026-09-06

## Status

Accepted. This supersedes ADR 0039's source-only body presentation. Its
authorization split and span-edit decisions are unchanged.

## Context

ADR 0039 displayed every note body as Markdown source. That was the safe choice
while the console had no Markdown implementation: fetching one from a CDN would
contradict its same-origin CSP, and writing a partial renderer would turn
agent-authored text into markup through code with no credible security boundary.

The source view remains useful for exact-span selection and for inspecting what
the corpus actually stores. It is a poor default reading surface for headings,
lists, tables, links and code blocks, however. The full-body editor also makes
source directly available when a reader intends to author rather than read, so
the note view no longer has to make every reader pay that cost.

Markdown parsing does not itself make HTML safe. Marked deliberately passes raw
HTML through and explicitly requires its callers to sanitize the generated
markup. Note bodies are agent-authored, so the output remains untrusted even on
a first-party console.

## Decision

The browse console offers **Rendered** and **Source** body modes and opens each
note in Rendered mode. Source is the exact body returned by `GET /notes/{id}`;
selection editing continues to address that source node and proposal payloads
continue to use the separately held `NOTE.body` and `content_revision`.

Rendered mode uses two pinned browser distributions:

- Marked 18.0.11 under the MIT license;
- DOMPurify 3.4.15 under its Apache-2.0 option.

Their published bundles and license texts live below `app/vault/static/vendor/`
and are served at `/vault/assets/` by a package-owned `StaticFiles` mount. The
mount is returned with the browse-console routes, so it is registered only when
the vault's public OAuth and console surfaces are registered. No CDN, npm
runtime, package manifest or browser build step is introduced.

The rendering pipeline is fixed in this order:

1. Marked parses the body as GFM with asynchronous parsing and hard line breaks
   disabled.
2. DOMPurify sanitizes that HTML against an explicit Markdown element and
   attribute allowlist. ARIA and data attributes are disabled.
3. DOMPurify returns a `DocumentFragment`, which is inserted with
   `appendChild`. Marked's string never reaches an application-owned
   `innerHTML` assignment.

The allowlist admits ordinary Markdown structure and GFM tables. It does not
admit scripts, styles, form controls, embedded browsing contexts or arbitrary
attributes. DOMPurify still owns URL-scheme validation,
and the page CSP continues to constrain images and every other fetch to the
same origin (with the existing `data:` image exception).

The external script tags carry the response nonce even though `script-src
'self'` already admits them. This keeps the page's invariant simple: every
script tag emitted by the template names the response nonce, while the bundles
remain same-origin resources.

## Consequences

The package gains roughly 74 KiB of reviewed JavaScript and two third-party
license files. Version updates are explicit repository changes. Tests pin both
the version banners and SHA-256 digests, so a changed bundle cannot pass as the
same reviewed version.

Rendered Markdown is presentation only. It does not change the API response,
stored body, amendment shape, review artifact, MCP surface or authorization
model. No migration or Python dependency is required.

The console now owns a browser security maintenance task: Marked and DOMPurify
must be reviewed and updated when their upstream projects publish relevant
fixes. Self-hosting removes availability and supply changes at request time; it
does not make a pinned sanitizer permanently current.

The exact source remains one click away and is the only body node from which a
mouse selection becomes a span edit. A reader who selects rendered text must
switch to Source before proposing that exact selection, because rendered DOM
offsets do not map byte-for-byte to Markdown source.

## Alternatives considered

**Keep source as the only view.** Safe and dependency-free, but leaves the
primary human reading surface optimized for authoring. Rejected now that the
dependencies and sanitization boundary are explicit.

**Render on the server.** This would add a Python Markdown implementation and
an HTML sanitizer, then ship already-rendered HTML through an API that currently
returns corpus data. It also makes every non-browser client pay for a browser
presentation concern. Rejected in favor of keeping the API representation
unchanged.

**Load Marked and DOMPurify from a CDN.** Smaller repository, but it requires
loosening the same-origin CSP and makes the console's core reading path depend
on a third-party origin at request time. Rejected.

**Implement a small Markdown subset locally.** The apparent dependency saving
is the dangerous part: raw HTML, malformed nesting, URL schemes and browser
parser correction are the boundary, not headings and emphasis. Rejected for the
same reason ADR 0039 rejected hand-rolled rendering.
