"""Streamlit UI for the tender extraction pipeline.

A thin presentation layer over solution.py — upload a tender, pick a provider,
get the results. All the logic lives in solution.py; this file only renders.

    streamlit run app.py
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import tempfile
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

import solution

load_dotenv(Path(__file__).parent / ".env")

st.set_page_config(page_title="חילוץ פרמטרים ממכרז", page_icon="📄", layout="wide")

# ─── Look & feel ─────────────────────────────────────────────────────────────
# Heebo carries Hebrew and Latin equally well; everything else is theme-neutral
# (rgba over the host theme) so light and dark mode both work.
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Heebo:wght@400;500;700;800&display=swap');
  html, body, .stMarkdown, .stMarkdown p, button, input, textarea,
  [data-testid="stSidebar"], [data-testid="stMarkdownContainer"] p {
    font-family: 'Heebo', -apple-system, 'Segoe UI', sans-serif !important;
  }
  .block-container { padding-top: 4.6rem; max-width: 1150px; }
  .stMarkdown p.hero-title {
    font-size: 2.3rem !important; font-weight: 800; line-height: 1.15; margin: 0;
    width: fit-content;
    background: linear-gradient(90deg, #6366f1, #14b8a6);
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  .stMarkdown p.hero-sub { opacity: .65; margin: .35rem 0 0; font-size: 1.02rem !important; }
  .kpi-card {
    border: 1px solid rgba(127,127,127,.22); border-radius: 14px;
    padding: .9rem 1.1rem; height: 100%;
    background: rgba(127,127,127,.06);
  }
  .kpi-label { font-size: .78rem; letter-spacing: .06em; text-transform: uppercase; opacity: .6; }
  .kpi-value { font-size: 1.55rem; font-weight: 700; margin-top: .15rem; }
  .kpi-note  { font-size: .78rem; opacity: .55; }
  .param-card {
    border: 1px solid rgba(127,127,127,.22); border-radius: 14px;
    padding: 1rem 1.2rem; margin-bottom: .9rem;
    background: rgba(127,127,127,.05);
    transition: border-color .15s ease;
  }
  .param-card:hover { border-color: rgba(99,102,241,.55); }
  .param-head { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; margin-bottom: .55rem; }
  .param-name { font-family: ui-monospace, monospace; font-size: .82rem; opacity: .6; }
  .param-source { font-weight: 600; font-size: .95rem; direction: rtl; }
  .pill {
    display: inline-block; padding: .12rem .6rem; border-radius: 999px;
    font-size: .78rem; font-weight: 700; line-height: 1.4;
  }
  .pill-score-hi  { background: rgba(16,185,129,.16); color: #10b981; }
  .pill-score-mid { background: rgba(245,158,11,.16); color: #d97706; }
  .pill-score-lo  { background: rgba(239,68,68,.15);  color: #ef4444; }
  .pill-chip      { background: rgba(127,127,127,.13); opacity: .85; font-weight: 500; }
  .rtl { direction: rtl; text-align: right; unicode-bidi: plaintext; }
  .answer-box {
    direction: rtl; text-align: right; unicode-bidi: plaintext;
    background: rgba(99,102,241,.07); border-inline-start: 3px solid rgba(99,102,241,.6);
    border-radius: 10px; padding: .7rem 1rem; font-size: 1.02rem; white-space: pre-wrap;
  }
  .notfound-box {
    direction: rtl; text-align: right;
    background: rgba(127,127,127,.08); border-inline-start: 3px solid rgba(127,127,127,.4);
    border-radius: 10px; padding: .7rem 1rem; opacity: .75;
  }
  .stButton > button[kind="primary"] {
    background: linear-gradient(90deg, #6366f1, #14b8a6);
    border: none; font-weight: 700; border-radius: 10px;
  }
  section[data-testid="stSidebar"] .stCaption, .sidebar-models { font-size: .8rem; }
</style>
""", unsafe_allow_html=True)


def score_pill(score: int) -> str:
    cls = "pill-score-hi" if score >= 4 else ("pill-score-mid" if score == 3 else "pill-score-lo")
    return f'<span class="pill {cls}">ציון {score}/5</span>'


