"""Render `docs/Implementation.md` as a single self-contained HTML page.

Run from the repository root: `python3 scripts/render_implementation_html.py`.
`docs/Implementation.html` is generated; edit the markdown, never the HTML.
"""
import re
from pathlib import Path

md = Path("docs/Implementation.md").read_text()

TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CloudServe Support System — Implementation</title>
<style>
:root {
  --bg:#0d1117; --panel:#161b22; --panel-2:#1c2128; --line:#30363d;
  --ink:#e6edf3; --ink-dim:#9198a1; --accent:#58a6ff; --accent-2:#3fb950;
  --warn:#d29922; --danger:#f85149; --code-bg:#0b0f14;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Helvetica,Arial,sans-serif;
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    --bg:#ffffff; --panel:#f6f8fa; --panel-2:#eef1f4; --line:#d0d7de;
    --ink:#1f2328; --ink-dim:#59636e; --accent:#0969da; --accent-2:#1a7f37;
    --warn:#9a6700; --danger:#cf222e; --code-bg:#f6f8fa;
  }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font-family:var(--sans);
       font-size:16px; line-height:1.65; -webkit-font-smoothing:antialiased; }
#layout { display:flex; align-items:flex-start; }
#toc { position:sticky; top:0; width:290px; flex:0 0 290px; height:100vh; overflow-y:auto;
       border-right:1px solid var(--line); background:var(--panel); padding:22px 16px 60px; }
#toc h2 { font-size:11px; letter-spacing:.14em; text-transform:uppercase; color:var(--ink-dim);
          margin:0 0 12px; font-weight:600; }
#toc a { display:block; color:var(--ink-dim); text-decoration:none; font-size:13px;
         padding:4px 8px; border-radius:6px; border-left:2px solid transparent; }
#toc a:hover { color:var(--ink); background:var(--panel-2); }
#toc a.lvl3 { padding-left:20px; font-size:12.5px; }
#toc a.active { color:var(--accent); border-left-color:var(--accent); background:var(--panel-2); }
main { flex:1; min-width:0; max-width:1000px; padding:48px 40px 140px; margin:0 auto; }
h1 { font-size:34px; line-height:1.25; margin:0 0 6px; letter-spacing:-.02em; }
h2 { font-size:25px; margin:56px 0 14px; padding-bottom:9px; border-bottom:1px solid var(--line);
     letter-spacing:-.015em; scroll-margin-top:16px; }
h3 { font-size:19px; margin:36px 0 10px; color:var(--accent); scroll-margin-top:16px; }
h4 { font-size:16px; margin:24px 0 8px; }
p, li { color:var(--ink); }
a { color:var(--accent); }
hr { border:0; border-top:1px solid var(--line); margin:44px 0; }
code { font-family:var(--mono); font-size:13px; background:var(--panel-2);
       padding:.15em .38em; border-radius:5px; }
pre { background:var(--code-bg); border:1px solid var(--line); border-radius:9px;
      padding:15px 17px; overflow-x:auto; font-size:13px; line-height:1.55; }
pre code { background:none; padding:0; font-size:13px; }
table { border-collapse:collapse; width:100%; margin:18px 0; font-size:14px; display:block;
        overflow-x:auto; }
th, td { border:1px solid var(--line); padding:8px 11px; text-align:left; vertical-align:top; }
th { background:var(--panel-2); font-weight:600; white-space:nowrap; }
tr:nth-child(even) td { background:color-mix(in srgb, var(--panel) 55%, transparent); }
blockquote { margin:20px 0; padding:13px 18px; border-left:3px solid var(--warn);
             background:color-mix(in srgb, var(--warn) 9%, transparent); border-radius:0 8px 8px 0; }
blockquote p { margin:.4em 0; }
.mermaid { background:var(--panel); border:1px solid var(--line); border-radius:10px;
           padding:18px; margin:22px 0; text-align:center; overflow-x:auto; }
#banner { background:linear-gradient(135deg,
          color-mix(in srgb, var(--accent) 14%, transparent), transparent);
          border:1px solid var(--line); border-radius:12px; padding:20px 24px; margin-bottom:8px; }
#banner .tag { display:inline-block; font-family:var(--mono); font-size:11px;
               letter-spacing:.08em; text-transform:uppercase; color:var(--accent);
               border:1px solid var(--accent); border-radius:999px; padding:2px 10px;
               margin-bottom:10px; }
#banner .stats { display:flex; flex-wrap:wrap; gap:22px; margin-top:14px; }
#banner .stat b { display:block; font-size:22px; font-family:var(--mono); color:var(--ink); }
#banner .stat span { font-size:11.5px; color:var(--ink-dim); letter-spacing:.04em;
                     text-transform:uppercase; }
#burger { display:none; position:fixed; top:12px; left:12px; z-index:30; background:var(--panel);
          color:var(--ink); border:1px solid var(--line); border-radius:8px; padding:8px 12px;
          font-size:14px; cursor:pointer; }
