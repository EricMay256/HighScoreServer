/* Runs a console page's script against a stub DOM and reports what it did.
 *
 * The console tests were string matches over the rendered page. They pin that
 * a line of code exists, which is worth something -- a deleted guard shows up
 * -- and worth nothing against a line that exists and is wrong. An inverted
 * `pristine` assignment passed every one of them while doing the opposite of
 * what its name says.
 *
 * So: evaluate the real script and drive the real handlers. Not a browser and
 * not trying to be one. The stubs implement the handful of DOM operations
 * `el()` and the form actually use -- appendChild, textContent, value,
 * disabled -- and nothing else, which is enough to observe state transitions
 * and payloads, and honest about being enough for no more than that.
 *
 * Usage: node console_harness.js <script.js>   ->   JSON report on stdout
 */

const fs = require("fs");

function makeNode(tag) {
  let ownText = "";
  const node = {
    tagName: tag,
    id: "",
    className: "",
    value: "",
    disabled: false,
    title: "",
    type: "",
    rows: 0,
    step: "",
    min: "",
    max: "",
    placeholder: "",
    htmlFor: "",
    children: [],
    onclick: null,
    oninput: null,
    style: {},
    dataset: {},
    parentNode: null,
    classList: {
      add() {},
      remove() {},
      toggle() {},
      contains() {
        return false;
      },
    },
    appendChild(child) {
      child.parentNode = node;
      node.children.push(child);
      return child;
    },
    append(...kids) {
      kids.forEach((kid) => {
        kid.parentNode = node;
        node.children.push(kid);
      });
    },
    replaceChildren(...kids) {
      node.children.forEach((kid) => { kid.parentNode = null; });
      kids.forEach((kid) => { kid.parentNode = node; });
      node.children = kids;
    },
    remove() {
      if (!node.parentNode) return;
      node.parentNode.children = node.parentNode.children.filter(
        (child) => child !== node,
      );
      node.parentNode = null;
    },
    focus() {},
    setAttribute() {},
    addEventListener() {},
    dispatchEvent() {},
  };
  Object.defineProperty(node, "textContent", {
    get() {
      if (node.children.length) {
        return node.children.map((child) => child.textContent).join("");
      }
      return ownText;
    },
    set(value) {
      ownText = String(value);
      node.children.forEach((child) => { child.parentNode = null; });
      node.children = [];
    },
  });
  return node;
}

/* Every id resolves to a node, created on first ask. The page looks up
 * elements it rendered into markup; the harness has no markup, and inventing
 * one node per id is closer to the truth than failing. */
const byId = new Map();
function descendantById(node, id) {
  for (const child of node.children || []) {
    if (child.id === id) return child;
    const nested = descendantById(child, id);
    if (nested) return nested;
  }
  return null;
}

function elementById(id) {
  for (const root of byId.values()) {
    const dynamic = descendantById(root, id);
    if (dynamic) {
      byId.set(id, dynamic);
      return dynamic;
    }
  }
  if (!byId.has(id)) {
    const node = makeNode("div");
    node.id = id;
    byId.set(id, node);
  }
  return byId.get(id);
}

const fetchCalls = [];
let fetchHandler = async () => ({
  ok: true,
  status: 200,
  json: async () => ({ proposal: { proposal_id: "proposal-1" } }),
  text: async () => "",
});
const documentStub = {
  getElementById: elementById,
  createElement: makeNode,
  querySelector: () => null,
  querySelectorAll: () => [],
  body: makeNode("body"),
};
/* The config block is markup in the page, so it is answered here rather than
 * created: the script parses it at its first line. */
const cfg = makeNode("script");
cfg.textContent = JSON.stringify({
  apiBase: "/api/v1/vault",
  scopes: "vault:read vault:propose",
  consolePath: "/vault/browse",
  clientName: "Vault browse console",
  storePrefix: "vault.browse",
});
byId.set("cfg", cfg);

function storageStub() {
  const items = new Map();
  const setCalls = [];
  return {
    getItem: (key) => (items.has(key) ? items.get(key) : null),
    setItem: (key, value) => {
      setCalls.push({ key, value: String(value) });
      items.set(key, String(value));
    },
    removeItem: (key) => items.delete(key),
    clear: () => items.clear(),
    setCalls,
  };
}

