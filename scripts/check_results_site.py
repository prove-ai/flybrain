"""Checks for the EXP1-EXP3 result pages. Each subcommand prints one success marker only after every
assertion passes, and exits 1 with the reasons otherwise.

    python3 scripts/check_results_site.py manifest|determinism|trace|pagenumbers|plain|site|prs|live

The selector resolver here is written separately from build_figures.py's, so a resolver bug cannot hide
in both. Paths are relative to this repository; the workspace (for the original result files) is its parent.
"""
from __future__ import annotations

import csv
import filecmp
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WS = REPO.parent
SRC = REPO / "site" / "src"
SOURCES = SRC / "results" / "sources"
IMG = SRC / "img" / "results"
MANIFEST = json.loads((REPO / "reports" / "figure_data_manifest.json").read_text())
NEW_PAGES = ["projects/steering-circuit/index.html", "projects/forward-only-training/index.html",
             "projects/fly-retrieval/index.html", "results/index.html"]
OLD_PROJECTS = ["flyworld-game", "flyworld-studio", "flyvision", "connectome-lm", "flycodes", "connectome-atlas",
                "sugar-circuit"]
LIVE = "https://flybrain-q2h5.onrender.com"
REMOTES = {"AUW160150/flybrain": "origin", "prove-ai/flybrain": "proveai"}
BRANCH = "results-exp1-exp2-exp3"
BASE_COMMIT = "f42f586668af0a6f0cd14b6d9475cc415b1b5284"

errors: list[str] = []


def fail(msg: str) -> None:
    errors.append(msg)


def finish(marker: str) -> None:
    if errors:
        print("\n".join(errors[:80]))
        print(f"{len(errors)} problem(s)")
        sys.exit(1)
    print(marker)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------------ resolver
NUMS = re.compile(r"[+\-−]?\d[\d,]*(?:\.\d+)?")


def to_float(tok: str) -> float:
    return float(tok.replace("−", "-").replace(",", ""))


class Resolver:
    def __init__(self):
        self.docs = {}

    def doc(self, rel):
        if rel not in self.docs:
            text = (SOURCES / rel).read_text(encoding="utf-8")
            self.docs[rel] = (json.loads(text) if rel.endswith(".json") else
                              list(csv.reader(io.StringIO(text))) if rel.endswith(".csv") else text)
        return self.docs[rel]

    def values(self, sel):
        """Every scalar the selector denotes, as a list (wildcards and multi-row csv expand)."""
        idx = None
        m = re.search(r"\[(\d+)\]$", sel)
        if m and sel.startswith("csv:"):
            idx, sel = int(m.group(1)), sel[:m.start()]
        num_k = None
        m = re.search(r"@(\d+)$", sel)
        if m and sel.startswith("md"):
            num_k, sel = int(m.group(1)), sel[:m.start()]
        kind, rest = sel.split(":", 1)
        rel, spec = rest.split("#", 1)
        d = self.doc(rel)
        if kind == "json":
            vals = self._ptr(d, [t.replace("~1", "/").replace("~0", "~") for t in spec.split("/")[1:]])
        elif kind == "md":
            vals = [self._md(d, spec)]
        elif kind == "mdtext":
            m = re.search(spec, d)
            if not m:
                raise KeyError(f"mdtext not found: {sel}")
            vals = [m.group(1)]
        elif kind == "csv":
            filt, col = spec.rsplit("|", 1)
            head, *rows = d
            conds = [c.split("=", 1) for c in filt.split(",")]
            hit = [r[head.index(col)] for r in rows if all(r[head.index(k)] == v for k, v in conds)]
            vals = [hit[idx]] if idx is not None else hit
        else:
            raise KeyError(sel)
        if num_k is not None:
            vals = [to_float(NUMS.findall(v)[num_k]) for v in vals]
        return vals

    def _ptr(self, cur, toks):
        if not toks:
            return [cur]
        t, rest = toks[0], toks[1:]
        if t == "*":
            items = cur if isinstance(cur, list) else [cur[k] for k in sorted(cur)]
            out = []
            for it in items:
                out += self._ptr(it, rest)
            return out
        return self._ptr(cur[int(t)] if isinstance(cur, list) else cur[t], rest)

    @staticmethod
    def _md(text, spec):
        caption, row, col = spec.split("|")
        lines = text.split("\n")
        s = next(i for i, ln in enumerate(lines) if caption in ln)
        t = next(i for i in range(s + 1, len(lines)) if lines[i].lstrip().startswith("|"))
        split = lambda ln: [c.strip() for c in ln.strip()[1:-1].split("|")]
        head = split(lines[t])
        rows = []
        for ln in lines[t + 2:]:
            if not ln.lstrip().startswith("|"):
                break
            rows.append(dict(zip(head, split(ln))))
        if "=" in row:
            pairs = [p.split("=", 1) for p in row.split(",")]
            hit = [r for r in rows if all(r.get(k) == v for k, v in pairs)]
        else:
            hit = [r for r in rows if r[head[0]] == row]
        if len(hit) != 1:
            raise KeyError(f"md row not unique: {spec}")
        return hit[0][col]

    def numbers(self, sel):
        """All numbers a selector can contribute (cells contribute each number they contain)."""
        out = []

        def walk(v):
            if isinstance(v, bool):
                return
            if isinstance(v, (int, float)):
                out.append(float(v))
            elif isinstance(v, str):
                out.extend(to_float(t) for t in NUMS.findall(v))
            elif isinstance(v, list):
                for x in v:
                    walk(x)
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
        for v in self.values(sel):
            walk(v)
        return out