@media (max-width:900px) {
  #toc { position:fixed; left:0; top:0; z-index:20; transform:translateX(-100%);
         transition:transform .2s ease; }
  #toc.open { transform:translateX(0); }
  #burger { display:block; }
  main { padding:64px 16px 120px; }
  h1 { font-size:26px; }
}
</style>
</head>
<body>
<button id="burger" aria-label="Toggle contents">&#9776; Contents</button>
<div id="layout">
  <nav id="toc"><h2>Contents</h2><div id="toc-links"></div></nav>
  <main>
    <div id="banner">
      <span class="tag">Implementation reference</span>
      <h1>CloudServe Support System</h1>
      <p style="color:var(--ink-dim);margin:.3em 0 0">Answers from documentation when it can
      defend the answer; escalates to a person, with context, when it cannot.</p>
      <div class="stats">
        <div class="stat"><b>16</b><span>Functional reqs</span></div>
        <div class="stat"><b>9</b><span>Non-functional</span></div>
        <div class="stat"><b>57</b><span>Decisions</span></div>
        <div class="stat"><b>505</b><span>Tests</span></div>
        <div class="stat"><b>90</b><span>Doc chunks</span></div>
        <div class="stat"><b>42/80</b><span>Answered</span></div>
        <div class="stat"><b>0</b><span>Bad citations</span></div>
      </div>
    </div>
    <article id="doc"></article>
  </main>
</div>
<script type="text/markdown" id="source">__MD__</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script type="module">
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.esm.min.mjs";

const src = document.getElementById("source").textContent;
const blocks = [];
// Pull mermaid fences out before markdown parsing, put placeholders back afterwards.
const prepared = src.replace(/```mermaid\n([\s\S]*?)```/g, (_, body) => {
  blocks.push(body);
  return `\n<div class="mermaid-slot" data-i="${blocks.length - 1}"></div>\n`;
});

marked.setOptions({ gfm: true, breaks: false, headerIds: true, mangle: false });
const doc = document.getElementById("doc");
doc.innerHTML = marked.parse(prepared);

// The first h1 and the hand-written contents table duplicate the banner and the sidebar.
doc.querySelector("h1")?.remove();

doc.querySelectorAll(".mermaid-slot").forEach(slot => {
  const d = document.createElement("div");
  d.className = "mermaid";
  d.textContent = blocks[Number(slot.dataset.i)];
  slot.replaceWith(d);
});

const dark = !window.matchMedia("(prefers-color-scheme: light)").matches;
mermaid.initialize({
  startOnLoad: false,
  theme: dark ? "dark" : "default",
  securityLevel: "strict",
  themeVariables: { fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontSize: "13px" }
});
await mermaid.run({ nodes: doc.querySelectorAll(".mermaid") });

// Sidebar, built from what actually rendered.
const links = document.getElementById("toc-links");
const heads = [...doc.querySelectorAll("h2, h3")];
const seen = new Map();
// GitHub's slug algorithm, so the #uc-4--the-answered-path style links written in the
// markdown resolve here exactly as they do when the .md is viewed on GitHub. Collapsing
// runs of punctuation instead would silently break every one of them.
const slug = s => s.trim().toLowerCase().replace(/[^\w\- ]+/g, "").replace(/ /g, "-");
for (const h of heads) {
  let id = h.id || slug(h.textContent);
  if (seen.has(id)) { const n = seen.get(id) + 1; seen.set(id, n); id = `${id}-${n}`; }
  else seen.set(id, 0);
  h.id = id;
  const a = document.createElement("a");
  a.href = `#${id}`;
  a.textContent = h.textContent.replace(/^#+\s*/, "");
  if (h.tagName === "H3") a.className = "lvl3";
  links.appendChild(a);
}

const byId = new Map([...links.children].map(a => [a.getAttribute("href").slice(1), a]));
const obs = new IntersectionObserver(entries => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    links.querySelector("a.active")?.classList.remove("active");
    byId.get(e.target.id)?.classList.add("active");
  }
}, { rootMargin: "0px 0px -78% 0px", threshold: 0 });
heads.forEach(h => obs.observe(h));

const toc = document.getElementById("toc");
document.getElementById("burger").addEventListener("click", () => toc.classList.toggle("open"));
links.addEventListener("click", () => toc.classList.remove("open"));
</script>
</body>
</html>
"""

# A <script> element holds raw text: HTML entities are NOT decoded inside it, so escaping the
# markdown would leave "&lt;" visible in the page. Only the sequence that would end the element
# early needs neutralising.
payload = re.sub(r"</(script)", r"<\\/\1", md, flags=re.IGNORECASE)
out = TEMPLATE.replace("__MD__", payload)
Path("docs/Implementation.html").write_text(out)
print("wrote docs/Implementation.html", len(out), "bytes")