const navigationCalls = [];
const globals = {
  document: documentStub,
  window: {
    location: {
      origin: "http://localhost:8000",
      search: "",
      assign: (url) => navigationCalls.push(url),
    },
    history: { replaceState() {} },
    getSelection: () => null,
  },
  localStorage: storageStub(),
  sessionStorage: storageStub(),
  navigator: { locks: null },
  crypto: {
    getRandomValues: (array) => array.fill(7),
    subtle: { digest: async () => new Uint8Array(32) },
  },
  btoa: (value) => Buffer.from(value, "binary").toString("base64"),
  TextEncoder,
  URLSearchParams,
  URL,
  console,
  fetch: async (url, options) => {
    fetchCalls.push({ url, options });
    return fetchHandler(url, options);
  },
};

const source = fs
  .readFileSync(process.argv[2], "utf8")
  /* `boot()` starts a sign-in; the harness is here for the form, not the
   * startup path, which the session-module tests cover. */
  .replace(/\nboot\(\);\s*$/, "\n");

const exported = `
return {
  renderListing,
  openNote,
  signIn,
  proposeForm,
  spanFromLines,
  occurrenceOf,
  setFilters: (filters) => { FILTERS = filters; },
  setNote: (note) => { NOTE = note; },
  setIdentity: (identity) => { IDENTITY = identity; },
  browseState: () => ({
    cursor: COMMITTED_LISTING ? COMMITTED_LISTING.cursor : null,
    query: COMMITTED_LISTING ? COMMITTED_LISTING.query : null,
    rows: ROWS.map((row) => row.title),
    note: NOTE ? NOTE.title : null,
  }),
};
`;

const load = new Function(...Object.keys(globals), source + exported);
const page = load(...Object.values(globals));

/* ---------- driving the form ---------- */

const BODY = [
  "First line, untouched.",
  "Second line, the one to reword.",
  "Third line, untouched.",
  "",
].join("\n");

function find(node, predicate) {
  if (predicate(node)) return node;
  for (const child of node.children || []) {
    const hit = find(child, predicate);
    if (hit) return hit;
  }
  return null;
}

function openForm(initialSpan = null) {
  page.setNote({
    note_id: "harness-note",
    kind: "note",
    body: BODY,
    content_revision: 4,
  });
  page.setIdentity({ scopes: ["vault:read", "vault:propose"] });

  const form = page.proposeForm(initialSpan || page.spanFromLines(1, 1));
  const byNodeId = (id) => find(form, (node) => node.id === id);
  return {
    form,
    from: byNodeId("propose-from"),
    to: byNodeId("propose-to"),
    replacement: byNodeId("propose-replacement"),
    rationale: byNodeId("propose-rationale"),
    quoted: find(form, (node) => node.className === "excerpt"),
    submit: find(form, (node) => node.textContent === "Propose"),
  };
}

function retarget(parts, from, to) {
  parts.from.value = String(from);
  parts.to.value = String(to);
  parts.from.oninput();
}

const report = {};

function jsonResponse(payload) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
    text: async () => "",
  };
}

function failedResponse(message) {
  return {
    ok: false,
    status: 500,
    json: async () => ({}),
    text: async () => message,
  };
}

function deferredResponse() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {
    promise,
    succeed(payload) { resolve(jsonResponse(payload)); },
  };
}

function listingPage(title, cursor = null, hasMore = false) {
  return {
    notes: [{
      note_id: title.toLowerCase().replaceAll(" ", "-"),
      title,
      kind: "note",
      status: "active",
      vault_path: "Agent/notes/" + title.toLowerCase().replaceAll(" ", "-") + ".md",
      doc_status: null,
      summary: null,
    }],
    next_cursor: cursor,
    has_more: hasMore,
  };
}

function noteDetail(title) {
  return {
    note_id: title.toLowerCase().replaceAll(" ", "-"),
    title,
    kind: "note",
    status: "active",
    vault_path: "Agent/notes/" + title.toLowerCase().replaceAll(" ", "-") + ".md",
    content_revision: 1,
    doc_type: null,
    doc_status: null,
    summary: null,
    tags: [],
    facets: {},
    related_ids: [],
    source_ids: [],
    body: title + " body",
  };
}

