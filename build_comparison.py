"""Build the three-provider comparison page from run artifacts in output/.

Reads whatever <prefix>results.json / <prefix>diagnostics.json each run wrote and
renders one static HTML page. Nothing here recomputes an answer — the page is a
view over recorded runs, so it can never disagree with what the pipeline produced.

    .venv/bin/python build_comparison.py
"""
from __future__ import annotations

import html
import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "output"
PAGE = OUT / "provider_comparison_3way.html"

# Each run is (label, file prefix, series colour token, footnote about the run's conditions).
RUNS = [
    ("Gemini", "", "s1", "warm tag cache (81 hits) · free tier"),
    ("Claude", "cmp_anthropic_", "s2", "cold run · paid tier"),
    ("Local", "local3_", "s3", "cold run · LM Studio, offline"),
]
REF = json.loads((ROOT / "data" / "answers_reference.json").read_text(encoding="utf-8"))
PARAMS = [k for k in REF if not k.startswith("_")]


def load(prefix: str) -> tuple[dict, dict]:
    def read(kind: str) -> dict:
        p = OUT / f"{prefix}{kind}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return read("results"), read("diagnostics")


# ── correctness checklist ────────────────────────────────────────────────────
# The reference sheet shipped with the assignment is a CHECKLIST, never a tuning
# target: a wordier answer that carries the reference's facts is still correct, so
# matching is on the facts (the numbers), not on the phrasing.
def norm(s: object) -> str:
    s = unicodedata.normalize("NFKC", str(s or "")).replace("₪", 'ש"ח')
    return re.sub(r"[\s,\-–—]+", " ", s).strip()


def digits(s: object) -> set[str]:
    return set(re.findall(r"\d+", norm(s).replace(",", "")))


def verdict(ref: object, got: object) -> tuple[str, str]:
    """-> (css class, label). Absent-by-design parameters invert the test."""
    if ref is None:
        return ("good", "✓ correctly absent") if not norm(got) else ("bad", "✗ invented")
    if not norm(got):
        return "bad", "✗ empty"
    rd, gd = digits(ref), digits(got)
    if rd:
        return ("good", "✓ correct") if rd <= gd else \
               (("warn", "~ partial") if rd & gd else ("bad", "✗ wrong"))
    rt, gt = set(norm(ref).split()), set(norm(got).split())
    ov = len(rt & gt) / max(len(rt), 1)
    return ("good", "✓ correct") if ov > .5 else \
           (("warn", "~ partial") if ov > .2 else ("bad", "✗ wrong"))


def esc(s: object) -> str:
    return html.escape(str(s or ""))