R = Resolver()


# ------------------------------------------------------------------------------------------ manifest
def cmd_manifest():
    files = MANIFEST["files"]
    for rel, meta in files.items():
        v = SOURCES / rel
        if not v.exists():
            fail(f"vendored copy missing: {rel}")
            continue
        if sha(v) != meta["sha256"]:
            fail(f"vendored hash mismatch: {rel}")
        if meta["origin"].startswith("workspace"):
            o = WS / rel
            if not o.exists() or sha(o) != meta["sha256"]:
                fail(f"workspace original missing or changed: {rel}")
    if "fly_retrieval/b2/flyhash_candidates_touched.csv" in files:
        for row in csv.DictReader(io.StringIO((SOURCES / "fly_retrieval/b2/flyhash_candidates_touched.csv").read_text())):
            p = WS / "fly_retrieval" / row["result_file"]
            if not p.exists() or sha(p) != row["result_file_sha256"]:
                fail(f"HDF5 result file changed: {row['result_file']}")
    for it in MANIFEST["inventory_only"]:
        if sha(WS / it["file"]) != it["sha256"]:
            fail(f"inventory file changed: {it['file']}")
    for fig in MANIFEST["figures"]:
        if not fig["sources"] or not fig["fields"]:
            fail(f"{fig['id']}: no sources or fields")
        for s in fig["fields"]:
            rel = s.split(":", 1)[1].split("#", 1)[0]
            if rel not in fig["sources"] or rel not in files:
                fail(f"{fig['id']}: field file not a recorded source: {rel}")
        for suffix in ("", "-sm"):
            if not (IMG / f"{fig['id']}{suffix}.svg").exists():
                fail(f"{fig['id']}: missing {fig['id']}{suffix}.svg")
    built = {p.stem.replace("-sm", "") for p in IMG.glob("*.svg")}
    if built != {f["id"] for f in MANIFEST["figures"]}:
        fail(f"SVGs without a manifest entry or vice versa: {sorted(built ^ {f['id'] for f in MANIFEST['figures']})}")
    print(f"{len(files)} source files, {len(MANIFEST['figures'])} figures, "
          f"{sum(len(f['fields']) for f in MANIFEST['figures'])} field selectors")
    finish("MANIFEST OK")


# ------------------------------------------------------------------------------------------ determinism
def cmd_determinism():
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        for d in (a, b):
            subprocess.run([sys.executable, str(REPO / "scripts" / "build_figures.py"), "--out", d], check=True,
                           stdout=subprocess.DEVNULL)
        names = sorted(p.name for p in Path(a).glob("*.svg"))
        if names != sorted(p.name for p in IMG.glob("*.svg")):
            fail("built file set differs from checked-in set")
        for n in names:
            if not filecmp.cmp(Path(a) / n, Path(b) / n, shallow=False):
                fail(f"two builds differ: {n}")
            if not filecmp.cmp(Path(a) / n, IMG / n, shallow=False):
                fail(f"checked-in SVG is stale: {n}")
    print(f"{len(names)} SVGs built twice")
    finish("DETERMINISTIC OK")