def kpi(label: str, value: str, note: str = "") -> str:
    return (f'<div class="kpi-card"><div class="kpi-label">{html.escape(label)}</div>'
            f'<div class="kpi-value">{html.escape(value)}</div>'
            f'<div class="kpi-note">{html.escape(note)}</div></div>')


def render_results(outcome: dict, provider_label: str) -> None:
    results = outcome["results"]
    diagnostics = outcome["diagnostics"]
    page_map = outcome["page_map"]

    failed = diagnostics.get("extraction_errors") or []
    if failed:
        st.error(
            f"These parameters FAILED to extract (an API error, **not** 'not found'): "
            f"{', '.join(failed)}. Re-run to retry them — tagging is cached."
        )

    grounded = sum(diagnostics["groundedness"].values())
    answered = sum(1 for v in results.values() if v["answer"])
    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(kpi("Pages / parameter", f'{diagnostics["avg_pages_sent"]}',
                    f'of {diagnostics["document"].get("pages", "?")} pages — lower is better'),
                unsafe_allow_html=True)
    c2.markdown(kpi("Grounded citations", f"{grounded}/{answered}",
                    "quote verified on the cited page, no LLM"), unsafe_allow_html=True)
    c3.markdown(kpi("LLM calls", f'{diagnostics["llm_calls"]}',
                    f'{diagnostics["cache_hits"]} from cache'), unsafe_allow_html=True)
    c4.markdown(kpi("Wall clock", f'{outcome["elapsed"]:.0f}s', provider_label),
                unsafe_allow_html=True)

    st.write("")

    for name, value in results.items():
        found = bool(value["answer"])
        agreement = diagnostics["agreement"].get(name, "—")
        pages = page_map.get(name, [])

        head = (
            f'<div class="param-head">'
            f'{score_pill(value["score"])}'
            f'<span class="param-source">{html.escape(value["source"])}</span>'
            f'<span class="pill pill-chip">{html.escape(agreement)}</span>'
            f'<span class="pill pill-chip">{len(pages)} עמודים נשלחו</span>'
            f'<span class="param-name">{html.escape(name)}</span>'
            f'</div>'
        )
        body = (f'<div class="answer-box">{html.escape(value["answer"])}</div>'
                if found else '<div class="notfound-box">לא נמצא</div>')
        st.markdown(f'<div class="param-card">{head}{body}</div>', unsafe_allow_html=True)

        if found and value["details"]:
            with st.expander("פירוט"):
                st.markdown(f'<div class="rtl">{html.escape(value["details"])}</div>',
                            unsafe_allow_html=True)

    st.divider()
    tab_json, tab_pages, tab_diag = st.tabs(["results.json", "parameter → pages", "diagnostics"])
    with tab_json:
        st.json(results)
        st.download_button(
            "⬇︎ Download results.json",
            json.dumps(results, ensure_ascii=False, indent=2).encode("utf-8"),
            file_name="results.json", mime="application/json",
        )
    with tab_pages:
        st.json(page_map)
    with tab_diag:
        st.json(diagnostics)


