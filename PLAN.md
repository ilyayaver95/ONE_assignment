# Tender Parameter Extraction — Architecture & Execution Plan

**Problem.** Extract a fixed set of parameters from a long Hebrew tender (מכרז) PDF, precisely,
cheaply, and with verifiable citations.

**Thesis.** For structured legal documents, *page-level structured routing* beats chunk-level
embedding similarity. We tag every page once with an LLM, index those tags, route each parameter
to a small set of pages, and extract only from those pages. No vector database, no cosine
similarity, no chunking hyperparameters.

**Why this is not just "cheaper".** Tagging every page costs roughly the same input tokens as
reading the document once. The wins are:
1. **Amortization** — one read serves all N parameters (and any future parameter).
2. **Precision** — the extractor sees 3 relevant pages, not 300 pages of noise.
3. **Traceability** — every value carries a page number and a verbatim quote.
4. **Auditability** — routing is an inspectable, measurable classifier, not an opaque embedding space.

---

## 1. Pipeline

```
PDF ──▶ [1] Ingest ──▶ [2] Prefilter ──▶ [3] Tag ──▶ [4] Route ──▶ [5] Extract ──▶ [6] Verify ──▶ [7] Report
        PyMuPDF        Hebrew            [tagger]     scores +      [extractor]    [judge] +       JSON / Excel / PDF
        + RTL norm     gazetteer         1 call/page  window        prompt         grounding       + Streamlit
                       (regex)           (cached)     expansion     families       check
```

