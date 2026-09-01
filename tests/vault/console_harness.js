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
  const node = {
    tagName: tag,
    id: "",
    className: "",
    textContent: "",
    value: "",
    disabled: false,
    title: "",
    type: "",
    rows: 0,
    min: "",
    max: "",
    placeholder: "",
    htmlFor: "",
    children: [],
    onclick: null,
    oninput: null,
    style: {},
    dataset: {},
    classList: {
      add() {},
      remove() {},
      toggle() {},
      contains() {
        return false;
      },
    },
    appendChild(child) {
      node.children.push(child);
      return child;
    },
    append(...kids) {
      kids.forEach((kid) => node.children.push(kid));
    },
    replaceChildren(...kids) {
      node.children = kids;
    },
    remove() {},
    focus() {},
    setAttribute() {},
    addEventListener() {},
    dispatchEvent() {},
  };
  return node;
}

/* Every id resolves to a node, created on first ask. The page looks up
 * elements it rendered into markup; the harness has no markup, and inventing
 * one node per id is closer to the truth than failing. */
const byId = new Map();
function elementById(id) {
  if (!byId.has(id)) {
    const node = makeNode("div");
    node.id = id;
    byId.set(id, node);
  }
  return byId.get(id);
}

const fetchCalls = [];
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
  return {
    getItem: (key) => (items.has(key) ? items.get(key) : null),
    setItem: (key, value) => items.set(key, String(value)),
    removeItem: (key) => items.delete(key),
    clear: () => items.clear(),
  };
}

const globals = {
  document: documentStub,
  window: {
    location: { origin: "http://localhost:8000", search: "" },
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
    return {
      ok: true,
      status: 200,
      json: async () => ({ proposal: { proposal_id: "proposal-1" } }),
      text: async () => "",
    };
  },
};

const source = fs
  .readFileSync(process.argv[2], "utf8")
  /* `boot()` starts a sign-in; the harness is here for the form, not the
   * startup path, which the session-module tests cover. */
  .replace(/\nboot\(\);\s*$/, "\n");

const exported = `
return {
  proposeForm,
  spanFromLines,
  occurrenceOf,
  setNote: (note) => { NOTE = note; },
  setIdentity: (identity) => { IDENTITY = identity; },
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

function openForm() {
  page.setNote({
    note_id: "harness-note",
    kind: "note",
    body: BODY,
    content_revision: 4,
  });
  page.setIdentity({ scopes: ["vault:read", "vault:propose"] });

  const form = page.proposeForm(page.spanFromLines(1, 1));
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

process.stdout.write(JSON.stringify(report, null, 2));