# ------------------------------------------------------------------------------------------ trace
# Digits in figure text that are not data: panel numbers, units, counts that define the design.
LITERAL_TEXT_OK = re.compile(
    r"^(1 · |2 · |0% = exactly|Target hidden for two 1–2 s spells|measured in percentage points \(1 point = 0\.01\)|"
    r"Share of time the target stayed in view at the|right distance \(1 point = 0\.01\)|"
    r"0 = the real circuit|20 scrambled circuits × 5 training runs each|SIFT: 1 million|GloVe: 1\.2 million|"
    r"90% accuracy|Rewired copies \(3\)|\+2\.5% needed|Shaded band: the \+2\.5% gain|"
    r"(?:Vertical axis: )?.*10× faster|entries examined per question \(M = million\)|3 bits|10 bits|30 bits|100 bits|"
    r"No chance was computed past 0\.10|Gap on the main score|Each dot is one scrambled circuit|"
    r"one scrambled circuit \(64 in all\)|.*\(64 in all\)\.|Shaded area right of the dashed line: a benefit of at least \+2\.5%|"
    r"2 · Familiar items missed|.*· EXPERIMENT [123]$|Rewired copy [123]$|.*range we can be 95% confident about.*|"
    r"95% confident about\. Filled dot: reliably$|(the right distance )?\(1 point = 0\.01\)$|"
    r"20 scrambled circuits × 5 training$|FlyHash setting that reaches 90% accuracy$|accuracy \(recall at 10\)$|"
    r"benefit of at least \+2\.5%, the size set in$)")


def scale_of(el, parents, axis):
    cur = el
    while cur is not None:
        spec = cur.get(f"data-s{axis}")
        if spec:
            return json.loads(spec)
        cur = parents.get(cur)
    return None


def to_px(s, v):
    import math
    if s["log"]:
        a, b, v = math.log10(s["v0"]), math.log10(s["v1"]), math.log10(v)
    else:
        a, b = s["v0"], s["v1"]
    return s["p0"] + (v - a) / (b - a) * (s["p1"] - s["p0"])


def geom(el, attr):
    """The pixel coordinate an encoding names: a line's own x1/x2/y1/y2, a rect's far edge for x2/y2."""
    if el.get(attr) is not None:
        return float(el.get(attr))
    far = {"x2": ("x", "width"), "y2": ("y", "height")}[attr]
    return float(el.get(far[0])) + float(el.get(far[1]))


def decimals(tok):
    return len(tok.split(".")[1]) if "." in tok else 0