# ─── Sidebar: document + provider ────────────────────────────────────────────
def render_monitoring() -> None:
    """KPIs across every recorded run — reads output/runs.jsonl (see record_run)."""
    import pandas as pd

    registry = solution.OUTPUT_DIR / "runs.jsonl"
    if not registry.exists():
        st.info("No runs recorded yet — every pipeline run appends itself to "
                "`output/runs.jsonl` automatically.")
        return
    rows = [json.loads(line) for line in registry.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if not rows:
        st.info("runs.jsonl is empty.")
        return

    frame = pd.DataFrame([{k: v for k, v in r.items() if k != "results"} for r in rows])
    frame["ts"] = pd.to_datetime(frame["ts"])
    frame["_file_i"] = frame.index                 # original jsonl position, for drill-down
    frame = frame.sort_values("ts").reset_index(drop=True)
    frame["run"] = frame.index + 1

    latest = frame.iloc[-1]
    st.markdown(
        kpi("runs recorded", str(len(frame)))
        + kpi("latest: found", f"{latest['found']}/7",
              f"{latest['provider']} · {latest['pdf']}")
        + kpi("latest: grounded", f"{latest['grounded']}/7")
        + kpi("latest: avg pages/param", f"{latest['avg_pages']}"),
        unsafe_allow_html=True,
    )
    st.write("")

    quality_col, econ_col = st.columns(2)
    with quality_col:
        st.markdown("**Answer quality** — `found` and `grounded`, of 7")
        st.caption("found = parameters with a non-empty answer. grounded = the model's verbatim "
                   "quote really appears on the pages it cites (deterministic check — the "
                   "hallucination guard). A gap between the lines means answers whose citation "
                   "could not be verified; found=0 means the model produced nothing usable.")
        st.line_chart(frame, x="run", y=["found", "grounded"], height=220)
    with econ_col:
        st.markdown("**Economy** — avg pages sent per parameter")
        st.caption("The assignment's explicit goal: how few pages each answer needed, out of the "
                   "whole document. Flat ≈3 across providers and documents means the router — not "
                   "the model — controls cost. A jump here would mean routing degraded.")
        st.line_chart(frame, x="run", y=["avg_pages"], height=220)

    tok_col, call_col = st.columns(2)
    with tok_col:
        st.markdown("**Tokens per run** — input / output")
        st.caption("What the run actually paid. Input dominates and tracks cache state, not "
                   "model quality: a cold run re-reads every page, a warm one only pays for "
                   "extraction. Compare runs on the same document to see caching work.")
        st.line_chart(frame, x="run", y=["input_tokens", "output_tokens"], height=220)
    with call_col:
        st.markdown("**Calls vs cache hits**")
        st.caption("Of ~95 calls per run, up to 81 are page-tags servable from the SQLite cache. "
                   "cache_hits near llm_calls = a warm re-run costing only the 14 extraction/judge "
                   "calls; cache_hits at 0 = first contact with this document+model pair.")
        st.line_chart(frame, x="run", y=["llm_calls", "cache_hits"], height=220)

    # ── a computed reading of the data on screen, not canned text ──
    best = frame.loc[frame["found"].idxmax()]
    worst = frame.loc[frame["found"].idxmin()]
    err_runs = frame[frame["errors"] > 0]
    warm = frame[frame["cache_hits"] > 0]
    lines = [
        f"**Best run:** #{best['run']} ({best['provider']} · {best['pdf']}) — "
        f"{best['found']}/7 found, {best['grounded']}/7 grounded.",
        f"**Worst run:** #{worst['run']} ({worst['provider']}) — {worst['found']}/7 found. "
        + ("Its `errors` column is non-zero, so this is quota/transport failure, not model "
           "opinion — failed calls are never disguised as 'not found'."
           if worst["errors"] > 0 else
           "Zero failed calls — the model genuinely found nothing; see `absence_contested` "
           "in its diagnostics for whether the tagger disagreed."),
    ]
    if len(err_runs):
        lines.append(f"**{len(err_runs)} of {len(frame)} runs had failed calls** (⚠ quota or "
                     "transport). Their low scores measure availability, not extraction quality.")
    if len(warm):
        saved = int((warm["cache_hits"] / warm["llm_calls"]).mean() * 100)
        lines.append(f"**Caching:** {len(warm)} warm runs served ~{saved}% of their calls from "
                     "the page-tag cache — the 'tag once, route many' economics, visible live.")
    st.markdown("#### Reading the current data")
    for line in lines:
        st.markdown("- " + line)

    st.caption("Every run, newest first. `elapsed_s` is empty on rows backfilled "
               "from artifacts that predate the registry.")
    st.dataframe(
        frame.sort_values("ts", ascending=False)[
            ["ts", "pdf", "provider", "extractor", "found", "grounded", "score_total",
             "avg_pages", "llm_calls", "cache_hits", "errors", "elapsed_s"]
        ],
        use_container_width=True, hide_index=True,
    )

    st.divider()
    newest_first = frame.iloc[::-1].reset_index(drop=True)
    labels = [f"#{r['run']} · {r['ts']:%d.%m %H:%M} · {r['provider']} · {r['pdf']}"
              for _, r in newest_first.iterrows()]
    picked = st.selectbox("Inspect a run", labels)
    row = rows[int(newest_first.loc[labels.index(picked), "_file_i"])]
    detail = pd.DataFrame([
        {"parameter": name, "answer": v["answer"], "source": v["source"], "score": v["score"]}
        for name, v in row.get("results", {}).items()
    ])
    st.dataframe(detail, use_container_width=True, hide_index=True)


with st.sidebar:
    st.header("📄 Document")
    uploaded = st.file_uploader("Upload a tender PDF", type="pdf")
    st.caption(
        "First run on a new document tags every page — this is the slow step. "
        "Re-runs hit the page-tag cache and take seconds."
    )

    st.divider()
    st.header("🤖 Model provider")
    PROVIDER_CHOICES = {                       # label → solution.setup_llm provider name
        "Gemini": "gemini",
        "Claude (Anthropic)": "anthropic",
        "Local (LM Studio)": "local",
    }
    provider_name = PROVIDER_CHOICES[st.radio(
        "Provider",
        tuple(PROVIDER_CHOICES),
        help="Every pipeline role runs on the chosen provider. "
             "Local runs on this machine: no key, no network, no cost.",
    )]

    if provider_name == "local":
        # No key to collect. setup_llm takes api_key as an optional LM Studio base-URL
        # override, so the same argument carries the only setting local mode has.
        default_base = os.environ.get("LMSTUDIO_BASE_URL") or "http://localhost:1234/v1"
        api_key = st.text_input(
            "LM Studio base URL",
            placeholder=default_base,
            help="Leave blank for the default. The server must already be running.",
        ).strip() or None
        st.caption(f"No API key needed. Serving from `{api_key or default_base}` — "
                   "start it with `lms server start`.")
    else:
        env_key = (os.environ.get("ANTHROPIC_API_KEY") if provider_name == "anthropic"
                   else os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        is_anthropic = provider_name == "anthropic"
        key_input = st.text_input(
            "Anthropic API key" if is_anthropic else "Gemini API key",
            type="password",
            placeholder="sk-ant-..." if is_anthropic else "AIza...",
            help="Used for this session only — never written to disk.",
        )
        api_key = key_input.strip() or env_key

        if key_input.strip():
            st.caption("Using the key entered above.")
        elif env_key:
            st.caption("Using the key from `.env`.")
        else:
            st.warning("No API key — the pipeline will run in stub mode (no LLM calls).")

    roles = solution.PROVIDER_MODELS[provider_name]
    st.markdown(
        '<div class="sidebar-models">'
        + "".join(f"<div><code>{role}</code> · {html.escape(model)}</div>"
                  for role, model in roles.items())
        + "</div>",
        unsafe_allow_html=True,
    )

# ─── Main ────────────────────────────────────────────────────────────────────
st.markdown('<p class="hero-title">חילוץ פרמטרים ממכרז</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="hero-sub">Each parameter is routed to the few pages that can answer it — '
    'only those pages reach the model.</p>',
    unsafe_allow_html=True,
)
st.write("")

tab_extract, tab_monitor = st.tabs(["✨ Extract", "📊 Monitoring"])

with tab_monitor:
    render_monitoring()

with tab_extract:
    if uploaded is None:
        st.info("⬅️ Upload a tender PDF in the sidebar to begin.")
    elif st.button("✨ Extract parameters", type="primary"):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as handle:
            handle.write(uploaded.getbuffer())
            pdf_path = Path(handle.name)

        llm = solution.setup_llm(provider_name, api_key)
        status = st.status(f"Working… ({llm.name})", expanded=True)
        try:
            outcome = asyncio.run(
                solution.process_document(pdf_path, on_stage=lambda m: status.write(m), llm=llm)
            )
            status.update(label=f"Done in {outcome['elapsed']:.0f}s · {llm.name}",
                          state="complete", expanded=False)
            st.session_state["outcome"] = outcome
            st.session_state["provider_label"] = llm.name
        except Exception as exc:                   # surface the real reason, don't swallow it
            status.update(label="Failed", state="error")
            st.exception(exc)
        finally:
            pdf_path.unlink(missing_ok=True)

    if "outcome" in st.session_state:
        render_results(st.session_state["outcome"], st.session_state.get("provider_label", ""))
