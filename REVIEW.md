# REVIEW.md — audit of the tender-extraction solution

Audited against the assignment PDF (`~/Downloads/מבחן בית - one - Data science 5[52][64]/תאור תרגיל בית - סיניור דאטה סיינסטיסט .pdf`), read in full, pages 1–6. **Note: the `./assignment/` folder referenced in the audit brief does not exist in the project; the spec PDF lives only in Downloads.** Every finding is tagged **VERIFIED** (I ran it, output shown) or **READ** (inferred from code). All runs happened on 2026-08-25; the Gemini free-tier **daily quota was exhausted mid-audit**, which itself became evidence (see F-05, §9).

**Environment note:** each live-model claim below states whether the quota outage affected it. Deterministic checks (routing, parsing, caching, stress fixtures) ran with the StubProvider plus the warm content-keyed tag cache, which serves the *real* tags from previous live runs — so routing behaviour observed is real.

---

## Top findings (ordered by severity)

### F-01 · BLOCKER — the planted trap's "not expected to appear" hint is sent to the model — VERIFIED
- **Where:** `param_config.json:114` → injected at `solution.py:591-593` (tagging catalogue) and `solution.py:887` (extraction prompt).
- **Spec:** the PDF says of `idea_author`: *"אין לצפות שימצא במסמך… **לא לרשום זאת למודל**, אלא אמורים לשלוח פורמט ודפים כרגיל"* — do **not** tell the model; send prompt and pages as usual.
- **Code does:** the side-car description is `"…פרמטר שאינו צפוי להופיע במסמכי מכרז."` and it reaches **both** models. Actual prompt line captured:
  ```
  - idea_author (הוגה הרעיון): מי הגה את הרעיון שבבסיס המכרז. פרמטר שאינו צפוי להופיע במסמכי מכרז.
  ```
  and in the extraction prompt: `הגדרה: … פרמטר שאינו צפוי להופיע במסמכי מכרז.`
- **Why it matters:** this is the assignment's one explicit prohibition. With the hint in the prompt, the `לא נמצא` result proves nothing — the model was told the answer. The README's claim "*idea_author is not special-cased anywhere*" is false at the prompt layer.
- **Smallest fix:** change the description in `param_config.json` to a neutral one (e.g. `"מי הגה את הרעיון שבבסיס המכרז."`) and delete the trailing sentence. One JSON line; no code change. (Then bump `PROMPT_VERSION` — see F-07.)

### F-02 · BLOCKER — the "independent" second run sees the first model's answer — VERIFIED
- **Where:** `solution.py:995` and `solution.py:1001` (`build_verification_prompt`), consumed at `solution.py:1144-1146`.
- **Spec:** *"שליחה למודל נוסף… **השוואה בין התוצאות של שני הריצות**… שמירת ציון וודאות פייר פרמטר לפי ההשוואה"* — run again, then compare the two results.
- **Code does:** run 2 is a *review*, not a run. Captured payload tail:
  ```
  התשובה שהוצעה על ידי מודל קודם:
  10,000 ₪
  ```
  `FIRST MODEL ANSWER IN JUDGE PAYLOAD: True`
- **Mitigations that exist:** the `Verdict` schema asks for `independent_answer` first (autoregressive ordering), and the README admits the anchoring risk. Both models are different (2.5-flash vs 3.5-flash-lite), same vendor. Both runs get the same pages (`solution.py:1144` uses the same `page_map`) — VERIFIED by code path and by `judge_independent_answer` in diagnostics.
- **Why it matters:** the score is the deliverable's confidence signal; an anchored judge inflates agreement. The audit brief pre-declared this a BLOCKER, and the spec's own wording supports independent-run-then-compare.
- **Smallest fix:** two calls — first send the judge the *same extraction prompt* (no candidate answer) to get its independent value, then compare the two answers deterministically (or with a third cheap call). `build_verification_prompt` already has the pieces; drop the `proposed` block from the first call.

### F-03 · MAJOR — the trap parameter is sent **zero pages**, spec says "send prompt and pages as usual" — VERIFIED
- **Where:** `solution.py:743-744` (threshold filter yields no pages) → `output/page_map.json` `"idea_author": []` → `solution.py:882` renders `"(לא נמצאו עמודים רלוונטיים)"`.
- **Spec:** *"אמורים לשלוח פורמט ודפים כרגיל"*.
- **Code does:** the prompt *is* built and the extractor *is* called (same code path — grep confirms `idea_author` appears in **no** `.py` file, no skip list, no conditional), but the text body is a placeholder, not pages. Captured extraction prompt ends:
  ```
  הטקסט:

  (לא נמצאו עמודים רלוונטיים)
  ```
- **Why it matters:** combined with F-01, "not found" is produced by an empty prompt plus a leading hint. Even without F-01, an extractor that reads zero document text isn't demonstrating the not-found handling the tender asks for.
- **Smallest fix:** in `match_parameters_to_pages`, when a parameter has no page ≥ threshold, fall back to its best-scoring 1–2 pages (or the top lexical-prefilter pages) so every parameter always carries real text. ~4 lines in `solution.py:734-744`.