def cmd_trace():
    ns = "{http://www.w3.org/2000/svg}"
    n_marks = n_text = 0
    for svg in sorted(IMG.glob("*.svg")):
        root = ET.parse(svg).getroot()
        parents = {c: p for p in root.iter() for c in p}
        for el in root.iter():
            tag = el.tag.replace(ns, "")
            enc = el.get("data-enc")
            text = "".join(t for t in el.itertext() if t) if tag == "text" else ""
            if tag == "text":
                title = el.find(f"{ns}title")
                if title is not None and title.text:
                    text = text.replace(title.text, "", 1)
            if not enc:
                if tag == "text" and re.search(r"\d", text) and "tk" not in (el.get("class") or "").split():
                    if not LITERAL_TEXT_OK.match(text):
                        fail(f"{svg.name}: unsourced number in text {text!r}")
                continue
            for attr, axis, value, sel, fmt in json.loads(enc):
                try:
                    vals = R.values(sel)
                except Exception as e:  # noqa: BLE001
                    fail(f"{svg.name}: cannot resolve {sel}: {e}")
                    continue
                if fmt.startswith("count="):
                    src = float(sum(1 for v in vals if v == fmt[6:]))
                elif fmt == "len":
                    src = float(len(vals))
                else:
                    if len(vals) != 1:
                        fail(f"{svg.name}: selector is not a single value: {sel}")
                        continue
                    raw = vals[0]
                    src = to_float(raw) if isinstance(raw, str) else float(raw)
                    if fmt == "/100":
                        src /= 100
                if abs(src - float(value)) > 1e-9 * max(1, abs(src)):
                    fail(f"{svg.name}: recorded {value} but source {sel} says {src}")
                if attr in ("text", "text2"):
                    n_text += 1
                    toks = NUMS.findall(text)
                    k = 0 if attr == "text" else 1
                    if len(toks) <= k:
                        fail(f"{svg.name}: no number #{k} in {text!r}")
                        continue
                    tok = toks[k]
                    shown = to_float(tok)
                    want = src * 100 if fmt == "x100" else src
                    if "M" in text[text.find(tok) + len(tok):][:1]:
                        want = src / 1e6
                    tol = 0.5 * 10 ** -decimals(tok) + 1e-9
                    signed_tok = tok[0] in "+-\u2212"
                    sign_bad = signed_tok and round(abs(want), decimals(tok)) > 0 and (shown < 0) != (want < 0)
                    if abs(abs(shown) - abs(want)) > tol or sign_bad:
                        fail(f"{svg.name}: text {text!r} shows {tok}, source gives {want}")
                else:
                    n_marks += 1
                    s = scale_of(el, parents, axis)
                    if s is None:
                        fail(f"{svg.name}: mark without a {axis} scale ({sel})")
                        continue
                    got, exp = geom(el, attr), to_px(s, src)
                    if abs(got - exp) > 0.05:
                        fail(f"{svg.name}: {attr} at {got:.2f}, value {src} maps to {exp:.2f} ({sel})")
    # every frontier marker sits on an exported row, and each method shows its whole front
    for layout in ("", "-sm"):
        root = ET.parse(IMG / f"exp3-frontier{layout}.svg").getroot()
        rows = []
        for e in root.iter():
            recs = json.loads(e.get("data-enc") or "[]")
            if len(recs) == 2 and all("b2/export.csv" in r[3] for r in recs):
                rows.append(recs)
        front = {}
        for (_, _, r, rsel, _), (_, _, q, qsel, _) in rows:
            key = rsel.split("#")[1].split("|")[0]
            if rsel.split("[")[-1] != qsel.split("[")[-1]:
                fail(f"frontier point pairs recall and speed from different rows: {rsel} / {qsel}")
            front.setdefault(key, []).append((r, q))
        summ = R.doc("fly_retrieval/reports/trackB2_summary.json")
        for key, pts in front.items():
            ds, algo = key.split(",")[0].split("=")[1], key.split(",")[1].split("=")[1]
            ref = [tuple(p) for p in summ[ds]["algorithms"][algo]["front"] if p[1] >= 10]
            if sorted(pts) != sorted(ref):
                fail(f"frontier{layout} {ds} {algo}: plotted points are not exactly the exported front")
        if len(front) != 12:
            fail(f"frontier{layout}: expected 12 method-dataset lines, found {len(front)}")
    print(f"{n_marks} mark positions and {n_text} number labels re-read from source")
    finish("TRACE OK")


