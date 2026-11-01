// The one place AI-written text becomes HTML (docs/desktop-ui-plan.md §10, "Security"). Everything a worker, the
// planner or a report says goes through render(); nothing else in the app uses {@html}.
//
// - markdown-it with html:false: raw HTML in the text stays text.
// - DOMPurify with a short tag list and no images, SVG, styles, forms or frames; no class or event attributes.
// - Links: only http(s) survive, and only as data-href (no href), so a click can't navigate the app; the shell asks
//   before opening one in the browser (open_link in src-tauri).
// - Bidirectional overrides and other invisible direction marks are removed, so text reads as it is.

import MarkdownIt from "markdown-it";
import DOMPurify from "dompurify";

const md = new MarkdownIt({ html: false, linkify: true, typographer: false });

const DIRECTION_MARKS = /[‎‏؜‪-‮⁦-⁩]/g;
// C0 controls other than tab and line breaks, and DEL.
const CONTROLS = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g;

const TAGS = ["p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "strong", "em", "del", "s",
  "code", "pre", "blockquote", "a", "table", "thead", "tbody", "tr", "th", "td"];
const ATTRS = ["href", "title", "start"];

// Terminal escape sequences (colours, cursor moves, titles, links): the terminal chat stores its answers styled.
const ESCAPES = /\u001b\[[0-?]*[ -/]*[@-~]|\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)|\u001b[@-_]/g;

/** Text written for a terminal (a terminal chat answer), as plain text. */
export function terminalText(text) {
  return plain(String(text ?? "").replace(ESCAPES, ""));
}

/** Plain text as it should be shown anywhere in the app: no direction overrides, no control characters. */
export function plain(text) {
  return String(text ?? "").replace(DIRECTION_MARKS, "").replace(CONTROLS, "");
}

/** An http(s) URL, or "" for anything else (javascript:, file:, data:, relative paths, …). */
export function safeUrl(href) {
  const value = String(href ?? "").trim();
  if (!/^https?:\/\//i.test(value)) return "";
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

let purifier = null;

function purify() {
  if (purifier) return purifier;
  purifier = DOMPurify(window);
  purifier.addHook("afterSanitizeAttributes", (node) => {
    if (node.nodeName !== "A") return;
    const url = safeUrl(node.getAttribute("href"));
    node.removeAttribute("href");
    if (url) {
      node.setAttribute("data-href", url);
      node.setAttribute("title", url);
      node.setAttribute("tabindex", "0");
      node.setAttribute("role", "link");
    }
  });
  return purifier;
}

/** Markdown from an AI or a report, as sanitised HTML. */
export function render(text) {
  return purify().sanitize(md.render(plain(text)), {
    ALLOWED_TAGS: TAGS,
    ALLOWED_ATTR: ATTRS,
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
    KEEP_CONTENT: true,
  });
}