# ── chart primitives (inline SVG, themed through the same tokens as the page) ──
def bars(title: str, sub: str, values: dict[str, list[float | None]], vmax: float,
         fmt=lambda v: f"{v:g}") -> str:
    """Grouped bars: one cluster per parameter, one bar per provider."""
    W, H, PAD_L, PAD_B, TOP = 880, 250, 46, 46, 10
    plot_h = H - PAD_B - TOP
    n_groups, n_series = len(PARAMS), len(RUNS)
    gw = (W - PAD_L - 12) / n_groups
    bw = min(20.0, (gw - 14) / n_series)
    svg = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(title)}">']
    steps = 5
    for i in range(steps + 1):
        v = vmax * i / steps
        y = TOP + plot_h - plot_h * (i / steps)
        svg.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - 8}" y2="{y:.1f}" '
                   f'stroke="var(--grid)" stroke-width="1"/>')
        svg.append(f'<text x="{PAD_L - 6}" y="{y + 3.5:.1f}" text-anchor="end" font-size="10.5" '
                   f'fill="var(--muted)">{fmt(v)}</text>')
    for g, param in enumerate(PARAMS):
        x0 = PAD_L + g * gw + (gw - bw * n_series) / 2
        for s, (label, _, tok, _) in enumerate(RUNS):
            v = values.get(label, [None] * n_groups)[g]
            x = x0 + s * bw
            if v is None:
                svg.append(f'<text x="{x + bw / 2:.1f}" y="{TOP + plot_h - 4:.1f}" '
                           f'text-anchor="middle" font-size="10" fill="var(--muted)">–</text>')
                continue
            h = max(0.0, plot_h * (v / vmax)) if vmax else 0.0
            y = TOP + plot_h - h
            svg.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw - 3:.1f}" height="{h:.1f}" '
                       f'rx="3" fill="var(--{tok})" class="m" '
                       f'data-tip="{esc(label)} · {esc(param)}: {fmt(v)}"/>')
        words = param.split("_")
        for li, word in enumerate(words[:2]):
            svg.append(f'<text x="{PAD_L + g * gw + gw / 2:.1f}" y="{H - 28 + li * 12}" '
                       f'text-anchor="middle" font-size="10.5" fill="var(--text2)">{esc(word)}</text>')
    svg.append(f'<line x1="{PAD_L}" y1="{TOP + plot_h}" x2="{W - 8}" y2="{TOP + plot_h}" '
               f'stroke="var(--grid)"/></svg>')
    keys = "".join(f'<span class="key"><i style="background:var(--{t})"></i>{esc(l)}</span>'
                   for l, _, t, _ in RUNS)
    return (f'<figure class="chart"><figcaption><b>{esc(title)}</b>'
            f'<span class="sub">{esc(sub)}</span><span class="legend">{keys}</span></figcaption>'
            + "".join(svg) + "</figure>")


def main() -> None:
    data = {label: load(prefix) for label, prefix, _, _ in RUNS}
    missing = [l for l, (r, _) in data.items() if not r]
    if missing:
        raise SystemExit(f"no results for: {', '.join(missing)} — run those providers first")

    scores = {l: [(data[l][0].get(p) or {}).get("score") for p in PARAMS] for l, *_ in RUNS}
    pages = {l: [(data[l][1].get("pages_sent_per_parameter") or {}).get(p) for p in PARAMS]
             for l, *_ in RUNS}
    correct = {l: [verdict(REF[p], (data[l][0].get(p) or {}).get("answer"))[0] for p in PARAMS]
               for l, *_ in RUNS}

    # ── tiles ────────────────────────────────────────────────────────────────
    tiles = []
    for label, prefix, tok, note in RUNS:
        res, diag = data[label]
        good = sum(v == "good" for v in correct[label])
        err = len(diag.get("extraction_errors") or [])
        tiles.append(
            f'<div class="tile"><div class="tl"><i style="background:var(--{tok})"></i>{esc(label)}</div>'
            f'<div class="tv">{good}<span class="tv-d">/{len(PARAMS)}</span></div>'
            f'<div class="tn">correct vs the answer sheet</div>'
            f'<dl class="mini">'
            f'<div><dt>calls</dt><dd>{diag.get("llm_calls", "–")}</dd></div>'
            f'<div><dt>tokens in</dt><dd>{diag.get("input_tokens", 0):,}</dd></div>'
            f'<div><dt>failed extractions</dt><dd class="{"bad-t" if err else ""}">{err}</dd></div>'
            f'</dl><div class="tn cond">{esc(note)}</div></div>')

    # ── answer table ─────────────────────────────────────────────────────────
    rows = []
    for p in PARAMS:
        cells = []
        for label, _, tok, _ in RUNS:
            res, diag = data[label]
            v = res.get(p) or {}
            cls, lab = verdict(REF[p], v.get("answer"))
            ans = norm(v.get("answer")) or "—"
            agree = (diag.get("agreement") or {}).get(p, "–")
            grounded = (diag.get("groundedness") or {}).get(p)
            cells.append(
                f'<td><div class="ans" dir="rtl">{esc(ans[:260])}</div>'
                f'<div class="meta"><span class="chip {cls}">{esc(lab)}</span> '
                f'<span class="mono">score {v.get("score", "–")}/5 · {esc(agree)}'
                f'{" · grounded" if grounded else ""}</span></div></td>')
        ref_txt = norm(REF[p]) if REF[p] is not None else "(not in the document)"
        rows.append(f'<tr><td class="pn">{esc(p)}</td>{"".join(cells)}'
                    f'<td><div class="ans ref" dir="rtl">{esc(ref_txt[:260])}</div></td></tr>')

    heads = "".join(f'<th><i style="background:var(--{t})"></i>{esc(l)}</th>' for l, _, t, _ in RUNS)

    charts = (
        bars("Certainty score by parameter", "1–5, model-given and evidence-capped — higher is better",
             {l: [s if isinstance(s, (int, float)) else None for s in scores[l]] for l, *_ in RUNS}, 5)
        + bars("Pages sent per parameter", "out of 81 — the page-economy objective, lower is better",
               {l: [v if isinstance(v, (int, float)) else None for v in pages[l]] for l, *_ in RUNS},
               max([v for l in pages for v in pages[l] if isinstance(v, (int, float))] + [1]))
    )

    totals = "".join(
        f'<tr><td class="pn">{esc(l)}</td>'
        f'<td class="num">{data[l][1].get("llm_calls", "–")}</td>'
        f'<td class="num">{data[l][1].get("cache_hits", "–")}</td>'
        f'<td class="num">{data[l][1].get("input_tokens", 0):,}</td>'
        f'<td class="num">{data[l][1].get("output_tokens", 0):,}</td>'
        f'<td class="num">{data[l][1].get("avg_pages_sent", "–")}</td>'
        f'<td class="num">{len(data[l][1].get("extraction_errors") or [])}</td></tr>'
        for l, *_ in RUNS)

    PAGE.write_text(TEMPLATE.format(
        tiles="".join(tiles), heads=heads, rows="".join(rows),
        charts=charts, totals=totals), encoding="utf-8")
    print(f"wrote {PAGE}")
    for l, *_ in RUNS:
        print(f"  {l:8} {sum(v == 'good' for v in correct[l])}/{len(PARAMS)} correct")


TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Three Providers, One Tender</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Hebrew:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root {{
  color-scheme: light;
  --surface:#fcfcfb; --raise:#ffffff; --text:#14140f; --text2:#54534c; --muted:#8a897f;
  --grid:#e6e5df; --s1:#2a78d6; --s2:#eb6834; --s3:#7a5cc4;
  --good:#0a7a3d; --warn:#a8760a; --bad:#d13b32;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  color-scheme: dark;
  --surface:#161615; --raise:#1e1e1c; --text:#f6f5f0; --text2:#b9b8ae; --muted:#84837a;
  --grid:#32312d; --s1:#5b9bea; --s2:#ef7f4f; --s3:#a189e0;
  --good:#3fb372; --warn:#d9a83c; --bad:#ef6a60;
}} }}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface:#161615; --raise:#1e1e1c; --text:#f6f5f0; --text2:#b9b8ae; --muted:#84837a;
  --grid:#32312d; --s1:#5b9bea; --s2:#ef7f4f; --s3:#a189e0;
  --good:#3fb372; --warn:#d9a83c; --bad:#ef6a60;
}}
* {{ box-sizing: border-box; margin: 0; }}
body {{ background: var(--surface); color: var(--text);
  font: 400 15px/1.6 "IBM Plex Sans Hebrew", -apple-system, "Segoe UI", sans-serif;
  max-width: 1080px; margin: 0 auto; padding: 40px 22px 72px; }}