### Stage 1 — Ingest
- PyMuPDF (`fitz`) → one `Page` object per physical page, 1-indexed.
- Hebrew normalization: strip niqqud, normalize final forms for matching only (ך/כ, ם/מ, ן/נ, ף/פ, ץ/צ),
  normalize digits, currency (₪ / ש"ח / שקל), whitespace, and Hebrew/Arabic numeral mixing.
  **Keep the raw text too** — quotes must be cited verbatim from raw.
- Deterministic structural features (no LLM): `char_count`, `has_table`, `is_toc`,
  section/appendix id parsed from headers (`נספח א'`, `סעיף 4.2`).
- **RTL correctness is a real test**, not an assumption: assert known Hebrew strings from the dev
  PDF extract in the correct character order.

### Stage 2 — Lexical prefilter (the cheap tier of the cascade)
A curated Hebrew gazetteer per parameter, written as prefix-tolerant regex (Hebrew attaches
ו/ה/ב/ל/כ/מ/ש as prefixes, so match `\S*ערבות` not `\bערבות\b`). No stemmer — semantics are the
LLM's job; the gazetteer is a **high-recall net**, tuned for recall ≈ 1.0, not precision.

Pages with zero lexical signal AND no structural signal are tagged at reduced depth (shorter prompt,
fewer output fields). Pages are never dropped entirely — that would cap recall permanently.
The prefilter's rejection rate and its recall against the oracle are both reported KPIs.

### Stage 3 — Page tagging (one LLM call per page)
One call per page — **not** batched. Batching's only real benefit was amortizing the prompt prefix,
and a per-page result cache does that better without muddying per-page attribution.

Each call sees: the page's raw text + ~200 chars of the previous page and ~200 of the next
**as context only** (this rescues continuation pages, e.g. a bare numbered list whose heading
was on the previous page).

Output — a three-layer `PageTags` record via **structured outputs** (`output_config.format`), never
regex-parsed JSON:

| Layer | Fields | Purpose |
|---|---|---|
| Structural | `page_role` (cover/toc/body/appendix/form/signature), `section_title` | Disambiguation — see the appendix trap below |
| Closed-set | `relevance[]`: per parameter a 0–3 score + short Hebrew evidence snippet | Routing — this is what makes routing a *measurable classifier* |
| Open-ended | `summary`, `topics[]`, `entities` (dates, ₪ amounts, %, orgs, durations) | Generalization — routes a *new, unseen* parameter without re-tagging |

**Caching — what was planned vs. what was built.** The plan called for provider-side *prompt*
caching of the stable ~2–3K-token prefix. **That was not implemented.** What exists instead is a
SQLite **result** cache keyed by `sha256(page_text | prompt_version | model)`: a re-run of the same
document costs nothing at all rather than ~10% of the prefix. It is the stronger win for repeated
runs and it makes the pipeline resumable after a rate-limit failure, but it does *not* reduce the
cost of the first pass, which prompt caching would have. `provider_cached_tokens` is reported in
the diagnostics and is always 0 — stated rather than hidden.

**Concurrency**: `asyncio` + bounded semaphore (default 8). This is the latency lever.

**Caching/idempotency**: results keyed by `sha256(page_text + prompt_version + model)` in SQLite.
Re-runs cost $0 and the pipeline is resumable.

> **The appendix trap.** `bid_guarantee` (ערבות) almost always appears twice: the binding clause in
> the body, and a template restating the amount in a נספח. Naive routing returns the appendix — a
> wrong-but-plausible answer. `page_role` lets the router prefer `body` over `form`/`appendix`, and
> lets the extractor report `ambiguous` when they genuinely conflict.

### Stage 4 — Routing
For each parameter: rank pages by relevance score, apply a per-parameter threshold and top-k cap,
break ties with `page_role` priority and lexical density.

**Minimum pages is an explicit goal**, not a nice-to-have: *"view sending a minimum of pages per
parameter as one of the goals."* So `pages_sent_per_parameter` is a headline KPI, and top-k is kept
tight (default 3–4).

**Window expansion is therefore CONDITIONAL, not automatic.** Unconditional ±1 expansion would triple
pages sent and work directly against a graded objective. Expand only on a detected truncation signal:
the page ends mid-sentence, a numbered list or table continues past the page edge, or there is no
terminal punctuation. Off by default, logged when it fires. Explicit `[עמוד N]` markers keep citations
resolving to exact pages.

### Stage 5 — Extraction
The 7 parameters are not one problem — they are three, and prompt families follow that split:

| Family | Parameters | Shape |
|---|---|---|
| `metadata` | `client_name`, `tender_name` | Cover-page entity, ~free |
| `atomic` | `contract_period`, `bid_guarantee`, `idea_author` | Typed single-clause fact |
| `list_or_table` | `threshold_conditions`, `evaluation_method` | Multi-page, list/table-valued — the hard ones |

Each parameter gets its **own typed Pydantic schema**, not a text blob:
`bid_guarantee → {amount, currency, valid_until, guarantee_type}`;
`threshold_conditions → list[Condition]`;
`evaluation_method → list[Criterion{name, weight_pct}]` (weights should sum to 100 — a free
validation check).

Every result carries `status: found | not_found | ambiguous`. `not_found` is a **first-class value**,
never an empty string. `ambiguous` is used when the document genuinely conflicts with itself.

### Stage 6 — Verification & calibrated confidence
Four independent signals, deliberately not all LLM-based:

1. **Citation groundedness** (deterministic, no LLM, no labels) — every value carries a verbatim
   Hebrew quote; verify by normalized fuzzy match that the quote actually appears on the cited page.
   This catches hallucination cold and is the single most valuable metric in the project.
2. **Cross-model verification** — a *different* model (Sonnet 5) re-extracts from the same pages.
   Same-model self-checking correlates its own errors; cross-model disagreement is real signal.
3. **Self-consistency** — k=3 runs at varied effort; measure answer stability.
4. **Routing confidence** — the tagger's relevance score for the winning pages.

**Score: follow the spec.** The assignment says *"ask the model to do this"*, and the dual-run
comparison (their step 6) is what informs it. So the judge model returns the 1–5 directly.

The four signals above are still computed and shipped as a **secondary `diagnostics` field** —
groundedness, agreement, self-consistency, routing confidence — so any score can be explained in the
video. This is additive, never a substitute for the required contract. (An earlier draft of this plan
made the composite the primary score; that contradicted the spec and was corrected.)

### Stage 7 — Reporting

**Their contract is the interface — everything internal renders down to it.** Confirmed against the
supplied example: a **dict keyed by the English parameter name**, each value holding **exactly four
keys**, all content in Hebrew.

```json
{
  "client_name": {
    "answer":  "רשות המים והביוב",
    "details": "תאגיד עירוני שהוקם לפי חוק תאגידי מים וביוב",
    "source":  "עמוד 2, פסקה ראשונה",
    "score":   5
  },
  "idea_author": { "answer": "", "details": "", "source": "לא נמצא", "score": 0 }
}
```

Four things the example settles that the prose did not:

1. **`source` is a human-readable Hebrew string, not a list of page numbers.** `"עמוד 2, פסקה ראשונה"`,
   `"עמוד 1, כותרת"` — page plus an optional intra-page locator. So a `format_source_hebrew()` helper
   handles singular/plural (`עמוד` / `עמודים`), contiguous ranges (`עמודים 12-14`), scattered pages
   (`עמודים 2, 7, 40`), and `"לא נמצא"`. The extractor returns an optional `locator` field
   (`כותרת` / `פסקה ראשונה` / `טבלה`) which is appended when present.
2. **The `parameter -> [2,3,7,8,40]` page map is a SEPARATE deliverable** from `source` — the spec
   requires both. Printed and saved alongside the results.
3. **`score` is 0 when not found**, despite the prose saying 1–5. The example is the concrete artifact,
   so follow it — as a single `NOT_FOUND_SCORE = 0` constant, with the ambiguity noted in the README
   and video. One-line change if they meant otherwise.
4. **Exactly four keys — so diagnostics do NOT go here.** Groundedness, agreement, self-consistency,
   pages-sent and cost land in a **separate `output/diagnostics.json`**, shown in the video. Polluting
   the required contract with extra keys risks breaking *"must match the format of the existing pipeline."*

`answer` and `details` are empty strings when not found. `details` is genuinely distinct from `answer`
— interpretation and expansion, not a restatement (their example: answer = the body's name,
details = the law it was incorporated under).

Saved as UTF-8 JSON (`ensure_ascii=False`) plus a console table. **No PDF renderer** — the spec
explicitly excuses it.

---

## 2. Evaluation — right-sized for the assignment

> Spec: *"The emphasis is on the structure of the solution, deep thinking, and not just on the accuracy."*

Reference answers for the dev tender **are provided in the assignment zip**. So the earlier
"no labeled data" workaround (oracle mode, silver labels, PR curves, a labeling app) is over-built
for this deliverable and is dropped. What stays is cheap, runs inside the one script, and is what a
reviewer actually finds impressive:

### Runs on every document, no labels needed
- **Citation groundedness** — each extracted value carries a verbatim Hebrew quote; verify by
  normalized fuzzy match that it really appears on the cited page. Deterministic, no LLM in the loop,
  and the one signal a model cannot flatter itself into passing. Highest value per line of code here.
- **Negative control** — `idea_author` must return `"לא נמצא"`. Treated **identically** to every other
  parameter, with zero special-casing anywhere in the code (spec: *"Do not write this to the model,
  rather send the prompt and pages as usual"*). Any hardcoded decoy handling is a red flag to a reviewer.
- **Pages sent per parameter** — a graded objective, so it is measured and printed.
- **Cross-model agreement** — their step 6, and the basis of the score.
- **Cost / tokens / wall-clock per stage.**

### Validated against the provided answers
Use the supplied answer file as a **correctness checklist** for the dev tender — not as a tuning target.
Tuning prompts until they match 7 known answers is exactly how you overfit to one PDF, and the second
tender exists to catch that.

### Holdout
`tender_test.pdf` is never used for prompt or threshold tuning. All the metrics above run on it
unmodified. Spec-relevant because *"in reality, a tender can also be 800 pages"* — the design must
survive a document it has not seen.

### Ablation (one measured claim, not a framework)
Routed pipeline vs. sending the whole document per parameter: tokens, wall-clock, and cost.
Reported in tokens and seconds as well as dollars — on a free tier the dollar axis collapses to ~0,
and the token/latency ratios are what carry the argument anyway.

## 3. Tech stack — and what is deliberately excluded

| Concern | Choice |
|---|---|
| Runtime | Python 3.12+ (venv is on 3.14 — pin down for PyMuPDF wheels) |
| Schemas / config | `pydantic` v2, `pydantic-settings` |
| LLM | Thin `Provider` protocol + adapters (Gemini now, Anthropic later); schema-constrained output; `tenacity` retries; `asyncio` + semaphore |
| PDF in | PyMuPDF |
| Persistence | SQLite (page-tag cache + telemetry) |
| Output | UTF-8 JSON (`ensure_ascii=False`) + console table. No PDF renderer — explicitly excused by the spec |
| Quality | `ruff` + type hints throughout; a few plain asserts for the RTL and grounding checks. **No** Docker / pre-commit / cassette suite — out of scope for a single-script deliverable |
| Observability | Own SQLite telemetry (source of truth) + **optional** Langfuse decorator layer |

### Models — addressed by ROLE, never by vendor name

Roles live in `config/settings.yaml`; `src/` never names a model. Switching provider is a config edit.

| Role | Dev (Gemini free tier) | Later (Anthropic) | Why this tier |
|---|---|---|---|
| `tagger` (~200 calls/doc) | Flash-class | `claude-haiku-4-5` | Bulk, short structured output |
| `extractor` | Pro-class | `claude-opus-5` | Few calls; Hebrew legal nuance justifies the tier |
| `judge` | Flash-class (≠ extractor) | `claude-sonnet-5` | Independent errors — see the caveat below |
| `oracle` (eval only) | Pro-class, no cascade | `claude-opus-5`, effort=high | Reference for silver labels |

Exact model IDs and free-tier rate limits are verified against provider docs at implementation time,
not assumed — they change often.

### Provider abstraction

```python
class Provider(Protocol):
    async def complete_structured(
        self, prompt: str, schema: type[T], model: str, **kw
    ) -> tuple[T, Usage]: ...
```

Two thin adapters (`GeminiProvider`, `AnthropicProvider`) behind one narrow interface. Provider-agnostic
core, adapters at the edge. Deliberately **not** LiteLLM: one more dependency whose internals would have
to be understood and defended, for an interface that is ~60 lines.

`Usage` is normalized across providers (`input_tokens`, `output_tokens`, `cached_tokens`, `latency_ms`,
`cost_usd`) so the KPI layer is provider-independent. Per-provider rates live in `llm/pricing.py` as a
config table.

**Two honest caveats, to be stated in the README:**
1. **The result cache is keyed by model name**, so switching provider or tier invalidates every
   entry and the next run pays a full cold pass.
2. **On a single provider, cross-model verification degrades to cross-*size* verification** (Flash vs.
   Pro). Same family, correlated failure modes, weaker independence than a true cross-vendor check.
   A real limitation — recorded, not hidden. Upgrading restores full independence.

### Excluded on purpose: LangChain
| LangChain component | Used instead | Why |
|---|---|---|
| `PromptTemplate` | Jinja2 files | Versioned, diffable prompts |
| `OutputParser` | Native structured outputs | The API *constrains* output; parsers retry-and-pray |
| Chains / LCEL | Plain functions | Readable stack traces |
| DocumentLoader | PyMuPDF | Hebrew RTL needs direct control |
| **VectorStore / Retriever** | **Nothing — not needed** | LangChain's core value is embedding retrieval. This project's thesis is that embedding retrieval is the wrong tool here. Importing it would be incoherent. |

### Observability
Langfuse is framework-agnostic and works without LangChain (verified against the current SDK): the
`@observe(as_type="generation")` decorator plus `update_current_generation(usage_details=...,
cost_details=...)`, with `start_as_current_observation` for nested pipeline spans and
`generation.score(...)` to push project KPIs into the same view as cost and latency.

Order of implementation: a thin `LLMClient` wrapper records
`(stage, model, prompt_version, tokens, cost, latency, cache_hit)` into **SQLite** — the source of
truth, fully owned, works offline, and is what the KPI notebook queries. Langfuse is added as an
optional ~30-line decorator layer for the visual trace UI; if the key is absent the pipeline runs
identically.

---

## 4. Deliverable shape — ONE script

> Spec: *"You must submit one script or one Jupyter notebook that runs the entire process end-to-end."*
> *"Preference will be given to modular thinking with functions. Meaning, a main function that calls
> small/medium functions."*

A multi-module package **violates the submission requirement**. The deliverable is a single
`solution.py`, decomposed into small, single-purpose functions with swappable parameters.

```
ONE_assignment/
├── solution.py            THE DELIVERABLE — runs end-to-end
├── param_config.json      side-car: gazetteer / prompt family / expected shape per parameter
├── .env                   API key
├── data/
│   ├── tender_sample.pdf  dev document (has reference answers)
│   ├── parameters.json    THEIRS — read it, never hardcode the list
│   └── tender_test.pdf    holdout
├── output/
│   ├── results.json       canonical output, ensure_ascii=False
│   └── cache.sqlite       page-tag cache (makes re-runs instant — critical for the video)
└── README.md              decisions + honest limitations
```

### Function decomposition inside `solution.py`

```python
def main():
    print("starting...")
    t0 = time.time()
    params      = load_parameters(path=PARAMS_JSON)
    pages       = load_document(file_name=PDF_PATH)
    pages       = enrich_pages_structurally(pages)
    candidates  = lexical_prefilter(pages, params, config=PARAM_CONFIG)
    tags        = tag_pages(pages, params, candidates, model=MODELS["tagger"])
    page_map    = match_parameters_to_pages(tags, params, max_pages_per_param=4)
    extractions = extract_all(page_map, pages, params, model=MODELS["extractor"])
    verified    = verify_and_score(extractions, pages, model=MODELS["judge"])
    results     = build_output(verified, params)
    save_and_print(results, path=OUTPUT_JSON)
    report_kpis(t0)
```

Every function takes explicit, swappable parameters — no hidden globals, no God-object. This shape
*is* the grading criterion, so it is designed first and everything else fits inside it.

**Anti-overfitting rule survives:** no page numbers or document-specific constants in the functions.
Everything document-specific lives in `param_config.json`, which must degrade gracefully for a
parameter it has never seen.

## 5. Phased execution

Everything lands in `solution.py`. Build in this order; each phase leaves the script runnable.

| Phase | Work | Deliverable | Est. |
|---|---|---|---|
| **0. Skeleton** | `main()` with every function stubbed, `param_config.json`, provider protocol + Gemini adapter, SQLite cache, usage/latency accounting | `python solution.py` runs end-to-end with stubs and prints timings | 0.5d |
| **1. Ingest** | PyMuPDF → page records, Hebrew normalization, structural features (`page_role`, `has_table`, `is_toc`, section id) | Pages extracted; **RTL correctness assert passes** on known strings | 0.5d |
| **2. Tag** | Gazetteer prefilter, tagging prompt, schema-constrained output, bounded async, SQLite result cache | Per-page tags for the dev tender; wall-clock measured | 1d |
| **3. Match** | Thresholds, tight top-k, `page_role` tie-breaks, conditional window expansion | `parameter -> [pages]` map — **a required output**, printed | 0.5d |
| **4. Extract** | Three modular prompt families, per-parameter shapes, `"לא נמצא"` path | Values with evidence quotes | 1d |
| **5. Verify & score** | Grounding check, second model, model-emitted 1–5, diagnostics | Full required output contract | 0.5d |
| **6. Polish** | README with decisions, KPI print-out, holdout run, ablation numbers | Submission-ready | 0.5d |
| **7. Video** | 7-minute unedited run: functions explained, output shown, debugger | The recording | 0.5d |

~4.5 days. Get a working end-to-end path by the end of Phase 3 — a complete rough pipeline beats
three polished stages and a stub.

## 6. KPIs

**Graded objectives first**
- **Pages sent per parameter** (minimise — an explicit assignment goal)
- Citation groundedness rate
- Negative-control correctness (`idea_author` → `"לא נמצא"`)
- Cross-model agreement rate
- Correctness vs. the provided answers (dev tender, n=7 — a checklist, not a percentage)

**Efficiency**
- Tokens and cost per document, and per parameter
- Cache hit rate
- Prefilter rejection rate **and its recall** — rejecting a page that held an answer is the one
  unforgivable prefilter error
- p50 / p95 latency per stage; **total wall-clock per document** (hard-capped by the 7-minute video)

**Ablation**
- Routed vs. whole-document baseline: tokens, wall-clock, cost

## 7. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Overfitting to one dev PDF | Second PDF is a strict holdout; all tuning lives in `config/`, never `src/`; label-free metrics run on unseen docs |
| Hebrew RTL extraction corruption | Explicit RTL unit test on known strings; raw text preserved for verbatim citation |
| Body/appendix duplicate values | `page_role` tagging; router preference; `ambiguous` status |
| Prompt-cache silently not hitting | Assert `cache_read_input_tokens > 0` in telemetry; alert if 0 |
| Clause split across a page break | Window expansion at extraction + neighbor context at tagging |
| LLM judge is itself wrong | Grounding check is deterministic and independent of any judge; Phase 8 validates the judge against humans |
| Prompts/thresholds tuned on Gemini regress after the Anthropic swap | Re-run the full eval suite on swap — the harness makes provider migration a *measured* event, not a leap of faith. Prompts are versioned per family, not per provider |
| **Total runtime blows the 7-minute unedited video** | Measure wall-clock from Phase 0. Bound concurrency to what the free tier tolerates, cache page tags in SQLite so a demo re-run is instant, and if a cold run is too slow, narrate a warm run and show the cold timing in the KPI print-out |
| Reference answers used as a tuning target → overfit to one PDF | Answers are a checklist only; `tender_test.pdf` stays a strict holdout; nothing document-specific in the functions |
| Free-tier rate limits throttle the async fan-out | Semaphore size and backoff are config values; `tenacity` handles 429s; SQLite cache means a throttled run resumes instead of restarting |

---

## 8. What to say in the README

The decisions worth defending explicitly, because they are what distinguish this from a prompt demo:
1. Page-level structured routing instead of embedding RAG — **and why**, for this document class.
2. No LangChain — a reasoned "no", not an unexamined one.
3. Confidence is *computed and calibrated*, not emitted by a model.
4. A deterministic, label-free groundedness check that does not depend on any LLM's judgment.
5. Evaluation honesty: silver labels named as such; n=14 reported as a checklist, not a percentage;
   the cross-size (not cross-vendor) verification limitation stated outright.
6. Role-based model addressing behind a thin provider protocol — the pipeline is not coupled to a vendor,
   and the eval suite is what makes swapping one safe.

---

## 9. Build log — measured, not estimated

### Phase 1 (ingest) — findings from the real tender
- RTL repair **does not fire**: this PDF stores logical order (0/81 pages flagged). The detector
  earns its place by proving the text is sound rather than assuming it.
- The running header is **8 lines on ~80/81 pages, 8.7% of all characters**, and it *contains* the
  client and tender names — so `client_name` matched 80/81 pages and `tender_name` 81/81 before it
  was handled. Lifted into `DocumentMeta` rather than discarded: `מי שבע` appears nowhere on page 1,
  only in the header from page 2 on, so stripping it outright would have made the parameter
  unanswerable.
- Bugs found by running, not reading: enrichment ran before boilerplate removal (59 pages misread as
  signature pages); the tab/pipe table heuristic never fired (replaced with PyMuPDF `find_tables()`,
  now 15 pages); section headings split number and title across lines, so a single-line regex only
  ever caught page numbers (now 55/81 pages carry a real title).

### Phase 2 (tagging) — measured on 81 pages
| Metric | Value |
|---|---|
| Cold run, concurrency 5 | **265 s**, 81 live calls |
| Warm run (cache hit) | **~2 s** |
| Tokens | 153,289 in / 25,869 out |
| Rate-limit failures, cold | 5/81 (429), retried on the next run |

**Prompt v1 → v2 was a real correction.** v1's appendix rule was too aggressive and demoted the
*binding clause* along with the template, so `bid_guarantee` had no score-3 page at all. v2 says the
body clause scores 3 even when the value repeats in an appendix; only an empty נוסח template scores 2.
Result: `bid_guarantee` → **3:[7]**, with 27/29/73 correctly demoted — and 7/29/73 are exactly the
three pages containing "10,000". The appendix trap is handled.

**Bug worth recording:** failed calls were being written to the cache, permanently memoising a
transient 429 as "this page has no content". `call_with_retry` now returns a success flag and only
successful results are cached.

**Open issue:** `client_name` still scores 3 on 13 pages — the tagger treats any mention of the
corporation as definitional, and the v2 prompt tweak did not fix it. Not on the critical path,
because the metadata family is answered from `DocumentMeta` with zero pages sent, but it should be
stated rather than hidden.

**Video implication:** a cold run is 4.4 minutes, which does not fit a 7-minute recording alongside
extraction and narration. Record against a warm cache and show the cold timing in the KPI print-out.

### Phases 3–4 (routing + extraction) — measured

**Result: 7/7 parameters correct against the supplied answers**, including `idea_author` → `לא נמצא`.

| Parameter | Extracted | Source | Pages sent |
|---|---|---|---|
| client_name | מי שבע - תאגיד אזורי למים וביוב בע"מ | עמוד 1, כותרת המסמך | 1 |
| tender_name | מכרז פומבי מס' 05/25 למתן שירותי תמיכה DBA… | עמוד 1, כותרת המסמך | 1 |
| threshold_conditions | 5 numbered conditions | עמודים 6-7, סעיף 3 | 6 |
| contract_period | 24 חודשים | עמודים 9, 44, סעיף 6.1 | 3 |
| evaluation_method | משקל מחיר 40%, משקל איכות 60% | עמוד 18, סעיף 11.2 | 5 |
| bid_guarantee | 10,000 ₪ | עמוד 7, סעיף 5 | 4 |
| idea_author | — | לא נמצא | 0 |

Average pages sent per parameter: **2.86**. Citation groundedness: **6/6** on found parameters
(verified deterministically, no LLM). The metadata family sends **1 page**, because the answer comes
from `DocumentMeta` rather than routed body pages.

**Two silent-failure bugs, same root cause, both fixed.** A rate-limited call returned an empty
schema object, which the pipeline then treated as truth — first by caching it as "this page has no
content", then by reporting a parameter as `לא נמצא` when its API call had simply failed. For a
system whose value proposition is trustworthy extraction, a failure that reads as a confident
negative is the worst possible failure mode. Now: `call_with_retry` returns a success flag, failures
are never cached, `Extraction.error` is distinct from `not_found`, and the run prints a loud warning
naming any parameter whose result is unreliable.

**Model quota is a real constraint, not a footnote.** `gemini-3.7-flash` has *no* free-tier quota —
it 429s on the first call, and the backoff dutifully retried it for minutes before failing. Extraction
now runs `gemini-2.5-flash` with a `gemini-3.5-flash-lite` fallback, since quotas are per-model.

**Window expansion was recalibrated twice.** "Ends without terminal punctuation" fired on 52/81 pages
and nearly doubled pages sent. Adding "and the next page does not open a new clause" first swung it
to 0/81 — because the printed page number at the top of each page was being read as a clause marker
(the same bare-integer bug that had already broken section titles). With page numbers stripped, it
fires on 36/81 genuinely-continuing pages, and `max_pages` is now a *final* budget applied after
expansion, so the graded pages-sent metric stays bounded.

`pages_actually_cited` is reported alongside `pages_sent`: the gap between them is the honest measure
of routing precision.

### Phase 5 (verification + score) — measured

Two independent signals per parameter: a **different model family** (judge `gemini-3.5-flash-lite`
vs extractor `gemini-2.5-flash`) and a **deterministic groundedness check** that involves no model
at all.

**The judge commits before it compares.** `Verdict` asks for `independent_answer` as its FIRST field,
then `verdict`, then `score`. Generation is autoregressive, so the judge must state its own reading
before it is asked whether it agrees — which limits anchoring on the answer under review. It does not
eliminate it, and that is stated rather than hidden.

**Score follows the spec** (*"ask the model to do this"*) with deterministic ceilings applied after:

```
score = judge_score
    capped at 2 if verdict == "disagree"
    capped at 3 if verdict in ("partial", "cannot_verify")
    capped at 3 if the cited quote is not verifiably on the cited page
```

The model supplies the number; evidence verifiable without any model caps it. A confident score
resting on an unverifiable quote is precisely what a reviewer of a legal extraction should distrust.

| Parameter | Verdict | Grounded | Score |
|---|---|---|---|
| client_name | agree | ✓ | 5 |
| tender_name | agree | ✓ | 5 |
| threshold_conditions | agree | ✓ | 5 |
| **contract_period** | **partial** | ✓ | **3** |
| evaluation_method | agree | ✓ | 5 |
| bid_guarantee | agree | ✓ | 5 |
| idea_author | agree (absence confirmed) | — | 0 |

**The one disagreement is the correct one.** The extractor answered `contract_period` as
`24 חודשים`, putting the extension options in `details`. The judge independently answered
*"24 חודשים … עם אופציה להארכה ב-3 תקופות נוספות"* — and the supplied reference answer is
`24 חודשים + אופציית הארכה`. So the judge is closer to ground truth than the extractor, and the
`partial` verdict flagged the single parameter that diverges from the reference, dropping its score
from 5 to 3.

This is not tuned away. The answer/details split is a deliberate schema choice; the reference merges
them. Both are defensible, and the verification stage surfacing the ambiguity is exactly its purpose.
Scores are no longer uniformly 5 — the signal discriminates.

`unverified` is a distinct agreement state from `disagree`, so a judge call that fails is never
mistaken for a real disagreement.

### Phase 6 (holdout, ablation, README) — measured

**Ablation** (token counts measured with the provider's tokeniser, not estimated):

| | routed | whole document per parameter |
|---|---|---|
| extraction | 24,348 | 568,189 |
| + one-off tagging | 190,328 | — |
| **end to end** | **214,676** | **568,189** (**−62%**) |

Per parameter: 3,478 vs 81,170 tokens. **Break-even at 2.4 parameters.** The honest framing is that
tagging costs roughly 2.3× a single document read — it is not free — but it is paid once and reused by
every parameter, including ones added later, while the baseline pays a whole document every time.
Reporting the 96% extraction-only saving without the tagging line would have been misleading.

**Holdout** (`tender_test.pdf`, 46 pages, never used for tuning): all 7 parameters resolved,
groundedness 6/6, average 2.86 pages sent. Two independent signs that nothing leaked from the dev
document:
- the evaluation weights are **reversed** between the tenders (60/40 quality/price vs 40/60), and the
  holdout answer follows its own document;
- the holdout has **no running header at all** (0 boilerplate lines, 0.2% of characters), so the
  `DocumentMeta` path finds nothing — and the metadata parameters still resolve from the cover page.
  The mechanism degrades gracefully rather than breaking.

`contract_period` drew the same `partial` verdict on both documents, which is consistent behaviour
rather than a one-off: the extractor puts the duration in `answer` and extensions in `details`, the
judge merges them.

**Cache accounting fixed.** A warm run previously reported 0 tagging tokens, understating the
pipeline's real cost. Cached entries now store the cold-run token counts, and the ablation computes
tagging cost from the prompts themselves so the number holds regardless of cache state.

**Runtime is quota-bound, not compute-bound.** Cold holdout run: 546 s for 67 calls, dominated by
rate-limit backoff. Warm runs are seconds. This is the constraint to design the 7-minute video around.

### Change: "not found" is scored on evidence, not a constant

The spec is internally inconsistent — its example shows `score: 0` for `idea_author`, its prose says
to score by *"the certainty level of the model that it is sure it did not find it."* The prose wins:
a confident absence is a correct answer and scores 5.

The interesting part is where that certainty comes from. It cannot come from the judge: a not-found
parameter is routed to zero pages, so the judge would be confirming an absence from an empty prompt.
It comes from the **tagging pass** — all 81 pages were examined and none matched. `attach_absence_evidence()`
carries `max_relevance` (best tagger score for that parameter on any page) and `coverage_complete`
into scoring:

| evidence | score |
|---|---|
| max_relevance 0, all pages tagged | 5 |
| max_relevance 1 | 4 |
| max_relevance >= 2 but nothing extracted | 2 — a conflict worth surfacing |
| judge says it DID find something | 2 |
| some pages failed to tag | 3 |
| the call failed | 1 |

Measured: `idea_author` scores 5 with `max_relevance = 0`; every real parameter reaches 3. An absence
that was never verified — a failed call, or incomplete page coverage — never scores high.

### Cleanup pass (before the interview-prep build)

Removed or resolved, so the code and the docs say the same thing:

- **Dead constants** `OUTPUT_JSON` / `PAGE_MAP_JSON` / `DIAGNOSTICS_JSON`, superseded by `--prefix`.
- **`locator_hints`** was loaded into `ParameterSpec` from config and never read — dropped from both.
- **`"ambiguous"` status and the `"signature"` page role** were documented in docstrings but never
  assigned anywhere. The comments now describe the states that actually exist. The bid-guarantee
  body-vs-appendix *conflict* case is handled by the tagger's 3-vs-2 scoring, not by a third status.
- **Orphaned computations now reach the diagnostics** rather than being discarded: `rtl_repaired`,
  `pages_with_section_title`, `pages_with_table`, `page_roles`, and the judge's `reason`.
  `find_tables()` costs a call per page, so its result should at minimum be visible.
- **`report_kpis()`** took a start timestamp reconstructed by subtracting a duration from `time.time()`.
  It now takes the duration.
- **Prompt caching** was described in this plan and never built; the section above now says so.

### Correction: a not-found score is the AGREEMENT, not the tagging evidence

Reported from a real run: `bid_guarantee` came back with `score: 0`, which the current code cannot
produce. Root cause was **a stale process** — the Streamlit app had been running since before the
scoring rewrite and was holding the old module, in which `NOT_FOUND_SCORE = 0` still existed.
Restarting it resolved that specific value. Worth remembering: Streamlit did not pick up the changed
import on its own.

The design point behind the report was correct and is now implemented. The spec says the certainty
score comes from *comparing the two runs*, so a not-found value is scored on agreement exactly as a
found one is — both models report it absent → 5. The previous version let `max_relevance` drag an
agreed absence down to 2, which meant two strong models reading the actual text could be overruled by
the cheapest model's page-level triage. The tagger loses that argument; the disagreement is now logged
as `absence_contested` in the diagnostics instead.

**A second bug surfaced in the same run.** Every found value scored 1 with `verdict: unverified` —
the judge calls had hit quota, and a failed judge was collapsing a correctly extracted, grounded value
to the lowest possible score. That is the same family as the two earlier silent failures: an
infrastructure problem masquerading as a quality judgment. `final_score()` now handles `unverified`
explicitly — a found value with a verified quote scores 3, unverified 2, and a not-found scores 3 —
because absence of confirmation is not evidence against the answer.