// 1. An edited replacement survives re-aiming the range.
{
  const parts = openForm();
  parts.replacement.value = "A sentence the operator wrote.";
  parts.replacement.oninput();
  retarget(parts, 2, 2);
  report.editedReplacementSurvivesRetarget = {
    replacement: parts.replacement.value,
    quoted: parts.quoted.textContent,
  };
}

// 2. An untouched replacement is reseeded from the new span.
{
  const parts = openForm();
  retarget(parts, 2, 2);
  report.untouchedReplacementFollowsTheRange = {
    replacement: parts.replacement.value,
  };
}

// 3. Restoring the original text makes the field pristine again.
{
  const parts = openForm();
  parts.replacement.value = "something else";
  parts.replacement.oninput();
  parts.replacement.value = "First line, untouched.";
  parts.replacement.oninput();
  retarget(parts, 3, 3);
  report.restoringTheTextResumesReseeding = {
    replacement: parts.replacement.value,
  };
}

// 4. An impossible range disables submission and submits nothing.
{
  const parts = openForm();
  retarget(parts, 1, 99);
  const disabledAfterBadRange = parts.submit.disabled;
  fetchCalls.length = 0;
  parts.rationale.value = "should not be sent";
  parts.submit.onclick();
  report.invalidRangeRefuses = {
    submitDisabled: disabledAfterBadRange,
    quoted: parts.quoted.textContent,
    requests: fetchCalls.length,
  };
}

// 5. Editing while invalid survives restoring a valid range.
{
  const parts = openForm();
  retarget(parts, 1, 99);
  parts.replacement.value = "An edit made while the range was invalid.";
  parts.replacement.oninput();
  retarget(parts, 2, 2);
  report.invalidRangeEditSurvivesRecovery = {
    replacement: parts.replacement.value,
    submitEnabled: parts.submit.disabled === false,
  };
}

// 6. A valid range re-enables submission and posts the displayed span.
{
  const parts = openForm();
  retarget(parts, 1, 99);
  retarget(parts, 2, 2);
  const enabledAgain = parts.submit.disabled === false;
  fetchCalls.length = 0;
  parts.replacement.value = "Second line, reworded.";
  parts.replacement.oninput();
  parts.rationale.value = "Reword the second line.";
  parts.submit.onclick();
  const posted = fetchCalls[0];
  report.validRangePostsWhatIsShown = {
    submitEnabled: enabledAgain,
    url: posted ? posted.url : null,
    body: posted ? JSON.parse(posted.options.body) : null,
  };
}

// 7. An unretargeted partial-line selection is submitted exactly as selected.
{
  const expected = "the one to reword";
  const parts = openForm({ start: BODY.indexOf(expected), expected });
  fetchCalls.length = 0;
  parts.replacement.value = "the phrase the operator revised";
  parts.replacement.oninput();
  parts.rationale.value = "Revise only the selected phrase.";
  parts.submit.onclick();
  const posted = fetchCalls[0];
  report.partialLineSelectionStaysExact = {
    body: posted ? JSON.parse(posted.options.body) : null,
  };
}

// 8. Fractional line numbers never construct a span.
{
  const parts = openForm();
  retarget(parts, 1.5, 2);
  fetchCalls.length = 0;
  parts.rationale.value = "should not be sent";
  parts.submit.onclick();
  report.fractionalRangeRefuses = {
    submitDisabled: parts.submit.disabled,
    requests: fetchCalls.length,
  };
}