### F-04 · MAJOR — judge-failure path mis-scores; the dead branch that was supposed to handle it is unreachable — VERIFIED
- **Where:** `solution.py:1151` passes `"cannot_verify"`; the `verdict == "unverified"` branch at `solution.py:1023-1029` is dead code (no caller ever passes `"unverified"`).
- **Spec:** score reflects extraction certainty; a rate-limit is not low certainty (the code's own docstring at `solution.py:1024-1026` says exactly this).
- **Code does:** on judge failure, a **found, grounded** answer gets `min(UNVERIFIED_SCORE=1, 3) = 1`, and an absent one gets 4 — while docstring and README table promise 3/3. Observed in the shipped `output/results.json`: `threshold_conditions`, `contract_period`, `evaluation_method`, `bid_guarantee` all correct, all grounded, all **score 1**; `idea_author` score 4. Diagnostics confirm `agreement: "unverified"` for exactly those five.
- **Why it matters:** the deliverable on disk right now says the four hardest answers are worthless (1/5) when they are correct and grounded — a rate limit masquerading as a quality judgment, which is the exact failure the code comments say they prevent. The README results table (scores of 5) does not match the shipped file.
- **Smallest fix:** at `solution.py:1151` pass `"unverified"` instead of `"cannot_verify"` (one word). The existing branch at 1023 then does what the docstring promises.

### F-05 · MAJOR — a fully failed run silently overwrites the deliverable with all-"לא נמצא" and exits 0 — VERIFIED
- **Where:** `solution.py:1334-1342` (`run_pipeline` always saves), `solution.py:1454-1466` (`main` has no exit code).
- **What happened:** with the daily quota exhausted, `python solution.py` ran **613.7s** (warm tag cache!), every extractor/judge call failed, and `output/results.json` was replaced by seven `"לא נמצא"` entries, score 1 each — exit code 0. KPI print does warn (`!! FAILED (not 'not found')`), but the contract file itself is indistinguishable from a legitimate all-absent result.
- **Smallest fix:** in `run_pipeline`, if `diagnostics["extraction_errors"]` is non-empty, write to `results.failed.json` (or refuse to overwrite an existing good `results.json`) and `sys.exit(1)`. ~5 lines.
- (I restored your pre-audit `results.json` / `diagnostics.json` / `page_map.json` from a backup taken before running.)

### F-06 · MAJOR — after window expansion, the page budget is re-applied by *page number*, discarding top-ranked pages — VERIFIED
- **Where:** `solution.py:1310-1313`: `expand_windows_if_truncated(numbers, pages)[: budgets…]` — `expand_…` returns a **sorted** list, so the slice keeps the lowest-numbered pages, not the highest-ranked.
- **Observed:** `bid_guarantee` ranked `[7, 27, 29, 73]` (max_pages=4); expansion grew it to `[7,8,27,28,29,30,73,74]`; the cap then kept `[7,8,27,28]` — the ranked winners 29 and 73 were silently dropped in favour of continuation pages 8 and 28. Matches the shipped `output/page_map.json` exactly.
- **Why it matters:** routing quality is the graded core of this design; the final cut is currently made by an accident of pagination.
- **Smallest fix:** cap first, expand after — or expand only pages that survive the cap: swap the two operations at `solution.py:1310-1313` (and identically at `solution.py:1372-1376` in the ablation).

### F-07 · MAJOR — tag-cache key ignores the parameter catalogue, so an added/renamed parameter is served stale tags and silently routes to zero pages — VERIFIED
- **Where:** `solution.py:261-265` — key = `raw_text | PROMPT_VERSION:brief | model`. The specs catalogue (names + descriptions) is *in the prompt* but *not in the key*.
- **Observed (stress test):** appended `"delivery_terms"` to `data/parameters.json` (then restored). Run completed end-to-end with **no code change** (good), but 74/81 pages hit stale cache entries that never scored the new parameter → `delivery_terms: 0 pages, "לא נמצא"`. Renaming `bid_guarantee → guarantee_of_bid` produced the same silent zero-page routing.
- **Why it matters:** "add an eighth parameter with only a config change" is your headline modularity claim, and the cache quietly breaks it for any warm-cache run — including the live demo.
- **Smallest fix:** include a hash of the catalogue (`spec.name + description` for all specs) in `cache_key`'s material. One line at `solution.py:264`.

### F-08 · MAJOR — metadata parameters bypass routing entirely and can cite a page that does not contain the answer — VERIFIED
- **Where:** `solution.py:730-732` (`family == "metadata"` → `[meta.first_page]`, tag scores ignored); `solution.py:492-494` (`first_page` = first page carrying *any* boilerplate line).
- **Observed:** on the sample tender, `'מי שבע' in page 1 raw_text → False` (it first appears on page 2), yet the shipped result cites `"עמוד 1, כותרת המסמך"` for `client_name`, and the extraction prompt asserts the header appears "החל מעמוד 1". The cited page was *sent*, but the answer text is verifiably not on it; groundedness passes only because `check_grounded` also searches `meta.header_text` (`solution.py:1080-1081`).
- **Also:** on a non-tender document the metadata family still unconditionally sends page 1 (stress run: `client_name: [1]` on the moss guide) — for those two parameters the routing gates nothing.
- **Smallest fix:** compute `first_page` per *line* (the page where the client-name line first appears), or cite the header as such ("כותרת רצה, עמודים 2–81") instead of a page number. Contained in `build_document_meta` / `format_source_hebrew`.

### F-09 · MAJOR — no temperature and no max_tokens are ever set — VERIFIED
- **Where:** `solution.py:190-198` — `GenerateContentConfig` sets only `response_mime_type`, `response_schema`, AFC-off. `inspect` confirms: `temperature set: False | max_output_tokens set: False`.
- **Spec/audit expectation:** extraction should run at temperature ≈ 0 for determinism; Gemini 2.5-flash defaults to temperature 1.0.
- **Mitigating fact:** output is schema-constrained, and a truncated JSON fails validation → retry → error (never silent). Longest observed tag response 471 output tokens; longest extraction answer ≈ 500 tokens — far below the default output cap, so truncation is a theoretical risk here. Determinism, however, is real: two runs can produce materially different answers/scores, and there is **no response cache for extraction/judge** (only tags), so rerun variance is visible in the demo.
- **Smallest fix:** add `temperature=0` (and a generous explicit `max_output_tokens`) to the config at `solution.py:193-198`.

### F-10 · MAJOR — zero-page PDF crashes; corrupt PDF leaks a raw traceback — VERIFIED
- **Where:** `solution.py:481` (`best_page, best_hits = pages[0], -1` → `IndexError` on empty list); `solution.py:381` (`pymupdf.open` → `FileDataError` propagates unhandled).
- **Observed:** hand-built zero-page PDF → `IndexError: list index out of range`; corrupt file → `pymupdf.FileDataError` traceback. Neither is a typed, user-facing error.
- **Smallest fix:** after `load_document`, `if not pages: raise SystemExit(f"{pdf} has no readable pages")`; wrap `pymupdf.open` similarly. ~4 lines.

### F-11 · MAJOR — deliverability: the demo cannot survive a quota event, and the 7-minute video is at risk — VERIFIED (measured)
- **Evidence:** today, both models returned `429 RESOURCE_EXHAUSTED` (daily quota, not per-minute). A *warm-cache* run took **613.7s** — over 10 minutes — because 14 failing calls each ran the full 5-attempt exponential backoff (`solution.py:672-701`). Your own README says a cold run is "~4–9 minutes". The spec requires an **unedited ≤7-minute video**.
- Also: entry point is fine (`python solution.py` end-to-end, `.env` + `requirements.txt`, no hardcoded absolute paths — READ, confirmed by grep), but `main.py` is untouched PyCharm boilerplate that a reviewer may run first, and the audit brief's own `coverage run main.py` would measure nothing.
- **Smallest fix:** record the video against the warm cache on a day with fresh quota (or a paid key: `EXTRACTOR_FALLBACKS` + a second `.env` key is your escape hatch); delete `main.py` or make it `from solution import main`.

---

## 1. Requirement coverage (PDF § by §)

| PDF requirement | Where implemented | Status |
|---|---|---|
| Component takes tender PDF + parameter list as input | `solution.py:1445-1447` (`--pdf`), `load_parameters` :289 | ✅ |
| Cut document into pages; each page a separate unit | `load_document` :372-406 | ✅ VERIFIED |
| Tag each page: which parameters appear | `tag_pages` :611, `PageTags` :119 | ✅ VERIFIED |
| Optional one extra tag (topic by keywords / other) | `PageTags.summary/topics` :125-127 | ⚠️ produced but never consumed (§10) |
| Robust to synonyms, inflections, indirect phrasing, translation | `TAGGING_SYSTEM` :563-566, gazetteer prefixes :522-527 | ✅ READ |
| Recommended: smart LLM for tagging | `MODELS["tagger"]` :44 | ✅ |
| Map each parameter → relevant pages; e.g. `[2,3,7,8,40]` | `match_parameters_to_pages` :708, saved `page_map.json` | ✅ VERIFIED |
| If absent — explicit `"לא נמצא"` (string) | `NOT_FOUND_SOURCE` :59, `build_output` :1205 | ✅ VERIFIED byte-exact (`d7 9c d7 90 20 d7 a0 d7 9e d7 a6 d7 90`, no trailing space/quotes/niqqud) |
| Modular prompt per parameter, reusable across similar params (חשוב!!) | 3 families serve 7 params :823-852 | ✅ but see §13 (a param-specific line inside a family) |
| Send prompt + relevant pages per parameter | `extract_all` :893 | ⚠️ F-03: trap gets 0 pages |
| Error handling waived ("אפשר להתעלם") | retries exist anyway :672 | ✅ (over-delivered) |
| Save outputs in fixed structure; JSON file; Hebrew readable | `save_and_print` :1256-1258, `ensure_ascii=False` | ✅ VERIFIED |
| Second model, "עדיף מאוד מודל אחר"; compare **two runs**; score from comparison | `verify_and_score` :1117 | ⚠️ F-02: review, not a second run |
| Read the 7 params from the JSON, don't copy from the PDF | `load_parameters` reads `parameters.json` :297 | ✅ VERIFIED (grep: no key in any `.py` logic) |
| `idea_author`: don't tell the model; send prompt+pages as usual | — | ❌ F-01 + F-03 |
| Not found → `answer`/`details` empty, `source` = לא נמצא | `build_output` :1202-1206 | ✅ VERIFIED |
| Not-found score = model's certainty it did not find | `final_score` :1031-1046 | ⚠️ prose vs example conflict — see below |
| Output contract: answer / details / source / score (1–5) | `build_output` :1197 | ✅ VERIFIED |
| Output based on real document content | `check_grounded` :1058 | ✅ over-delivered |
| One script / one notebook, end-to-end | `solution.py` main :1454 | ✅ VERIFIED (2.2s stubbed, full path) |
| Modular main calling small/medium functions | `main` → `run_pipeline` → stages | ✅ 54 functions, longest 90 lines |
| Minimum pages per parameter (a stated goal) | cap + threshold + ablation | ✅ VERIFIED: 2.86 avg / 81 pages |
| Reality: tender can be 800 pages | per-page architecture | ✅ structurally VERIFIED at 810 pages |
| Unedited video ≤ 7 min | — | ⚠️ F-11 |

**Invented requirements (things the spec never asked for):** lexical prefilter, boilerplate stripping, groundedness check, SQLite cache, Streamlit app, ablation harness, RTL repair. None *conflict* with the spec, and several serve graded goals (page economy, structure) — but the Streamlit app and ablation are demo garnish; be ready to say why they exist.

**PLAN.md vs PDF — you asked to be told:** `PLAN.md:161-162` decides *"score is 0 when not found… the example is the concrete artifact, so follow it — as a single NOT_FOUND_SCORE = 0 constant"*. The code does the **opposite** (agreed absence scores 5, per the prose; `solution.py:1046`, README documents it). The PDF is internally inconsistent (prose: "score by certainty of absence", scale "1–5"; example: `"score": 0`). So: plan ≠ code, and whichever you pick, say it out loud in the video. If the grader diffs against the example, `score: 0` for `idea_author` is the safe choice — one constant in `final_score`.

## 2. Output contract — VERIFIED
Ran the pipeline (warm cache, stub extraction; plus the shipped live-run artifacts). `results.json` has exactly the four keys per parameter — `['answer','details','score','source']`, no extras; scores are `int` in 1–5; file is UTF-8 with `ensure_ascii=False` (Hebrew literal in file, verified by reading raw). Diagnostics live in `diagnostics.json`, page map in `page_map.json`; nothing leaked into the contract file. Differences from the spec's example: theirs shows `score: 0` for the absent parameter (see above) and a `details` style with interpretation — matched. `source` style "עמוד 2, פסקה ראשונה" — matched ("עמוד 1, כותרת המסמך").

## 3. The planted trap — F-01, F-03 (both VERIFIED, evidence above)
Grep results: `idea_author` appears in **zero** `.py` files (only in the two JSONs); no conditional, no skip list, no default keyed to it. The code path is genuinely identical — prompt built, model called — but the *data* smuggles the forbidden hint into the prompt (F-01) and routing hands it zero pages (F-03). Sentinel string verified byte-for-byte.

## 4. Modularity — VERIFIED (numbers), READ (assessment)
54 functions in `solution.py` (1470 LOC), longest `run_ablation` 90 lines (reporting), then `tag_pages` 59. No circular imports (`app.py → solution.py`, one direction; `solution.py` imports nothing local). Functions doing two jobs: `tag_pages` (cache policy + progress printing + calling), `run_ablation` (compute + format), `save_and_print` (write + print) — acceptable at this scale.
**Adding an eighth parameter changes:** `data/parameters.json` only (optionally `param_config.json` for keywords). **No `.py` file changes — VERIFIED end-to-end** (delivery_terms flowed to a result on `_default`). Two caveats that undercut the story: F-07 (stale cache starves the new parameter) and the fact that with no keywords the prefilter marks *all* pages as signal-bearing, so tagging runs at full depth everywhere (cost, not correctness — `solution.py:542-544`).
Behaviour-as-literal instead of config: `role_bonus` dict (`solution.py:726`), `density` divisor 600 (:740), `threshold=0.5` boilerplate cutoff (:459), judge/extractor concurrency (:901, :1125). MINOR.

## 5. Page economy — VERIFIED (measured, live tokenizer)
From today's `--ablation` run (real `count_tokens`, warm tag cache):
- Pages/parameter: 1, 1, 6, 3, 5, 4, 0 — **mean 2.86** of 81 pages (**3.5%** of the document per parameter).
- Extraction tokens: **24,348 routed vs 568,189 whole-doc (−96%)**; end-to-end incl. one-off tagging **214,676 vs 568,189 (−62%)**; break-even 2.4 parameters.
- Model calls per cold run: 81 tag + 7 extract + 7 judge = **95** (102 with retag stragglers in the shipped diagnostics).
Paths where the whole document could reach the model: **none found.** The only whole-document string (`solution.py:1378`) lives in `run_ablation` and is passed to `count_tokens`, never to `complete` — VERIFIED by reading every `provider.complete` call site (there are exactly three: tag :652, extract :911, judge :1146). No unbounded top-k (`scored[:spec.max_pages]` :744), no blanket ±1 expansion (conditional, :779-803 — though see F-06 for the cap ordering bug). The genuinely weak spot is not a fallback but the metadata family (F-08), which *always* sends page 1 even when nothing matched.

## 6. Hebrew correctness — VERIFIED
Printed page 1 of both tenders via the project's own loader: logical order, readable (sample cover: "מכרז פומבי… למתן שירותי תמיכה DBA… מרץ 2025"; test cover: "הרשות לשמירת הטבע והגנים לאומיים… מכרז פומבי (דו שלבי) מס' 2003-25"). `rtl_repaired=False` on both — correct, they are stored logically. The repair path itself was unit-verified on synthetic visual-order text (`"היצקנופ 28/2024 תקידב"` → `"בדיקת 28/2024 פונקציה"`, number preserved).
Normalization is used for matching only; prompts are built from `body_text` (raw minus boilerplate), and `check_grounded` searches `raw_text` — original text reaches the model. ✅
Citations: `extract_all` filters model-cited pages to the sent set (`solution.py:924`) — a cited-but-never-sent page is impossible **except** via the metadata path, where the cited `first_page` can lack the answer text (F-08). One more artifact: the shipped `threshold_conditions` answer contains an **Arabic** letter inside a Hebrew word — `הצهרה` (U+0647 ARABIC LETTER HEH) — VERIFIED. The model transcribed rather than copied; there is no output-sanitation pass. MINOR: a one-line confusable check (Arabic block inside Hebrew runs) in `build_output` would catch it.

## 7. Flow integrity — VERIFIED (trace below, real data)
`bid_guarantee`, sample tender, boundaries as actually passed:
1. **Ingest → `Page`** (dataclass): `Page(number=7, page_role='body', has_table=True, section_title='1. ניסיונו', char_count=1830)`
2. **Tag → `PageTags`** (pydantic): page 7 → `relevance=[('threshold_conditions', 3, 'תנאי סף מקצועי…'), ('bid_guarantee', 3, 'המציע צירף להצעתו ערבות ה…')]`
3. **Match → `dict[str, list[int]]`**: `[7, 27, 29, 73]`
4. **Expand → `[7,8,27,28,29,30,73,74]`** then cap → `[7,8,27,28]` (**F-06 fires here**)
5. **Extract → `Extraction`** (dataclass) → **Output → 4-key dict**.
No stage re-reads the PDF or reaches forward. Two integrity blemishes: (a) `pages` list is mutated in place across `strip_boilerplate`/`enrich_pages_structurally` (`solution.py:497-514, 432-456`) — READ, shared-object mutation, benign but stated; (b) **hidden global state: `CALLS` (`solution.py:250`) is never reset per document — VERIFIED**: across four `process_document` calls in one process, `llm_calls` climbed 824→919→938→955, so the Streamlit app reports cumulative KPIs from the second upload on. Smallest fix: `CALLS.clear()` at the top of `process_document`. The top-level boundary `process_document` returns a bare dict consumed with `.get(...)`/defaults in `app.py:50` — MINOR, type it if you care.

## 8. Leakage — VERIFIED
`answers_reference.json` / `.png` are read by **nothing** (grep over all `.py`: zero hits; only a docstring uses the word "answers"). No page number, issuer, tender number, date, or amount from any sample appears in code, prompts, or config — grep for `05/25`, `2003-25`, `מי שבע`, `טבע והגנים`, `פריוריטי`, `SQL` in `.py`/`param_config.json`: clean (the tender names appear only in README/PLAN, which are fine). The unseen test tender ran previously with a healthy quota: shipped `test_results.json` resolves all 7 (incl. `idea_author: לא נמצא`, score 5), avg 2.86 pages, agreement 6×agree/1×partial — VERIFIED artifacts, not rerun live today (quota).

## 9. Deliverability — VERIFIED
One entry point (`python solution.py`) runs end-to-end from a clean venv: `requirements.txt` complete (deptry: no issues), `.env.example` documents the only env var, paths are `Path(__file__).parent`-relative (works from any cwd — READ). Fresh-state run today: completed, exit 0, **613.7s** — but produced garbage because of quota (F-05, F-11). Undocumented: nothing, except that `main.py` is a decoy (PyCharm boilerplate) and the parameters path is not a CLI flag (`--pdf` exists, `--params` doesn't) — MINOR. **The 7-minute video does not fit a cold run** (README's own 4–9 min claim + backoff amplification measured today); it fits a warm-cache run (2.2s stubbed; seconds-to-~2min live) with the cold timing shown from the KPI log. One more: `.gitignore` covers `.env`, but this isn't a git repo — if you ship a **zip**, your real API key in `.env` ships with it. Check the zip.

## 10. Dead code and cruft — VERIFIED (raw output pasted)

```
=== RUFF (--select F,ARG,ERA,SIM,RUF --statistics) ===
3  RUF001  ambiguous-unicode-character-string
2  ARG002  unused-method-argument        (StubProvider.complete 'prompt' :228, count_tokens 'model' :232)
1  ARG005  unused-lambda-argument        (:1289 'message')
1  RUF002  ambiguous-unicode-character-docstring
1  RUF003  ambiguous-unicode-character-comment
1  RUF100  unused-noqa                   (:689)
Found 9 errors.

=== VULTURE (--min-confidence 60) ===
solution.py:24: unused import 'Callable' (90% confidence)
solution.py:97: unused variable 'header_lines' (60% confidence)
solution.py:125: unused variable 'summary' (60% confidence)
solution.py:126: unused variable 'topics' (60% confidence)
solution.py:1289: unused variable 'message' (100% confidence)

=== DEPTRY ===
Success! No dependency issues found.

=== COVERAGE (full pipeline, stub provider + warm tag cache; the brief's
    `coverage run main.py` would execute only PyCharm boilerplate) ===
solution.py  635 stmts  172 miss  73%
Missing: 182-184,187-214,219-222,233,242,274-278,306,353-357,388-389,396,445,525,
543-544,651-666,689-701,776,917,920,1021,1027-1029,1033,1035,1046-1055,1077-1093,
1142,1150-1152,1173-1177,1182-1194,1271-1272,1357-1438,1442-1451,1455-1466,1470
```

Classification of every miss/hit:
- **(a) DEAD — delete:**
  - `main.py` entire file (PyCharm boilerplate, 16 LOC) — and it shadows the real entry point in a reviewer's mind.
  - `solution.py:1023-1029` — the `verdict == "unverified"` branch; unreachable (F-04). Fix F-04 *or* delete, not both.
  - `solution.py:97` `DocumentMeta.header_lines` — written at :494, read nowhere (grep-verified).
  - `solution.py:306` — `raw.items()` dict-branch of `load_parameters`; their file is a list, no dict variant exists. Speculative generality.
  - Quoted annotation `"Callable[[str], None] | None"` at :1281 — with `from __future__ import annotations` the quotes (and hence the runtime `Callable` import flag) are unnecessary.
- **(b) DELIBERATE FALLBACK — keep, must be demonstrable:**
  - :689-701 retry/backoff — fires on 429; demonstrable *today* by simply running (quota exhausted). Comment exists.
  - :917-920 extractor fallback model — fires when `gemini-2.5-flash` fails and `EXTRACTOR_FALLBACKS` steps in; demonstrable by setting `MODELS["extractor"]="nonexistent-model"`.
  - :233, 242 `StubProvider`/`get_provider` live branch — fires with no `GEMINI_API_KEY`; demonstrable by `env -u GEMINI_API_KEY python solution.py` (I did; it runs in 2.2s).
  - :353-357, 396 RTL repair — fires on visual-order PDFs; neither sample triggers it. Unit-verified on synthetic text. If you can't produce such a PDF for the demo, be ready to show the unit call instead.
  - :388-389 `find_tables` except — fires on pages where MuPDF's table finder throws; concrete trigger is a malformed page object; low value, could also be dropped.
  - :1150-1152 judge-failure path — fired in the shipped run (agreement="unverified" ×5). Currently mis-scores (F-04).
- **(c) UNTESTED BUT LIVE — my run didn't hit it:**
  - :182-222 `GeminiProvider` — hit on any live run (quota-blocked today; the shipped diagnostics prove it ran).
  - :274-278 `cache_put`, :651-666 cold tag path — hit on any new document.
  - :1077-1093 `check_grounded` body, :1173-1194 locator/source formatting — hit whenever extraction finds values (shipped run proves it).
  - :1357-1438 `run_ablation` — hit via `--ablation` (I ran it today).
  - :1442-1466 argparse/main — hit when run as a script (I ran it).
  - :445, 525, 543-544, 776, 917, 1021, 1033-1055 — branch arms needing specific inputs (toc pages, keywordless params, disagree verdicts).
- **Semi-dead worth a decision:** `PageTags.summary`/`topics` (:125-127) are *requested from the LLM on all 81 calls* (≈17% of tag output tokens) and consumed by **nothing** — no routing, no diagnostics. They satisfy the spec's optional "extra tag" bullet, but README's claim that the open-ended layer lets "a *new* parameter be routed without re-tagging" is implemented nowhere. Either wire them into diagnostics (cheap) or stop claiming the routing use.
- Also: `UNVERIFIED_SCORE` (:63) is used both as "call failed" score and as the judge's neutral placeholder — misleading name; commented-out code blocks: none found; leftover debug prints: none (all prints are deliberate progress/KPI output); abandoned-approach remnants: none besides `main.py`.
- **LOC before → after proposed deletions:** 1637 (solution 1470 + app 151 + main 16) → **≈1600** (delete main.py 16, dead branch 7, header_lines 2, dict-branch 2, unquote annotation, drop unused noqa). This codebase is not fat; the cruft is small and specific.

## 11. Architecture correctness — mostly VERIFIED
- **Import graph (actual):** `app.py → solution.py`; `solution.py → stdlib + pydantic (+ lazy: google.genai at :182, pymupdf at :379, dotenv at :1456)`. No cycles. Core routing (`match_parameters_to_pages`, `final_score`, `check_grounded`, `format_source_hebrew`) is pure and unit-testable with no network/PDF — VERIFIED (my boundary trace drove them with in-memory objects).
- **Typed boundaries:** `Page`, `DocumentMeta`, `ParameterSpec`, `PageTags`, `Extraction`, `Usage` — all typed. Bare-dict exceptions: `process_document`'s return (consumed by `app.py:46-52` with `.get(...) or []` — the exact cope the brief flags) and `page_map` as `dict[str, list[int]]` (fine). MINOR.
- **Pure vs effectful:** functions that both compute and do I/O: `tag_pages` (LLM+cache+print), `extract_all`, `verify_and_score` (LLM+print), `load_document` (disk), `run_ablation` (LLM-tokenizer+print), `save_and_print`. The computation inside each is delegated to pure helpers (`build_*`, `match_*`, `final_score`) — the effectful shells are thin. Acceptable; listed for completeness.
- **Client construction:** exactly one site — `GeminiProvider.__init__` via `get_provider` (:236-242), called once per `process_document`/`run_ablation`. Never in a loop. ✅ VERIFIED by grep.
- **Config injection:** functions take config as *default arguments bound to module constants* (`model: str = MODELS["tagger"]` etc.). Injectable in tests, but defaults are baked at def time — notably `load_parameters(params_path=PARAMS_JSON)` means monkeypatching `solution.PARAMS_JSON` later does nothing (bit me in testing). MINOR.
- **Error taxonomy:** failures are *not* typed — everything collapses to `(result, usage, ok: bool)` from `call_with_retry` (:689 broad `except Exception`, logged) plus `Extraction.error`. Distinguishable states exist (error ≠ not_found ≠ unverified) but as strings/bools, not exceptions. Silently swallowed: `count_tokens` (:221-222, silent `len//3` fallback — the ablation would quietly report estimates as if measured) and `find_tables` (:388). No literal bare `except:` anywhere. MINOR each.
- **Seams for offline testing (exactly three):** (1) `get_provider`/the `Provider` protocol, (2) `init_storage`'s `db_path`, (3) the `pdf_path`/`params_path` arguments. That's the right number; VERIFIED — my whole stress harness used only these.

## 12. LLM integration — VERIFIED where possible (quota limited the rest)
- **Page-index integrity:** the highest-risk failure is **designed out** — tagging is one page per call (`tag_pages` :611), and pages are keyed by `page.number` in the results dict, never zipped positionally. Relevance items are matched by **parameter name** (`rel.parameter != spec.name` :737), not position, so fewer/more/reordered items degrade to "no score" rather than misattribution — READ, and structurally confirmed in the trace. Residual risk: a model that returns a *misspelled* parameter name is silently dropped (recall loss, no warning). MINOR: log unknown names.
- **Response parsing:** native schema-constrained output (`response_mime_type="application/json"` + `response_schema` :193-195), `response.parsed` with `model_validate_json` fallback :203-204. Fenced/preamble/single-quote/truncated cases → pydantic validation error → retry → typed failure, never a regex. This is the provider's structured-output mode; ✅ the audit's parser-abuse suite is moot by construction.
- **max_tokens / longest response:** not set (F-09). Longest observed: 471 output tokens (tag cache, 364 entries), extraction answers ≈500 tokens vs multi-thousand default cap — no truncation observed.
- **Context budget:** no explicit guard, but prompts are structurally bounded: measured largest extraction prompt today = **13,247 chars ≈ 4.4k tokens** (threshold_conditions, 6 pages); largest tag prompt 3,393 tokens. The unguarded case is a single pathological page (a huge table page in a real tender). Not a BLOCKER at these budgets; add a per-page char clamp in `render_pages_for_prompt` if you want the 800-page claim airtight. MINOR.
- **Confidence pass independence: FAILS — F-02** (payload pasted above). Same pages both runs ✅; different model (cross-size, same vendor) ⚠️ — spec says "עדיף מאוד מודל אחר", satisfied literally, and README states the weakness honestly.
- **Determinism/cost:** temperature unset (F-09). Cache: tags only, content-hash keyed (F-07); extraction/judge **uncached** — rerun burns ~24k tokens and can change answers. Cost, measured: cold sample run ≈ 214,676 tokens end-to-end → **$0.00 on free tier; ≈ $0.06 at paid flash pricing**; per-stage split: tagging 190,328 / extraction 24,348 / judge ≈ small (shipped test-doc run: 131,282 in / 19,025 out for 67 calls total).
- **Document-sourced instruction risk:** pages are delimited (`[עמוד N]` blocks :862, header block labelled), and the system text says "ענה אך ורק על סמך הטקסט שניתן לך" — but **no line says page content is data, not instructions**. MINOR: add one sentence to `EXTRACTION_SHARED`/`TAGGING_SYSTEM`.

## 13. Universality — would it survive an unseen 200-page municipal tender?
**Lexicon audit (term by term, `param_config.json`):** every keyword is generic Israeli-tender vocabulary (תנאי סף, ערבות בנקאית, מפ"ל, אמות מידה, תקופת ההתקשרות, אופציה, רשות/עירייה/תאגיד…). **No** term from the sample answers (no פריוריטי, no SQL, no ניטור), no numbers, no issuer names — VERIFIED by grep. The prefilter is recall-only (pages without hits are still tagged, :530-546), so even a bad lexicon can't drop an answer, only waste tokens.
**Constants:** `RELEVANCE_THRESHOLD=2`, `MAX_PAGES_PER_PARAM=4`, per-param `max_pages`, `role_bonus`, boilerplate `threshold=0.5`, truncation `density=0.6` — all guesses-dressed-as-config, tuned (at most) on the dev tender. The holdout run transferring cleanly (shipped test results, reversed 60/40 weights found correctly) is the one piece of empirical support. Honest answer: defensible defaults, no sweep behind them.
**Hardcoded parameter references in code:** two, both in prompt literals — `TAGGING_SYSTEM` :569-571 names **שם המזמין ושם המכרז** in a special scoring rule, and `FAMILY_INSTRUCTIONS["list_or_table"]` :851 has a line specifically for **שיטת הערכה** ("ציין את המשקלות באחוזים"). Rename or drop those parameters and the prompts talk about ghosts. MINOR-to-MAJOR depending on how hard you sell "the code never assumes the set". Move both sentences into `param_config.json` descriptions.
**Structural assumptions found:** page 1 = cover role (:446) — soft (only a +0.2 routing bonus); **metadata family → first_page is a hard rule** (F-08) — breaks on a transmittal-letter-first tender; text layer assumed (no OCR — rasterized fixture VERIFIED silent all-not-found, no warning); Hebrew-only assumed in normalization/gazetteer (English annex pages get tagged but the prefilter can't see them — recall saved by the tag-everything rule); memory O(n) pages, no all-pairs structure (810 pages: 116MB RSS) ✅; params iterate whatever the JSON holds, non-ASCII keys fine, missing enrichment fine ✅ VERIFIED.

**Stress tests (all run today, stub extraction + real cached tags):**

| Fixture | Result |
|---|---|
| 810-page duplicate | **PASS** — 20.4s non-LLM wall, 116MB RSS, avg pages/param 2.86 (economy holds), routing capped correctly. Caveat: dedup made tagging free; 800 *distinct* pages = 800 tagger calls ≈ 190k×10 tokens and free-tier RPD becomes the binding constraint. |
| Shuffled page order | **PASS/DEGRADED-gracefully** — no crash, routing adapts (avg 3.14), no positional dependency surfaced. |
| Non-tender (moss field guide) | **PASS with caveat** — all seven `לא נמצא`; but metadata params still sent page 1 unconditionally (F-08); live-model behaviour on that page untestable today (quota). |
| Rasterized, no text layer | **DEGRADED-silent** — all `לא נמצא`, scores 3, **no warning** that zero text was extracted. `solution.py:372-406` has no empty-text guard. |
| 8th parameter, Hebrew-plausible, no enrichment | **PASS end-to-end / FAIL on warm cache** — flowed on `_default`, no code change, but stale cache starved it to 0 pages (F-07). |
| Remove a parameter | **PASS** — nothing references it. |
| Rename a parameter key | **PASS structurally** — flows on defaults; same stale-cache starvation as above. |
| Zero-page PDF | **FAIL** — `IndexError` at `solution.py:481` (F-10). |
| Corrupt PDF | **FAIL** — raw `pymupdf.FileDataError` traceback at `solution.py:381` (F-10). |

**Verdict for this section:** on an unseen 200-page municipal tender with a text layer and a conventional cover, this pipeline would very likely produce sensible output with no changes — routing, prefilter, and prompts carry no sample-specific content, and the holdout run supports it. What must change before I'd trust it unattended: F-08 (cover-page hard rule), the no-text-layer silent path, F-10 (crash on degenerate input), and F-07 if any warm cache exists. Scanned appendices and huge table pages remain out of scope and should be *stated* as such.

## 14. Honest weaknesses — the three interview attacks
1. **"Your confidence score is a self-licking ice-cream — the judge saw the answer."** (F-02.) Ready answer: the schema forces the judge to emit its own reading *before* the verdict (autoregressive commitment), groundedness is checked deterministically outside any model, and the README states the anchoring risk. Better answer: fix it — run the judge blind and compare; it's two small changes. If asked why same-vendor: free-tier constraint, `Provider` protocol makes Anthropic a config swap; quota independence between models is real (different per-model limits).
2. **"You told the model the trap parameter wouldn't be found."** (F-01.) There is no good answer to this one — it's the single explicit prohibition in the spec and it's in your prompt via config. Fix before submission; after the fix, the story is genuinely strong (identical code path, grep-clean, scores from agreement).
3. **"A rate limit rewrote your deliverable to garbage and exited 0 — what happens during the live demo?"** (F-04/F-05/F-11 — this literally happened during the audit.) Ready answer needs to be operational, not aspirational: pass `"unverified"` (one word), fail loudly on extraction errors, record the video on warm cache with fresh quota, keep a paid key in `.env` as fallback. Also be ready for: "your README table says score 5, your shipped results.json says 1 — which is it?"

Design decision I'd push back on: scoring an agreed absence **5** against the spec's example showing **0**. Your prose reading is defensible and documented, but PLAN.md itself decided the opposite and the example is the only concrete artifact the grader can diff against. Cheap insurance: `score: 0` for not-found (one constant), keep the agreement logic in diagnostics.

---

## Verdict

**FIX BLOCKERS FIRST.**

The architecture is genuinely good — real page economy (2.86/81 pages, −96% extraction tokens, measured), clean modularity (8th parameter flows with zero code changes), no leakage, no hardcoded trap handling in code, honest diagnostics. But F-01 violates the assignment's only explicit prohibition, F-02 undermines the metric the whole verification stage exists to produce, and F-04/F-05 mean the artifact currently on disk contradicts your README and would sink the demo. All four fixes are small (one JSON sentence, one restructured judge call, one word, five lines). Fix those, re-run both tenders on fresh quota, re-check the README table against the actual files, then submit.
