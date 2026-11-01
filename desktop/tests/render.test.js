// XSS fixtures for the one component that turns AI text into HTML (src/lib/render.js; desktop-ui-plan §10).
import { describe, expect, it } from "vitest";
import { plain, render, safeUrl } from "../src/lib/render.js";

function dom(html) {
  const t = document.createElement("template");
  t.innerHTML = html;
  return t.content;
}

function assertInert(html) {
  const root = dom(html);
  for (const el of root.querySelectorAll("*")) {
    for (const attr of el.attributes) {
      expect(attr.name.startsWith("on"), `${el.tagName} has ${attr.name}`).toBe(false);
      expect(["href", "src", "style", "class", "srcset", "action", "formaction", "xlink:href"]).not.toContain(attr.name);
    }
  }
  for (const tag of ["script", "img", "svg", "iframe", "object", "embed", "style", "form", "input", "math", "video", "audio", "link", "meta", "base"]) {
    expect(root.querySelector(tag), tag).toBeNull();
  }
  for (const a of root.querySelectorAll("[data-href]")) {
    expect(a.getAttribute("data-href")).toMatch(/^https?:\/\//);
  }
  return root;
}

const FIXTURES = {
  "img onerror": "<img src=x onerror=alert(1)>",
  "javascript link": "[click](javascript:alert(1))",
  "mixed-case javascript link": "[click](JaVaScRiPt:alert(1))",
  "entity-encoded javascript link": "[click](&#106;avascript:alert(1))",
  "javascript autolink": "<javascript:alert(1)>",
  "file link": "[passwd](file:///etc/passwd)",
  "data link": "[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
  "vbscript link": "[x](vbscript:msgbox(1))",
  "svg": "<svg onload=alert(1)><circle r=4 /></svg>",
  "raw script block": "<script>alert(1)</script>",
  "raw html block": "<div style=\"position:fixed;inset:0\" onclick=\"alert(1)\">cover</div>",
  "iframe": "<iframe src=\"https://evil.example\"></iframe>",
  "markdown image": "![pixel](https://tracker.example/p.png)",
  "image with javascript": "![x](javascript:alert(1))",
  "link title injection": "[x](https://ok.example \"\\\" onmouseover=alert(1) x=\\\"\")",
  "html inside a table": "| a |\n|---|\n| <img src=x onerror=alert(1)> |",
  "reference link": "[x][1]\n\n[1]: javascript:alert(1)",
};

describe("render", () => {
  for (const [name, text] of Object.entries(FIXTURES)) {
    it(`keeps ${name} inert`, () => {
      assertInert(render(text));
    });
  }

  it("shows raw HTML as text", () => {
    const root = assertInert(render("<b>bold</b> <script>alert(1)</script>"));
    expect(root.textContent).toContain("<script>alert(1)</script>");
  });

  it("turns http(s) links into data-href without href", () => {
    const root = assertInert(render("See [docs](https://example.com/a?b=1) and https://example.org"));
    const links = [...root.querySelectorAll("a")];
    expect(links.map((a) => a.getAttribute("data-href"))).toEqual(["https://example.com/a?b=1", "https://example.org/"]);
    expect(links.every((a) => !a.hasAttribute("href"))).toBe(true);
  });

  it("drops dangerous links but keeps their text", () => {
    const root = assertInert(render("[keep this text](javascript:alert(1))"));
    expect(root.textContent).toContain("keep this text");
    expect(root.querySelector("[data-href]")).toBeNull();
  });

  it("removes bidirectional overrides", () => {
    const text = "invoice‮txt.exe and ⁦isolate⁩";
    expect(render(text)).not.toMatch(/[‪-‮⁦-⁩]/);
    expect(plain(text)).toBe("invoicetxt.exe and isolate");
  });

  it("keeps ordinary markdown", () => {
    const root = assertInert(render("# Title\n\n- **one**\n- `two`\n\n```\ncode\n```"));
    expect(root.querySelector("h1").textContent).toBe("Title");
    expect(root.querySelectorAll("li")).toHaveLength(2);
    expect(root.querySelector("pre code").textContent).toBe("code\n");
  });
});

describe("safeUrl", () => {
  it("accepts http(s) only", () => {
    expect(safeUrl("https://a.example/x")).toBe("https://a.example/x");
    expect(safeUrl("HTTP://a.example")).toBe("http://a.example/");
    for (const bad of ["javascript:alert(1)", "file:///etc/passwd", "/relative", "//host/x", "ftp://x", "data:,x", "", null]) {
      expect(safeUrl(bad), String(bad)).toBe("");
    }
  });
});