async function runNavigationCases() {
  // 9. A slower old listing cannot replace a newer filter result.
  {
    const oldResponse = deferredResponse();
    const newResponse = deferredResponse();
    const responses = [oldResponse, newResponse];
    fetchHandler = () => responses.shift().promise;

    page.setFilters({ tag: "old", facet: "" });
    const oldRequest = page.renderListing(false);
    page.setFilters({ tag: "new", facet: "" });
    const newRequest = page.renderListing(false);

    newResponse.succeed(listingPage("New listing", "new-cursor"));
    await newRequest;
    oldResponse.succeed(listingPage("Old listing", "old-cursor"));
    await oldRequest;
    report.reversedListingsKeepNewest = page.browseState();
  }

  // 10. A slower old note cannot replace the newer note navigation.
  {
    const oldResponse = deferredResponse();
    const newResponse = deferredResponse();
    const responses = [oldResponse, newResponse];
    fetchHandler = () => responses.shift().promise;

    const oldRequest = page.openNote("old-note");
    const newRequest = page.openNote("new-note");
    newResponse.succeed(noteDetail("New note"));
    await newRequest;
    oldResponse.succeed(noteDetail("Old note"));
    await oldRequest;
    report.reversedNotesKeepNewest = page.browseState();
  }

  // 11. Failed pagination remains retryable and keeps accumulated rows.
  {
    page.setFilters({ tag: "kept", facet: "" });
    fetchHandler = async () => jsonResponse(
      listingPage("Kept listing", "kept-cursor", true),
    );
    await page.renderListing(false);
    const listing = elementById("listing");
    const more = find(listing, (node) => node.id === "more");

    fetchHandler = async () => failedResponse("temporary pagination failure");
    await more.onclick();
    report.failedPaginationKeepsListing = {
      ...page.browseState(),
      retryEnabled: more.disabled === false,
      rowVisible: Boolean(find(
        listing,
        (node) => node.tagName === "button" && node.textContent === "Kept listing",
      )),
    };

    // 12. A failed fresh request preserves that same listing and control.
    page.setFilters({ tag: "replacement", facet: "" });
    try {
      await page.renderListing(false);
    } catch (err) {
      // Expected: this case observes what survived the failed replacement.
    }
    report.failedRefreshKeepsListing = {
      ...page.browseState(),
      loadMoreVisible: more.parentNode === listing,
      rowVisible: Boolean(find(
        listing,
        (node) => node.tagName === "button" && node.textContent === "Kept listing",
      )),
    };

    // 13. That preserved button still owns the committed query and cursor.
    fetchCalls.length = 0;
    fetchHandler = async () => jsonResponse(
      listingPage("Appended listing", "appended-cursor", false),
    );
    await more.onclick();
    report.preservedPaginationUsesCommittedQuery = {
      ...page.browseState(),
      url: fetchCalls[0] ? fetchCalls[0].url : null,
    };
  }

  // 14. Opening a note invalidates pagination already in flight.
  {
    page.setFilters({ tag: "before-note", facet: "" });
    fetchHandler = async () => jsonResponse(
      listingPage("Listing before note", "before-note-cursor", true),
    );
    await page.renderListing(false);
    const more = find(elementById("listing"), (node) => node.id === "more");
    const appendResponse = deferredResponse();
    const noteResponse = deferredResponse();
    const responses = [appendResponse, noteResponse];
    fetchHandler = () => responses.shift().promise;

    const appendRequest = more.onclick();
    const noteRequest = page.openNote("newer-navigation");
    noteResponse.succeed(noteDetail("Newer navigation"));
    await noteRequest;
    appendResponse.succeed(listingPage("Stale appended listing"));
    await appendRequest;
    report.noteNavigationInvalidatesPagination = page.browseState();
  }

  // 15. Concurrent sign-in callers share registration and PKCE state.
  {
    const metadataResponse = deferredResponse();
    fetchCalls.length = 0;
    navigationCalls.length = 0;
    globals.sessionStorage.setCalls.length = 0;
    fetchHandler = (url) => {
      if (url === "/.well-known/oauth-authorization-server") {
        return metadataResponse.promise;
      }
      if (url === "https://auth.test/register") {
        return Promise.resolve(jsonResponse({ client_id: "client-1" }));
      }
      throw new Error("Unexpected sign-in URL: " + url);
    };

    const first = page.signIn();
    const second = page.signIn();
    metadataResponse.succeed({
      registration_endpoint: "https://auth.test/register",
      authorization_endpoint: "https://auth.test/authorize",
    });
    await Promise.all([first, second]);

    report.concurrentSignInIsSingleFlight = {
      sharedPromise: first === second,
      registrations: fetchCalls.filter(
        (call) => call.url === "https://auth.test/register",
      ).length,
      verifierWrites: globals.sessionStorage.setCalls.filter(
        (call) => call.key === "vault.browse.pkce",
      ).length,
      stateWrites: globals.sessionStorage.setCalls.filter(
        (call) => call.key === "vault.browse.state",
      ).length,
      navigations: navigationCalls.length,
    };
  }
}

runNavigationCases()
  .then(() => process.stdout.write(JSON.stringify(report, null, 2)))
  .catch((err) => {
    console.error(err);
    process.exitCode = 1;
  });