header {{ display: flex; justify-content: space-between; align-items: start; gap: 16px; }}
h1 {{ font-size: 2rem; font-weight: 600; letter-spacing: -.02em; text-wrap: balance; }}
h2 {{ font-size: 1.1rem; font-weight: 600; margin: 44px 0 12px; letter-spacing: -.01em; }}
.sub {{ color: var(--text2); font-size: .93rem; max-width: 68ch; }}
.mono {{ font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: .82em; }}
.tiles {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 14px; margin: 26px 0 8px; }}
.tile {{ background: var(--raise); border: 1px solid var(--grid); border-radius: 12px; padding: 16px 18px; }}
.tl {{ font-size: .74rem; text-transform: uppercase; letter-spacing: .07em; color: var(--muted);
  display: flex; align-items: center; gap: 7px; }}
.tl i, .key i, th i {{ display: inline-block; width: 9px; height: 9px; border-radius: 2px; flex: none; }}
.tv {{ font-size: 2.1rem; font-weight: 600; line-height: 1.1; margin-top: 6px;
  font-variant-numeric: tabular-nums; }}
.tv-d {{ font-size: 1.1rem; color: var(--muted); font-weight: 400; }}
.tn {{ font-size: .78rem; color: var(--text2); }}
.cond {{ margin-top: 10px; color: var(--muted); }}
dl.mini {{ margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--grid);
  display: flex; flex-direction: column; gap: 3px; }}
dl.mini > div {{ display: flex; justify-content: space-between; font-size: .8rem; }}
dl.mini dt {{ color: var(--muted); }}
dl.mini dd {{ font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums; }}
.bad-t {{ color: var(--bad); font-weight: 600; }}
.panel {{ background: var(--raise); border: 1px solid var(--grid); border-left: 3px solid var(--s3);
  border-radius: 10px; padding: 14px 18px; margin: 22px 0; font-size: .92rem; }}
.panel.warn {{ border-left-color: var(--warn); }}
.panel b {{ font-weight: 600; }}
.chart {{ margin: 26px 0; }}
.chart figcaption {{ margin-bottom: 8px; font-size: .95rem; }}
.chart .sub {{ margin-left: 9px; font-size: .84rem; display: inline; }}
.legend {{ float: right; }}
.key {{ margin-left: 15px; font-size: .82rem; color: var(--text2); display: inline-flex;
  align-items: center; gap: 5px; }}
svg {{ width: 100%; height: auto; display: block; }}
.m {{ cursor: default; }} .m:hover {{ opacity: .82; }}
.twrap {{ overflow-x: auto; border: 1px solid var(--grid); border-radius: 10px; background: var(--raise); }}
table {{ border-collapse: collapse; width: 100%; min-width: 780px; }}
th, td {{ text-align: left; padding: 11px 13px; border-bottom: 1px solid var(--grid);
  vertical-align: top; font-size: .87rem; }}
tr:last-child td {{ border-bottom: 0; }}
th {{ color: var(--muted); font-size: .73rem; text-transform: uppercase; letter-spacing: .06em;
  font-weight: 500; white-space: nowrap; }}
th i {{ margin-right: 6px; vertical-align: middle; }}
.pn {{ font-family: "IBM Plex Mono", monospace; font-size: .8rem; white-space: nowrap; color: var(--text2); }}
.num {{ font-family: "IBM Plex Mono", monospace; font-variant-numeric: tabular-nums; }}
.ans {{ line-height: 1.5; }}
.ans.ref {{ color: var(--text2); }}
.meta {{ margin-top: 5px; color: var(--muted); }}
.chip {{ display: inline-block; border-radius: 999px; padding: 1px 9px; font-size: .74rem; font-weight: 600; }}
.chip.good {{ color: var(--good); background: color-mix(in srgb, var(--good) 13%, transparent); }}
.chip.warn {{ color: var(--warn); background: color-mix(in srgb, var(--warn) 15%, transparent); }}
.chip.bad {{ color: var(--bad); background: color-mix(in srgb, var(--bad) 13%, transparent); }}
#themebtn {{ border: 1px solid var(--grid); background: var(--raise); color: var(--text2);
  border-radius: 9px; padding: 6px 12px; cursor: pointer; font-size: .82rem; flex: none; }}