# ------------------------------------------------------------------------------------------ page text
class Text(HTMLParser):
    """Collects visible text with flags for <details>, source lines and alt text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.chunks = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("img",) and a.get("alt"):
            self.chunks.append(("alt", a["alt"]))
        if tag in ("br", "hr", "img", "meta", "link", "input"):
            return
        self.stack.append((tag, a.get("class", "") or ""))

    def handle_endtag(self, tag):
        while self.stack:
            t, _ = self.stack.pop()
            if t == tag:
                break

    def handle_data(self, data):
        tags = [t for t, _ in self.stack]
        classes = " ".join(c for _, c in self.stack)
        if any(t in ("script", "style", "title", "head", "svg") for t in tags):
            return
        if "footer" in tags:
            self.chunks.append(("footer", data))
            return
        kind = "details" if "details" in tags else "src" if "rfig-src" in classes else \
            "code" if "code" in tags else "main"
        self.chunks.append((kind, data))


def page_text(html):
    p = Text()
    p.feed(html)
    return p.chunks


def index_new_html():
    """The parts of the landing page this branch added (the old parts are checked for being unchanged)."""
    old = subprocess.run(["git", "-C", str(REPO), "show", f"{BASE_COMMIT}:site/src/index.html"],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    new = (SRC / "index.html").read_text().splitlines()
    import difflib
    return "\n".join(l[1:] for l in difflib.ndiff(old, new) if l.startswith("+ "))


def pages():
    out = {"/" + p.replace("index.html", ""): (SRC / p).read_text() for p in NEW_PAGES}
    out["/"] = index_new_html()
    return out


def page_figures(html):
    return sorted({m for m in re.findall(r'/img/results/([a-z0-9-]+?)(?:-sm)?\.svg', html)})


def cmd_pagenumbers():
    figs = {f["id"]: f for f in MANIFEST["figures"]}
    consts = [c["value"] for c in MANIFEST["constants"]]
    for c in MANIFEST["constants"]:
        # a constant must be the source's value, or named by the selector itself (a readout key, a field
        # name such as fnr_at_fpr_1pct, a column header such as "95% CI"), and the selector must resolve
        R.values(c["source"])
        named = [float(x) / (100 if pct else 1) for x, pct in
                 re.findall(r"(\d+(?:\.\d+)?)(%|pct)?", c["source"].split("#", 1)[1])]
        if not any(abs(c["value"] - v) < 1e-12 for v in R.numbers(c["source"]) + named):
            fail(f"constant {c['value']} not found at {c['source']}")
    checked = 0
    for url, html in pages().items():
        pool = list(consts)
        for fid in page_figures(html):
            for sel in figs[fid]["fields"]:
                pool += R.numbers(sel)
        for sel in MANIFEST["pages"].get(url, []):
            pool += R.numbers(sel)
        cands = set()
        for v in pool:
            for c in (v, v * 100, v / 100):
                cands.add(abs(c))
        text = " ".join(t for k, t in page_text(html))
        for m in re.finditer(r"[+\-−]?\d[\d,]*\.\d+%?|[+\-−]?\d+(?:\.\d+)?%", text):
            tok = m.group(0).rstrip("%")
            val = abs(to_float(tok))
            tol = 0.5 * 10 ** -decimals(tok) + 1e-9
            checked += 1
            if not any(abs(val - c) <= tol for c in cands):
                ctx = text[max(0, m.start() - 50):m.end() + 20].replace("\n", " ")
                fail(f"{url}: {m.group(0)} has no source value  …{ctx}…")
    print(f"{checked} numbers checked across {len(pages())} pages")
    finish("PAGE NUMBERS OK")


BANNED = [(r"\bCIs?\b", "CI"), (r"SESOI", "SESOI"), (r"\bp\s*[=<>]", "p ="), ("Δ", "Δ"), ("±", "±"),
          (r"\bTOST\b", "TOST"), (r"\bMDE\b", "MDE"), (r"\bSD\b", "SD"), (r"\bBPTT\b", "BPTT"), (r"\bQPS\b", "QPS"),
          (r"\bFBF\b", "FBF"), (r"\bANN\b", "ANN"), (r"\bKCs?\b", "KC"), (r"\bG1b?\b", "G1"), (r"recall@", "recall@"),
          (r"\bnull\b", "null (jargon)"), (r"\bseed", "seed (jargon)")]
ACRONYMS = {
    "SIFT": "image descriptors", "GloVe": "word vectors", "MNIST": "handwritten digits",
    "HNSW": "hierarchical navigable small world", "ScaNN": "Google's search library",
    "FAISS": "Meta's similarity-search library", "IVF-PQ": "inverted file with product quantisation",
    "DoOR": "Database of Odorant Responses", "MaleCNS": "central nervous system", "EXP1": "Experiment 1",
    "EXP2": "Experiment 2", "EXP3": "Experiment 3", "SimHash": "compact codes", "MinHash": "estimating overlap",
}


def cmd_plain():
    svg_text = {p.name: " ".join(ET.parse(p).getroot().itertext()) for p in IMG.glob("*.svg")}
    for name, t in svg_text.items():
        for rx, label in BANNED:
            if label in ("seed (jargon)",):
                continue
            if re.search(rx, t):
                fail(f"{name}: statistical notation or jargon '{label}' in figure text")
    for url, html in pages().items():
        chunks = page_text(html)
        main = " ".join(t for k, t in chunks if k in ("main", "details", "alt"))
        for rx, label in BANNED:
            for m in re.finditer(rx, main):
                fail(f"{url}: '{label}' in visible text …{main[max(0, m.start() - 40):m.end() + 30]}…")
        body = " ".join(t for k, t in chunks if k == "main")
        sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", body))
        for ac, expansion in ACRONYMS.items():
            first = next((s for s in sentences if re.search(rf"(?<![\w-]){re.escape(ac)}(?![\w-])", s)), None)
            if first is not None and expansion not in first:
                fail(f"{url}: {ac} first appears without its explanation: “{first[:160]}”")
    finish("PLAIN OK")


# ------------------------------------------------------------------------------------------ site
def git_show(path):
    return subprocess.run(["git", "-C", str(REPO), "show", f"{BASE_COMMIT}:{path}"], capture_output=True, text=True,
                          check=True).stdout


def resolve_local(href):
    href = href.split("#")[0].split("?")[0]
    if not href or not href.startswith("/"):
        return True
    p = SRC / href.lstrip("/")
    if href.endswith("/"):
        p = p / "index.html"
    if p.exists():
        return True
    built = ("/flyworld/", "/atlas/", "/flycodes/", "/sugar/")
    return href.startswith(built)


def cmd_site():
    idx = (SRC / "index.html").read_text()
    old = git_show("site/src/index.html")
    blocks = lambda h, rx: re.findall(rx, h, re.S)
    for rx, what in ((r'<section class="hero">.*?</section>', "hero and live panel"),
                     (r'<div class="stats">.*?</div></div>', "key figures"), (r"<footer>.*?</footer>", "footer")):
        if blocks(idx, rx) != blocks(old, rx) or not blocks(old, rx):
            fail(f"landing page {what} changed")
    cards = lambda h: re.findall(r'<li><a href="[^"]+"><span class="n">(\d\d)</span>.*?</a></li>', h, re.S)
    full = lambda h: re.findall(r'(<li><a href="[^"]+"><span class="n">\d\d</span>.*?</a></li>)', h, re.S)
    if cards(idx) != [f"{i:02d}" for i in range(1, 11)]:
        fail(f"cards are {cards(idx)}, expected 01-10")
    if full(idx)[:7] != full(old):
        fail("one of the seven existing cards changed")
    for c in full(idx)[7:]:
        if '<b class="read">Read</b>' not in c or "<small>" not in c or '<span class="d">' not in c:
            fail(f"new card does not follow the pattern: {c[:80]}")
    if 'id="method"' not in idx or "preregistered with its analysis code frozen" not in idx:
        fail("Method section does not mention the preregistration")
    changed = subprocess.run(["git", "-C", str(REPO), "diff", "--name-only", BASE_COMMIT, "--"] +
                             [f"site/src/projects/{p}" for p in OLD_PROJECTS], capture_output=True, text=True).stdout
    if changed.strip():
        fail(f"existing project pages changed: {changed.split()}")
    nfig = 0
    for page in NEW_PAGES + ["index.html"]:
        html = (SRC / page).read_text()
        if page != "index.html" and "<h1>" not in html:
            fail(f"{page}: no h1")
        for href in re.findall(r'(?:href|src)="([^"]+)"', html):
            if not resolve_local(href):
                fail(f"{page}: broken local link {href}")
        for fig in re.findall(r'<figure class="rfig[^"]*"[^>]*>.*?</figure>', html, re.S):
            nfig += 1
            cap = re.search(r"<figcaption>(.*?)</figcaption>", fig, re.S)
            if not cap or len(re.sub(r"<[^>]+>", "", cap.group(1)).split()) < 12:
                fail(f"{page}: figure without a real caption")
            srcline = re.search(r'<p class="rfig-src">(.*?)</p>', fig, re.S)
            links = re.findall(r'href="(/results/sources/[^"]+)"', srcline.group(1)) if srcline else []
            if not links or not all(resolve_local(h) for h in links):
                fail(f"{page}: figure without a resolving source link")
            for src, w, h in re.findall(r'<img class="rfig-(?:lg|sm)" src="([^"]+)" width="(\d+)" height="(\d+)"', fig):
                vb = re.search(r'viewBox="0 0 (\d+) (\d+)"', (SRC / src.lstrip("/")).read_text())
                if not vb or vb.groups() != (w, h):
                    fail(f"{page}: {src} declared {w}x{h}, file is {vb.groups() if vb else None}")
            if len(re.findall(r'<img class="rfig-(lg|sm)"', fig)) != 2:
                fail(f"{page}: figure needs both a desktop and a phone image")
    used = set()
    for page in NEW_PAGES:
        used |= set(page_figures((SRC / page).read_text()))
    missing = {f["id"] for f in MANIFEST["figures"]} - used
    if missing:
        fail(f"figures built but not shown on any result page: {sorted(missing)}")
    print(f"{len(NEW_PAGES)} new pages, {nfig} figure blocks, 3 new cards")
    finish("SITE OK")


# ------------------------------------------------------------------------------------------ git & live
def cmd_prs():
    for repo, remote in REMOTES.items():
        out = subprocess.run(["gh", "pr", "list", "--repo", repo, "--head", BRANCH, "--state", "all", "--json",
                              "number,state,baseRefName,url,headRefOid"], capture_output=True, text=True)
        prs = json.loads(out.stdout or "[]")
        live = [p for p in prs if p["baseRefName"] == "main" and p["state"] in ("OPEN", "MERGED")]
        if not live:
            fail(f"{repo}: no open or merged pull request from {BRANCH} into main")
        for p in live:
            print(f"{repo}: #{p['number']} {p['state']} {p['url']}")
        ls = subprocess.run(["git", "-C", str(REPO), "ls-remote", remote, "refs/heads/main", f"refs/heads/{BRANCH}"],
                            capture_output=True, text=True).stdout.split()
        heads = dict(zip(ls[1::2], ls[0::2]))
        if f"refs/heads/{BRANCH}" not in heads and not any(p["state"] == "MERGED" for p in live):
            fail(f"{repo}: branch not pushed")
        main = heads.get("refs/heads/main")
        subprocess.run(["git", "-C", str(REPO), "fetch", "-q", remote], check=False)
        anc = subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", BASE_COMMIT, main or "HEAD"])
        if not main or anc.returncode != 0:
            fail(f"{repo}: main no longer contains {BASE_COMMIT[:7]} (history rewritten?)")
    finish("PRS OK")


def cmd_live():
    urls = ["/", "/results/", "/projects/steering-circuit/", "/projects/forward-only-training/",
            "/projects/fly-retrieval/", "/js/results.js", "/css/site.css"]
    # /sugar/ has no index page; the project page links its explorer and anatomy views directly
    urls += [f"/projects/{p}/" for p in OLD_PROJECTS] + ["/flyworld/", "/atlas/", "/flycodes/"]
    urls += sorted(set(re.findall(r'href="(/sugar/[^"]+)"', (SRC / "projects/sugar-circuit/index.html").read_text())))
    urls += [f"/img/results/{f['id']}{s}.svg" for f in MANIFEST["figures"] for s in ("", "-sm")]
    urls += [m["served_at"] for m in MANIFEST["files"].values()]
    for u in urls:
        try:
            with urllib.request.urlopen(urllib.request.Request(LIVE + u, headers={"User-Agent": "flybrain-check"}),
                                        timeout=30) as r:
                body = r.read()
                if r.status != 200:
                    fail(f"{u}: status {r.status}")
                if u.endswith(".svg") and sha(Path(IMG / u.split("/")[-1])) != hashlib.sha256(body).hexdigest():
                    fail(f"{u}: live SVG differs from the repository")
                if u == "/" and b'id="results"' not in body:
                    fail("live landing page has no Results section")
        except Exception as e:  # noqa: BLE001
            fail(f"{u}: {e}")
    print(f"{len(urls)} URLs fetched from {LIVE}")
    finish("LIVE OK")


if __name__ == "__main__":
    cmds = {"manifest": cmd_manifest, "determinism": cmd_determinism, "trace": cmd_trace,
            "pagenumbers": cmd_pagenumbers, "plain": cmd_plain, "site": cmd_site, "prs": cmd_prs, "live": cmd_live}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(f"usage: {sys.argv[0]} {'|'.join(cmds)}")
    cmds[sys.argv[1]]()