#themebtn:focus-visible {{ outline: 2px solid var(--s3); outline-offset: 2px; }}
#tip {{ position: fixed; pointer-events: none; background: var(--text); color: var(--surface);
  padding: 4px 9px; border-radius: 6px; font-size: .78rem; opacity: 0; transition: opacity .08s; z-index: 9; }}
ul {{ padding-left: 20px; }} li {{ margin: 7px 0; font-size: .92rem; }}
@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; }} }}
</style></head><body>
<header>
  <div>
    <h1>Three providers, one tender</h1>
    <p class="sub">The same pipeline and the same 81-page Hebrew tender
    (<span dir="rtl">מכרז מי שבע 05/25</span>), run against a paid cloud provider, a free-tier
    cloud provider, and three models running offline on a laptop. Scored against the answer
    sheet supplied with the assignment.</p>
  </div>
  <button id="themebtn">◐ theme</button>
</header>
{tiles}
<div class="panel warn"><b>Read the run conditions before the numbers.</b> These three runs were not
executed under identical conditions, and saying otherwise would overstate the comparison.
Gemini's run reused a warm page-tag cache (81 of its 102 calls were cache hits), so its token
totals are a fraction of the others' and are <b>not</b> comparable; its verification stage also
came back <span class="mono">unverified</span> on five parameters after exhausting free-tier
quota. Claude and Local both ran cold. Correctness is comparable across all three — that is
scored against the answer sheet, not against each other.</div>
<h2>Answers, side by side</h2>
<div class="twrap"><table><thead><tr><th>parameter</th>{heads}<th>reference</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<h2>Where the models disagree</h2>
{charts}
<h2>Cost of the run</h2>
<div class="twrap"><table><thead><tr><th>provider</th><th>calls</th><th>cache hits</th>
<th>tokens in</th><th>tokens out</th><th>avg pages/param</th><th>failed</th></tr></thead>
<tbody>{totals}</tbody></table></div>
<h2>How correctness was scored</h2>
<ul>
<li>The supplied answer sheet is a <b>checklist, never a tuning target</b>. Nothing in the
pipeline reads it; it is applied here, after the fact, to recorded runs.</li>
<li>A match is decided on <b>facts, not phrasing</b>: an answer is correct when it carries the
reference's figures (24 months, 40/60, ₪10,000). A wordier but factually complete answer counts
as correct — the tender's own language is verbose, and penalising that would reward terseness
over accuracy.</li>
<li><span class="mono">idea_author</span> is <b>absent from the document by design</b>. The
correct behaviour is to return nothing, so for that row an empty answer scores as correct and a
confident answer would be a hallucination.</li>
<li>The <span class="mono">score</span> shown per answer is the pipeline's own certainty, capped
by evidence — it is <b>not</b> the correctness verdict, and the two can disagree.</li>
</ul>
<div id="tip"></div>
<script>
(function () {{
  var b = document.getElementById('themebtn'), r = document.documentElement;
  b.addEventListener('click', function () {{
    var dark = matchMedia('(prefers-color-scheme: dark)').matches;
    var cur = r.getAttribute('data-theme') || (dark ? 'dark' : 'light');
    r.setAttribute('data-theme', cur === 'dark' ? 'light' : 'dark');
  }});
  var tip = document.getElementById('tip');
  document.addEventListener('mouseover', function (e) {{
    var t = e.target.getAttribute && e.target.getAttribute('data-tip');
    if (!t) {{ tip.style.opacity = 0; return; }}
    tip.textContent = t; tip.style.opacity = 1;
    tip.style.left = (e.clientX + 12) + 'px'; tip.style.top = (e.clientY - 28) + 'px';
  }});
}})();
</script>
</body></html>"""

if __name__ == "__main__":
    main()
