"""
Tender parameter extraction — page-level structured routing.

Instead of sending an entire tender to an LLM for every parameter, the document is cut into
pages, each page is tagged once, and each parameter is routed to the few pages that can answer
it. Only those pages are sent for extraction.

    PDF -> pages -> prefilter -> tag -> match -> extract -> verify -> output

Run:  python solution.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, TypeVar

from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────────────────
# Configuration.  Everything document-specific lives here or in param_config.json,
# never inside a function — that is what keeps the pipeline from overfitting to
# the one tender it was developed against.
# ─────────────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"

PDF_PATH = DATA_DIR / "tender_sample.pdf"
PARAMS_JSON = DATA_DIR / "parameters.json"
PARAM_CONFIG_PATH = ROOT / "param_config.json"
CACHE_DB = OUTPUT_DIR / "cache.sqlite"

# Models are addressed by ROLE, per provider. Swapping provider or tier is a change here only.
PROVIDER_MODELS = {
    "gemini": {
        "tagger": "gemini-3.5-flash-lite",   # 81 calls/doc — cheapest tier that reads Hebrew well
        "extractor": "gemini-2.5-flash",      # few calls, Hebrew legal nuance
                                             # (3.7-flash has no free-tier quota: instant 429)
        "judge": "gemini-3.5-flash-lite",    # different family from the extractor
    },
    "anthropic": {
        "tagger": "claude-haiku-4-5",        # bulk per-page classification, short structured output
        "extractor": "claude-opus-5",        # the quality-critical role
        "judge": "claude-sonnet-5",          # blind second run — a different model from the extractor
    },
    "local": {                               # LM Studio, OpenAI-compatible, fully offline
        "tagger": "mistralai/ministral-3-3b",   # smallest — 81 calls
        "extractor": "google/gemma-4-12b-qat",  # largest local model for the quality role
        "judge": "mistralai/ministral-3-3b",    # different family from the extractor: the blind
                                             # cross-check stays cross-family, which is the point
                                             # of the role. qwen3.5-9b was the first choice and was
                                             # dropped — see LMStudioProvider on reasoning_content.
    },
}

# Per-provider extractor fallbacks: quotas/refusals are per-model, so a second
# model is a genuine escape hatch (this is the pipeline's own fallback chain).
PROVIDER_FALLBACKS = {
    "gemini": ["gemini-3.5-flash-lite"],
    "anthropic": ["claude-sonnet-5"],
    "local": ["mistralai/ministral-3-3b"],   # the only other local model with working JSON grammar
}

MODELS = PROVIDER_MODELS["gemini"]           # default provider's roles
EXTRACTOR_FALLBACKS = PROVIDER_FALLBACKS["gemini"]

PROMPT_VERSION = "v3"   # v3: idea_author's "not expected to appear" hint removed from prompts
TAGGER_CONCURRENCY = 5          # free-tier ceiling; 10 triggers sustained 429s

# Output budget for local models. Deliberately far below the cloud providers':
# a 12B model on a laptop runs at single-digit tokens/sec, so a 4000-token budget
# can take longer than the HTTP read timeout. When that timeout fires, LM Studio is
# left with an abandoned in-flight generation and rejects every following request
# with a 400 — including requests for a *different* model, which takes the fallback
# chain down with it. Capping generation is what keeps one slow call from wedging
# the whole run. 1500 still clears the longest real answer (threshold_conditions
# lands near 400 tokens) with headroom for models that emit reasoning tokens.
LOCAL_MAX_TOKENS = 1500
MAX_PAGES_PER_PARAM = 4         # sending few pages is an explicit goal of the task
RELEVANCE_THRESHOLD = 2         # keep pages scoring >= this (scale is 0..3)

# A running header is a formatting habit, not a property of tenders. Of the two
# documents shipped with this assignment one stamps all 81 pages and the other stamps
# none, so every constant below describes an OPTIONAL fast path: when a document has
# no stamp the pipeline must route metadata parameters like any other parameter, not
# read an empty header. See DocumentMeta.has_running_header.
HEADER_MIN_COVERAGE = 0.4       # a stamp on fewer pages than this is not a running header
EDGE_LINE_MAX_CHARS = 120       # header/footer lines are short; a repeated clause is not
COVER_MAX_CHARS = 600           # a title page announces the tender and little else
COVER_WINDOW = 4                # how deep to look for it: bundles open with front matter
LETTER_SPACING_MIN_RUN = 5      # letters in a row before "א ב ג ד ה" counts as tracked-out text
LETTER_SPACING_PAGE_SHARE = .35  # single-letter tokens above which a whole page is tracked-out

# The required output contract uses these exact literals.
NOT_FOUND_SOURCE = "לא נמצא"
# A confident "not found" is a correct answer and scores high, per the spec:
# "Score - based on the certainty level of the model that it is sure it did not find it."
# Only an UNVERIFIED absence — a failed call, or incomplete page coverage — scores low.
UNVERIFIED_SCORE = 1

HEBREW_LETTERS = r"א-ת"


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Page:
    """One physical page of the tender. 1-indexed, as humans cite them."""
    number: int
    raw_text: str
    norm_text: str = ""
    char_count: int = 0
    has_table: bool = False
    is_toc: bool = False
    page_role: str = "body"          # cover | toc | body | appendix | form
    section_title: str | None = None
    rtl_repaired: bool = False
    despaced: bool = False           # letter-spacing was collapsed on this page
    body_text: str = ""   # raw_text minus repeated header/footer


@dataclass
class DocumentMeta:
    """The running header — WHEN the document has one. Many tenders do not.

    Where a stamp does repeat it carries the publisher and the tender's full name, so
    the metadata parameters can be answered with ZERO body pages sent to the model,
    which serves the "minimum pages per parameter" goal directly. That is a fast path,
    not the design: `has_running_header` gates it, and every caller has a route for
    False. An unstamped document is a normal document, not a degraded one.
    """
    header_text: str = ""
    header_lines: set[str] = field(default_factory=set)
    first_page: int = 1
    pages_covered: int = 0            # pages carrying at least one header line
    page_count: int = 0               # pages in the document, for the ratio

    @property
    def coverage(self) -> float:
        return self.pages_covered / self.page_count if self.page_count else 0.0

    @property
    def has_running_header(self) -> bool:
        """A stamp worth answering from: real text, repeating across the document.

        Both halves are load-bearing. Identity alone would accept a phrase appearing
        on three pages of eighty; coverage alone would accept the signature footer, or
        any body line that happens to close every page — text that repeats perfectly
        and names nobody. Only a stamp that both recurs AND identifies the tender can
        stand in for the pages that would otherwise have to be read.
        """
        return carries_identity(self.header_text) and self.coverage >= HEADER_MIN_COVERAGE


@dataclass
class ParameterSpec:
    """A parameter to find, merged from their parameters.json and our side-car config."""
    name: str                         # english key, e.g. "bid_guarantee"
    hebrew_name: str = ""
    description: str = ""
    family: str = "atomic"            # metadata | atomic | list_or_table
    keywords: list[str] = field(default_factory=list)
    max_pages: int = MAX_PAGES_PER_PARAM


class ParameterRelevance(BaseModel):
    """How relevant one page is to one parameter. Constrained LLM output."""
    parameter: str
    score: int = Field(ge=0, le=3, description="0 none, 1 mention, 2 related, 3 contains the answer")
    evidence: str = Field(default="", description="short verbatim Hebrew snippet")


class PageTags(BaseModel):
    """What ONE tagging call returns for ONE page.

    Deliberately excludes page_role and section_title: those are derived
    deterministically in enrich_pages_structurally(), for free and verifiably.
    """
    summary: str = Field(default="", description="משפט אחד בעברית: על מה העמוד הזה")
    topics: list[str] = Field(default_factory=list, description="2-4 נושאים קצרים בעברית")
    relevance: list[ParameterRelevance] = Field(default_factory=list)


@dataclass
class Extraction:
    """Internal, richer than the required output; rendered down in build_output()."""
    parameter: str
    status: str = "not_found"         # found | not_found
    answer: str = ""
    details: str = ""
    pages: list[int] = field(default_factory=list)
    locator: str = ""
    quote: str = ""
    grounded: bool = False
    agreement: str = "unknown"        # agree | partial | disagree | unknown
    score: int = UNVERIFIED_SCORE
    error: bool = False               # the call failed — NOT the same as "not found"
    max_relevance: int = 0            # best tagger score for this parameter, any page
    coverage_complete: bool = True    # every page tagged successfully
    judge_answer: str = ""            # the second model's independent reading
    judge_reason: str = ""


@dataclass
class Usage:
    """Provider-neutral accounting, so the KPI layer never knows who served the call."""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    latency_ms: float = 0.0
    from_cache: bool = False


T = TypeVar("T", bound=BaseModel)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Provider — a narrow seam so the free tier can be swapped for a paid one later
# ─────────────────────────────────────────────────────────────────────────────

class Provider(Protocol):
    """Anything that can turn a prompt into a validated Pydantic object."""

    async def complete(self, prompt: str, schema: type[T], model: str) -> tuple[T, Usage]:
        ...

    def count_tokens(self, text: str, model: str) -> int:
        ...


class GeminiProvider:
    """Google AI Studio free tier. Uses native schema-constrained output — no JSON parsing by hand."""

    def __init__(self, api_key: str) -> None:
        from google import genai  # imported lazily so the script runs without the dependency

        self._client = genai.Client(api_key=api_key)

    async def complete(self, prompt: str, schema: type[T], model: str) -> tuple[T, Usage]:
        from google.genai import types

        started = time.perf_counter()
        response = await self._client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                # We never use function calling; leaving AFC on emits a warning per call.
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            ),
        )
        latency_ms = (time.perf_counter() - started) * 1000

        parsed = response.parsed
        if not isinstance(parsed, schema):        # older SDKs return text only
            parsed = schema.model_validate_json(response.text)

        meta = response.usage_metadata
        usage = Usage(
            model=model,
            input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
            output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
            cached_tokens=getattr(meta, "cached_content_token_count", 0) or 0,
            latency_ms=latency_ms,
        )
        return parsed, usage


    def count_tokens(self, text: str, model: str) -> int:
        """Exact counts from the provider — Hebrew tokenises poorly under char/4 rules."""
        try:
            return self._client.models.count_tokens(model=model, contents=text).total_tokens or 0
        except Exception:
            return len(text) // 3


class AnthropicProvider:
    """Anthropic Claude. Uses messages.parse — native schema-constrained output, no JSON parsing by hand."""

    def __init__(self, api_key: str | None = None) -> None:
        import anthropic  # imported lazily so the script runs without the dependency

        # No explicit key lets the SDK resolve env / `ant auth login` credentials.
        self._client = anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()
        self._sync = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    async def complete(self, prompt: str, schema: type[T], model: str) -> tuple[T, Usage]:
        started = time.perf_counter()
        response = await self._client.messages.parse(
            model=model,
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        )
        latency_ms = (time.perf_counter() - started) * 1000

        parsed = response.parsed_output
        if not isinstance(parsed, schema):
            raise ValueError(f"{model}: no parsed output (stop_reason={response.stop_reason})")

        usage = Usage(
            model=model,
            input_tokens=response.usage.input_tokens or 0,
            output_tokens=response.usage.output_tokens or 0,
            cached_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            latency_ms=latency_ms,
        )
        return parsed, usage

    def count_tokens(self, text: str, model: str) -> int:
        try:
            counted = self._sync.messages.count_tokens(
                model=model, messages=[{"role": "user", "content": text}]
            )
            return counted.input_tokens or 0
        except Exception:
            return len(text) // 3


class LMStudioProvider:
    """Local models served by LM Studio's OpenAI-compatible API. No key, no cloud, no cost.

    Structured output rides the same guarantee as the cloud providers: the JSON schema
    is enforced server-side (llama.cpp grammar), and the reply is validated by the
    same Pydantic model — never parsed from free text."""

    def __init__(self, base_url: str | None = None) -> None:
        import httpx  # imported lazily, like the other providers' SDKs

        self._base = (base_url or os.environ.get("LMSTUDIO_BASE_URL")
                      or "http://localhost:1234/v1").rstrip("/")
        # A 12B model on a laptop can take minutes on a long prompt — be patient.
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0))

    async def complete(self, prompt: str, schema: type[T], model: str) -> tuple[T, Usage]:
        started = time.perf_counter()
        response = await self._client.post(f"{self._base}/chat/completions", json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": LOCAL_MAX_TOKENS,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "strict": True,
                "schema": schema.model_json_schema(),
            }},
        })
        if response.status_code >= 400:
            # LM Studio puts the actual cause (context overflow, model not loaded,
            # bad grammar) in the body. raise_for_status() throws it away and leaves
            # you with a bare "400 Bad Request", which is unactionable.
            raise RuntimeError(
                f"LM Studio {response.status_code} for {model}: {response.text[:400]}")
        data = response.json()
        latency_ms = (time.perf_counter() - started) * 1000

        message = data["choices"][0]["message"]
        text = message.get("content") or ""
        # Some local models still fence or think out loud despite the grammar.
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())

        if not text:
            # Reasoning models split their output into a separate `reasoning_content`
            # channel. Some of them (qwen3.5-9b, in this LM Studio build) emit the
            # schema-constrained JSON *into that channel* and leave `content` empty —
            # and the JSON there comes back with corrupted keys ("/status"), so it is
            # not salvageable. Fail loudly with the cause rather than on "Invalid JSON",
            # which sends you looking for a truncated response that never happened.
            reasoning = (message.get("reasoning_content") or "").strip()
            raise RuntimeError(
                f"{model} returned an empty content field"
                + (f"; its output went to reasoning_content instead: {reasoning[:200]!r}. "
                   "This model cannot be used for structured output here — pick another "
                   "in PROVIDER_MODELS['local']." if reasoning else ".")
            )

        parsed = schema.model_validate_json(text)

        spent = data.get("usage") or {}
        usage = Usage(
            model=model,
            input_tokens=spent.get("prompt_tokens", 0) or 0,
            output_tokens=spent.get("completion_tokens", 0) or 0,
            latency_ms=latency_ms,
        )
        return parsed, usage

    def count_tokens(self, text: str, model: str) -> int:
        return len(text) // 3     # LM Studio exposes no counting endpoint — heuristic


class StubProvider:
    """Returns schema-valid empty objects so the whole pipeline runs with no API key."""

    async def complete(self, prompt: str, schema: type[T], model: str) -> tuple[T, Usage]:
        await asyncio.sleep(0)
        return schema(), Usage(model=f"stub:{model}")

    def count_tokens(self, text: str, model: str) -> int:
        return len(text) // 3


@dataclass
class LLMSetup:
    """One resolved choice of provider + the models each pipeline role should use."""
    name: str                                  # "gemini" | "anthropic" | "stub"
    provider: Provider
    models: dict[str, str]
    fallbacks: list[str] = field(default_factory=list)


def setup_llm(provider_name: str | None = None, api_key: str | None = None) -> LLMSetup:
    """Resolve provider + models. Explicit arguments (e.g. from the UI) win over the
    environment. Absence of a key is not an error — it degrades to the stub."""
    gemini_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    if provider_name is None:                  # infer from whichever key exists
        provider_name = "gemini" if gemini_key else ("anthropic" if anthropic_key else None)

    if provider_name == "local":
        # No key required — api_key doubles as an optional base-URL override from the UI.
        return LLMSetup("local", LMStudioProvider(base_url=api_key or None),
                        PROVIDER_MODELS["local"], PROVIDER_FALLBACKS["local"])
    if provider_name == "anthropic":
        key = api_key or anthropic_key
        if key:
            return LLMSetup("anthropic", AnthropicProvider(key),
                            PROVIDER_MODELS["anthropic"], PROVIDER_FALLBACKS["anthropic"])
        print("  ! no Anthropic API key — running with StubProvider (no LLM calls)")
    elif provider_name == "gemini":
        key = api_key or gemini_key
        if key:
            return LLMSetup("gemini", GeminiProvider(key),
                            PROVIDER_MODELS["gemini"], PROVIDER_FALLBACKS["gemini"])
        print("  ! no Gemini API key — running with StubProvider (no LLM calls)")
    else:
        print("  ! no API key set — running with StubProvider (no LLM calls)")
    return LLMSetup("stub", StubProvider(), MODELS, [])


def get_provider() -> Provider:
    """Kept for convenience: environment-resolved provider only."""
    return setup_llm().provider


# ─────────────────────────────────────────────────────────────────────────────
# 2. Cache + telemetry.  Page tags are keyed by content hash, so re-runs are free —
#    which is what makes a live demo of an 800-page document feasible.
# ─────────────────────────────────────────────────────────────────────────────

CALLS: list[Usage] = []


def init_storage(db_path: Path = CACHE_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS tag_cache (key TEXT PRIMARY KEY, payload TEXT)")
    conn.commit()
    return conn


def cache_key(page: Page, prompt_version: str, model: str) -> str:
    import hashlib

    material = f"{page.raw_text}|{prompt_version}|{model}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def cache_get(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT payload FROM tag_cache WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def cache_put(conn: sqlite3.Connection, key: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tag_cache (key, payload) VALUES (?, ?)",
        (key, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()


def record(usage: Usage) -> None:
    CALLS.append(usage)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Ingest
# ─────────────────────────────────────────────────────────────────────────────

def load_parameters(
    params_path: Path = PARAMS_JSON,
    config_path: Path = PARAM_CONFIG_PATH,
) -> list[ParameterSpec]:
    """Read THEIR parameter list, enrich it from our side-car.

    The list is never hardcoded — an unknown parameter falls back to _default and still runs.
    """
    raw = json.loads(params_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    default = config.get("_default", {})

    entries = raw.items() if isinstance(raw, dict) else ((p, {}) for p in raw)

    specs: list[ParameterSpec] = []
    for name, meta in entries:
        if not isinstance(meta, dict):
            meta = {"hebrew_name": str(meta)}
        cfg = config.get(name, default)
        specs.append(
            ParameterSpec(
                name=name,
                # Their parameters.json is a bare list of English keys, so Hebrew
                # names and descriptions can only come from the side-car.
                hebrew_name=meta.get("hebrew_name") or cfg.get("hebrew_name", ""),
                description=meta.get("description") or cfg.get("description", ""),
                family=cfg.get("family", default.get("family", "atomic")),
                keywords=cfg.get("keywords", []),
                max_pages=cfg.get("max_pages", MAX_PAGES_PER_PARAM),
            )
        )
    return specs


HEBREW_FINALS = "ךםןףץ"
_HEBREW_WORD = re.compile(rf"[{HEBREW_LETTERS}]+")
# A number group like 28/2024 or 31.12.2024 must flip back as a whole, not per-segment.
_LATIN_DIGIT_RUN = re.compile(r"[0-9A-Za-z]+(?:[./,:\-][0-9A-Za-z]+)*")


def detect_rtl_reversal(text: str) -> bool:
    """Is this extracted Hebrew stored back-to-front?

    Some PDF producers emit Hebrew in visual rather than logical order, and PyMuPDF
    faithfully returns what is stored — so "כללי" comes back as "יללכ". Rather than
    flip everything blindly (which would corrupt a correctly-encoded document), use an
    orthographic invariant: the five final-form letters ךםןףץ may ONLY end a word.
    If they are turning up at the START of words, the text is reversed.
    """
    starting = ending = 0
    for word in _HEBREW_WORD.findall(text):
        if len(word) < 2:
            continue
        starting += word[0] in HEBREW_FINALS
        ending += word[-1] in HEBREW_FINALS
    return starting > ending


def repair_rtl(text: str) -> str:
    """Flip each line back to logical order, keeping numbers and Latin runs readable.

    A naive line[::-1] would also reverse "28/2024" into "4202/82", so any run of
    digits or Latin characters is flipped back after the line is reversed.
    """
    lines = []
    for line in text.split("\n"):
        flipped = line[::-1]
        lines.append(_LATIN_DIGIT_RUN.sub(lambda m: m.group(0)[::-1], flipped))
    return "\n".join(lines)


# Tenders set title pages in tracked-out type — "ה ח ב ר ה  ה ל א ו מ י ת" — and the
# extractor returns exactly that. Below LETTER_SPACING_MIN_RUN the pattern also matches
# ordinary prose, because ו/ה/ב/ל/כ/מ/ש are real one-letter words that can fall next to
# each other; at 5 every match in the four trusted documents is genuinely spaced text.
_LETTER_SPACED = re.compile(
    rf"(?:[{HEBREW_LETTERS}][ \t]){{{LETTER_SPACING_MIN_RUN - 1},}}[{HEBREW_LETTERS}]"
)
# Used only once a page has already proved itself tracked-out; matches runs of two, and
# includes digits, which the global pass must never touch — a tracked-out cover spaces
# its tender number too ("מ ס ' 5 4 / 1 2"), and that number is half of tender_name.
_LETTER_SPACED_ANY = re.compile(rf"(?:[{HEBREW_LETTERS}0-9][ \t])+[{HEBREW_LETTERS}0-9]")


def single_letter_share(text: str) -> float:
    """Share of Hebrew tokens that are a lone letter — how tracked-out this text is."""
    tokens = re.findall(rf"[{HEBREW_LETTERS}]+", text)
    if len(tokens) < 10:                 # too little Hebrew to judge
        return 0.0
    return sum(1 for t in tokens if len(t) == 1) / len(tokens)


def _close_run(match: re.Match[str]) -> str:
    return match.group().replace(" ", "").replace("\t", "")


def despace_hebrew(text: str) -> str:
    """Close up letter-spaced Hebrew: "ה ז מ נ ה" -> "הזמנה".

    Same class of defect as visual-order RTL, and repaired in the same place and the
    same way — at load, on raw_text, so every later stage sees one clean string. Left
    alone it is not a cosmetic problem: a spaced page is invisible to the lexical
    prefilter, to cover and header detection, and to the grounding check, whose word
    overlap discards one-character tokens and so sees a spaced quote as no words at all.

    Two passes, because one threshold cannot serve both jobs:

    * A run of LETTER_SPACING_MIN_RUN letters is safe to close anywhere, since no
      ordinary Hebrew puts that many one-letter words in a row.
    * That leaves the short fragments — "מ ס", "ש ל", "ה ק מ ה" — which are exactly
      what a genuinely tracked-out title page is full of, and which cannot be closed
      globally because ו/ה/ב/ל/כ/מ/ש are real one-letter words. So the second pass is
      earned per page: once a page is still LETTER_SPACING_PAGE_SHARE single letters
      after the first pass, it has proved what it is, and runs of two can be closed on
      it. The margin is wide — normal pages across the five tenders on hand peak at
      21%, the one tracked-out page sits at 61%.

    Only spaces WITHIN a line are closed. Never the newlines between words: on a fully
    spaced page it is the newline that separates one word from the next, so crossing it
    would run "ה ז מ נ ה\nל ה צ י ע" together into a single token.
    """
    closed = _LETTER_SPACED.sub(_close_run, text)
    if single_letter_share(closed) > LETTER_SPACING_PAGE_SHARE:
        closed = _LETTER_SPACED_ANY.sub(_close_run, closed)
    return closed


def normalize_hebrew(text: str) -> str:
    """Normalize for MATCHING only. Citations always quote raw_text."""
    text = re.sub(r"[֑-ׇ]", "", text)              # niqqud and cantillation
    text = text.replace("׳", "'").replace("״", '"')  # geresh / gershayim
    text = re.sub(r"[‎‏‪-‮]", "", text)   # bidi control marks
    # Extraction drops the space where Hebrew meets Latin/digits: "תמיכהDBA".
    text = re.sub(rf"([{HEBREW_LETTERS}])([A-Za-z0-9])", r"\1 \2", text)
    text = re.sub(rf"([A-Za-z0-9])([{HEBREW_LETTERS}])", r"\1 \2", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def load_document(file_name: Path = PDF_PATH) -> list[Page]:
    """Cut the PDF into pages. Each page is a separate unit of analysis.

    RTL orientation is decided ONCE for the whole document by majority vote across
    pages — deciding per page risks flip-flopping on sparse pages that carry too
    little Hebrew to judge.
    """
    import pymupdf

    doc = pymupdf.open(file_name)
    extracted = []
    for number, fitz_page in enumerate(doc, start=1):
        # PyMuPDF's own table finder — a real tender table extracts as positioned
        # text with no tabs or pipes, so a character heuristic never sees it.
        try:
            table_count = len(fitz_page.find_tables().tables)
        except Exception:
            table_count = 0
        extracted.append((number, fitz_page.get_text("text"), table_count))
    doc.close()

    reversed_votes = sum(detect_rtl_reversal(text) for _, text, _ in extracted)
    needs_repair = reversed_votes > len(extracted) / 2
    if needs_repair:
        print(f"  ! RTL: text stored in visual order on {reversed_votes}/{len(extracted)} "
              f"pages — repairing to logical order")

    pages = []
    for number, text, table_count in extracted:
        raw = repair_rtl(text) if needs_repair else text
        # Per page, unlike the RTL vote: tracked-out type is a property of the title
        # pages and section dividers that use it, not of the document. Doc 2 of the
        # sample set spaces 12 of its 49 pages and leaves the rest alone.
        despaced = despace_hebrew(raw)
        pages.append(
            Page(number=number, raw_text=despaced, norm_text=normalize_hebrew(despaced),
                 rtl_repaired=needs_repair, despaced=despaced != raw,
                 has_table=table_count > 0)
        )
    spaced_pages = sum(p.despaced for p in pages)
    if spaced_pages:
        print(f"  ! letter-spacing: closed up tracked-out Hebrew on {spaced_pages}/{len(pages)} pages")
    return pages


# A section number carries a dot ("2.", "11.4.4"); a bare "4" is the page number.
_SECTION_NUMBER = re.compile(r"^\s*(\d+\.\d+(?:\.\d+)*|\d+\.)\s*(.*)$")


def find_section_title(body: str) -> str | None:
    """First numbered heading on the page, e.g. "11.4.4 הציון המשוקלל".

    Headings in this tender are split across lines — the number on one line, the
    title on the next — so a single-line regex finds only page numbers.
    """
    lines = [ln.strip() for ln in body.split("\n")]
    for index, line in enumerate(lines):
        match = _SECTION_NUMBER.match(line)
        if not match:
            continue
        number, tail = match.group(1), match.group(2).strip()
        if not tail:  # title is on a following line
            tail = next((nxt for nxt in lines[index + 1: index + 4] if nxt), "")
        if re.match(rf"^[{HEBREW_LETTERS}]", tail):
            return f"{number} {tail}"[:80]
    return None


# What a title page says about itself. A cover DESIGNATES the tender ("מכרז פומבי",
# "הזמנה להציע הצעות"); a body page that merely mentions מכרז in passing does not.
_TENDER_DESIGNATION = re.compile(
    r"מכרז\s+(?:פומבי|סגור|פנימי|מסגרת|זוטא|משותף|דו[\s-]*שלבי)"
    r"|הזמנה\s+להציע\s+הצעות"
    r"|בקשה\s+לקבלת\s+הצעות"
)


def detect_cover_pages(pages: list[Page], window: int = COVER_WINDOW) -> set[int]:
    """Which opening page is the title page — decided by content, not by position.

    "Page 1 is the cover" holds for a standalone tender and breaks for everything
    else: a bundle opens with a transmittal sheet, a scan opens with a blank page, a
    compiled PDF opens with its table of contents. A cover is recognisable by what it
    does instead — it designates the tender and then stops: a designation, no numbered
    clause running down the page, and little text.

    All three conditions are needed. On the sample tender, page 3 is a four-line
    schedule table that is short and unnumbered too; what excludes it is that it says
    "פרסום המכרז" rather than designating one. Page 2 of the holdout designates the
    tender but runs 1,700 characters, which is a section opener, not a cover.

    Falls back to the first page when nothing announces itself, so a document that
    simply begins with its cover is unaffected by any of this.
    """
    cover = {
        page.number
        for page in pages[:window]
        if not page.is_toc
        and len(page.body_text or page.raw_text) < COVER_MAX_CHARS
        and _TENDER_DESIGNATION.search(page.norm_text)
        and not find_section_title(page.body_text or page.raw_text)
    }
    return cover or ({pages[0].number} if pages else set())


def enrich_pages_structurally(pages: list[Page]) -> list[Page]:
    """Deterministic features — no LLM, no cost. Used for routing tie-breaks.

    MUST run after strip_boilerplate(): the repeated "חתימה וחותמת" footer would
    otherwise mark almost every page as a signature page.

    Two passes, because the cover test needs is_toc for every page in the window
    before it can rule any page out.
    """
    for page in pages:
        body = page.body_text or page.raw_text
        page.char_count = len(body)
        page.is_toc = bool(re.search(r"תוכן\s*עניינים", page.norm_text)) or body.count(".....") > 2
        page.section_title = find_section_title(body)

    covers = detect_cover_pages(pages)
    for page in pages:
        norm = page.norm_text
        if page.is_toc:
            page.page_role = "toc"
        elif page.number in covers:
            page.page_role = "cover"
        elif re.search(rf"נספח\s+[{HEBREW_LETTERS}]['\u05f3]?(?:\s|$)", norm):
            page.page_role = "appendix"
        elif re.search(r"חתימת\s+המציע|הצהרת\s+המציע", norm):
            page.page_role = "form"
        else:
            page.page_role = "body"
    return pages


def detect_boilerplate(pages: list[Page], threshold: float = 0.5,
                       edge_threshold: float = 0.35, edge_lines: int = 3) -> set[str]:
    """Find the header/footer lines that repeat across the document.

    Where a tender stamps every page with the publisher, the tender number and a
    signature block, leaving that in place is not merely wasted tokens — the stamp
    *contains the answers* to client_name and tender_name, so those two parameters
    match on every page and routing collapses to "all 81 pages". Removing it is what
    makes the metadata fast path possible at all.

    Two tiers, because "repeats on most pages" is only one shape a stamp takes:

    * global — any line on more than `threshold` of pages. The classic stamp that
      runs the length of the document.
    * edge — a line among the first or last `edge_lines` of its page, on more than
      `edge_threshold` of pages. This catches a stamp that stops at an appendix
      boundary or changes partway through a bundle, which a single global cutoff
      misses entirely. Position is what lets the cutoff drop this low without eating
      body prose: a recurring clause is not pinned to the top of every page, and the
      length cap keeps a repeated paragraph out.

    A document with no stamp returns an empty set. That is a correct answer about the
    document, not a failure — callers must treat it as one.
    """
    from collections import Counter

    counts: Counter[str] = Counter()
    edge_counts: Counter[str] = Counter()
    for page in pages:
        lines = [ln.strip() for ln in page.raw_text.split("\n") if ln.strip()]
        for line in set(lines):
            counts[line] += 1
        for line in set(lines[:edge_lines] + lines[-edge_lines:]):
            edge_counts[line] += 1

    cutoff, edge_cutoff = len(pages) * threshold, len(pages) * edge_threshold
    stamp = {line for line, count in counts.items() if count > cutoff and len(line) > 3}
    return stamp | {line for line, count in edge_counts.items()
                    if count > edge_cutoff and 3 < len(line) <= EDGE_LINE_MAX_CHARS}


# Words that make up a signature block. A footer built only from these repeats as
# faithfully as a masthead but names no publisher and no tender, so it must never be
# what makes a document count as "headed" — see DocumentMeta.has_running_header.
HEADER_FURNITURE = {"חתימה", "וחתימה", "חותמת", "וחותמת", "חתימת", "המציע", "הספק",
                    "תאריך", "עמוד", "מתוך", "שם", "מס"}


def header_content(header_text: str) -> str:
    """The header minus its furniture — rules, page numbers, signature blanks."""
    kept = []
    for raw in header_text.split("\n"):
        line = raw.strip()
        # Split letters from digits so a page-counter that extracts glued together
        # ("מתוך44") is still recognised as the furniture it is.
        words = re.findall(rf"[{HEBREW_LETTERS}]+|[A-Za-z]+|\d+", line)
        if not words or all(w in HEADER_FURNITURE or w.isdigit() for w in words):
            continue
        kept.append(line)
    return "\n".join(kept)


# A stamp earns the metadata fast path only if it carries IDENTITY: the tender's
# designation, or the publisher's name. Repetition alone is not enough and the
# distinction is the whole gate — a running footer ("המשך בעמוד הבא"), a signature
# rule, or a clause that happens to close every page all repeat just as faithfully as
# a masthead while answering neither client_name nor tender_name. Strip those (they
# are still boilerplate, and removing them still saves tokens), but do not then try
# to read the tender's identity out of them.
_HEADER_IDENTITY = re.compile(
    _TENDER_DESIGNATION.pattern
    + r"|מכרז\s+מס|עיריי?ת|מועצה\s+(?:מקומית|אזורית|דתית)?|תאגיד|רשות|בע\"מ"
      r"|משרד\s+ה|חברה\s+ה|קיבוץ|מוא\"ז"
)


def carries_identity(header_text: str) -> bool:
    """Does this repeated stamp name the tender or its publisher?"""
    return bool(_HEADER_IDENTITY.search(normalize_hebrew(header_content(header_text))))


def build_document_meta(pages: list[Page], boilerplate: set[str]) -> DocumentMeta:
    """Reassemble the header in reading order, from the page that carries most of it.

    Also measures how much of the document the stamp actually covers, which is what
    lets has_running_header tell a masthead from a phrase that merely recurs.
    """
    if not pages:
        return DocumentMeta()
    best_page, best_hits = pages[0], -1
    for page in pages:
        hits = sum(ln.strip() in boilerplate for ln in page.raw_text.split("\n"))
        if hits > best_hits:
            best_page, best_hits = page, hits

    ordered, seen = [], set()          # de-duplicate, keeping first-seen order
    for line in (ln.strip() for ln in best_page.raw_text.split("\n")):
        if line in boilerplate and line not in seen:
            seen.add(line)
            ordered.append(line)
    stamped = [p.number for p in pages
               if any(ln.strip() in boilerplate for ln in p.raw_text.split("\n"))]
    return DocumentMeta(header_text="\n".join(ordered), header_lines=boilerplate,
                        first_page=stamped[0] if stamped else 1,
                        pages_covered=len(stamped), page_count=len(pages))


def strip_boilerplate(
    pages: list[Page],
    boilerplate: set[str],
) -> list[Page]:
    """Populate body_text. raw_text is left untouched so citations still verify.

    Safe to strip from every page, including the cover: the header is preserved
    whole in DocumentMeta, so nothing is lost — see build_document_meta().
    """
    for page in pages:
        kept = [
            ln for ln in page.raw_text.split("\n")
            if ln.strip() not in boilerplate
            and ln.strip() != str(page.number)      # the printed page number
        ]
        page.body_text = "\n".join(kept)
        page.norm_text = normalize_hebrew(page.body_text)
    return pages


# ─────────────────────────────────────────────────────────────────────────────
# 4. Lexical prefilter — the cheap tier of the cascade.
#    Tuned for RECALL, not precision: it may pass junk, it must never drop an answer.
# ─────────────────────────────────────────────────────────────────────────────

def build_gazetteer(spec: ParameterSpec) -> re.Pattern[str] | None:
    """Hebrew attaches ו/ה/ב/ל/כ/מ/ש as prefixes, so allow a short prefix before each keyword."""
    if not spec.keywords:
        return None
    alternatives = "|".join(rf"[{HEBREW_LETTERS}]{{0,3}}{re.escape(k)}" for k in spec.keywords)
    return re.compile(alternatives)


def lexical_prefilter(
    pages: list[Page],
    specs: list[ParameterSpec],
) -> dict[str, set[int]]:
    """Return, per parameter, the pages showing any lexical signal.

    Pages with no signal are still tagged (at reduced depth) — dropping them outright
    would cap recall permanently, which is the one unforgivable prefilter error.
    """
    hits: dict[str, set[int]] = {}
    for spec in specs:
        pattern = build_gazetteer(spec)
        if pattern is None:
            hits[spec.name] = {p.number for p in pages}
            continue
        hits[spec.name] = {p.number for p in pages if pattern.search(p.norm_text)}
    return hits


# ─────────────────────────────────────────────────────────────────────────────
# 5. Page tagging — one LLM call per page, the heart of the pipeline
# ─────────────────────────────────────────────────────────────────────────────

TAGGING_SYSTEM = """אתה מסייע לנתח מסמכי מכרז בעברית.
תקבל עמוד בודד ממכרז, ותסווג עבור כל פרמטר עד כמה העמוד רלוונטי לו.

סולם הרלוונטיות:
  3 = העמוד מכיל את התשובה עצמה לפרמטר
  2 = העמוד עוסק בנושא הפרמטר אך אינו נותן את הערך המלא
  1 = אזכור אגבי בלבד
  0 = לא רלוונטי

כללים חשובים:
- זהה גם ניסוחים עקיפים, מילים נרדפות, נטיות לשוניות והטיות
  (למשל: "ערבות" / "בטוחה" / "כתב ערבות" / "ערבות בנקאית אוטונומית";
   "תנאי סף" / "רשאים להשתתף" / "דרישות מוקדמות" / "כשירות").
- שים לב לתחיליות עבריות (ו/ה/ב/ל/כ/מ/ש) ולצורות רבים ונקבה.
- הסעיף המחייב בגוף המכרז מקבל 3, גם אם אותו ערך חוזר גם בנספח או בטופס.
  רק נוסח/תבנית ריקה למילוי (למשל "נוסח כתב ערבות" עם מקום ריק) מקבלת 2.
- עבור שם המזמין ושם המכרז: תן 3 רק כאשר העמוד מציג אותם באופן מגדיר
  (עמוד שער, כותרת, סעיף "המזמין הוא..."). אזכור שגרתי של "החברה" או של שם
  התאגיד בתוך טקסט אחר אינו 3 — תן 1.
- אל תנחש. אם הפרמטר אינו בעמוד, תן 0.
- ב-evidence העתק ציטוט קצר ומדויק מהעמוד (עד 15 מילים), מילה במילה. אל תמציא.
- החזר ערך לכל פרמטר ברשימה, גם אם הציון 0."""


def build_tagging_prompt(
    page: Page,
    specs: list[ParameterSpec],
    previous: Page | None,
    following: Page | None,
    context_chars: int = 200,
    brief: bool = False,
) -> str:
    """Assemble the per-page prompt.

    Neighbour context matters: a clause's heading often sits on the previous page, so a
    bare numbered list would otherwise be unclassifiable. It is supplied as CONTEXT ONLY
    — tagging must describe this page, or a heading would drag its neighbours' scores up.
    """
    catalogue = "\n".join(
        f"- {spec.name} ({spec.hebrew_name}): {spec.description}" for spec in specs
    )

    parts = [TAGGING_SYSTEM, "", "הפרמטרים לסיווג:", catalogue, ""]

    if not brief and previous is not None:
        parts += [f"[הקשר בלבד — סוף עמוד {previous.number}]",
                  previous.body_text.strip()[-context_chars:], ""]

    parts += [f"[עמוד {page.number} — סווג את זה בלבד]",
              page.body_text.strip() or "(עמוד ריק)", ""]

    if not brief and following is not None:
        parts += [f"[הקשר בלבד — תחילת עמוד {following.number}]",
                  following.body_text.strip()[:context_chars], ""]

    return "\n".join(parts)


async def tag_pages(
    pages: list[Page],
    specs: list[ParameterSpec],
    candidates: dict[str, set[int]],
    provider: Provider,
    conn: sqlite3.Connection,
    model: str = MODELS["tagger"],
    concurrency: int = TAGGER_CONCURRENCY,
) -> dict[int, PageTags]:
    """Tag every page exactly once, with bounded concurrency and a content-hash cache.

    One page per call rather than batching: batching's only real gain was amortising
    the prompt prefix, and it muddies per-page attribution — one page's content bleeds
    into another's tags.
    """
    by_number = {p.number: p for p in pages}
    with_signal = set().union(*candidates.values()) if candidates else set()
    semaphore = asyncio.Semaphore(concurrency)
    done = 0

    async def tag_one(page: Page) -> tuple[int, PageTags]:
        nonlocal done
        brief = page.number not in with_signal      # no lexical signal -> cheaper prompt
        prompt = build_tagging_prompt(
            page, specs, by_number.get(page.number - 1), by_number.get(page.number + 1),
            brief=brief,
        )

        key = cache_key(page, f"{PROMPT_VERSION}:{brief}", model)
        if (cached := cache_get(conn, key)) is not None:
            # Report the tokens the cold run actually cost, flagged as cached, so a
            # warm run does not silently understate the pipeline's real price.
            payload = cached.get("tags", cached)
            spent = cached.get("usage", {})
            record(Usage(model=model, from_cache=True,
                         input_tokens=spent.get("input_tokens", 0),
                         output_tokens=spent.get("output_tokens", 0)))
            done += 1
            return page.number, PageTags.model_validate(payload)

        async with semaphore:
            tags, usage, ok = await call_with_retry(provider, prompt, PageTags, model)

        record(usage)
        # Never memoise a failed call — and never a stub result: a keyless run must
        # not poison the cache that a later real run will read.
        if ok and not usage.model.startswith("stub:"):
            cache_put(conn, key, {
                "tags": tags.model_dump(),
                "usage": {"input_tokens": usage.input_tokens,
                          "output_tokens": usage.output_tokens},
            })
        elif not ok:
            print(f"    ! page {page.number} left untagged — will retry on next run")
        done += 1
        if done % 10 == 0:
            print(f"    tagged {done}/{len(pages)} pages")
        return page.number, tags

    results = await asyncio.gather(*(tag_one(p) for p in pages))
    return dict(results)


async def call_with_retry(
    provider: Provider,
    prompt: str,
    schema: type[T],
    model: str,
    attempts: int = 5,
    base_delay: float = 4.0,
) -> tuple[T, Usage, bool]:
    """Free-tier quotas mean 429s are routine, not exceptional — back off and retry.

    The spec waives error handling, but a rate-limited run that dies half way through
    is a demo that cannot be recorded, so this much is worth keeping.
    """
    for attempt in range(attempts):
        try:
            result, usage = await provider.complete(prompt, schema, model)
            return result, usage, True
        except Exception as exc:                     # noqa: BLE001 - provider-agnostic
            transient = any(t in str(exc).lower() for t in ("429", "resource_exhausted",
                                                            "quota", "503", "unavailable",
                                                            "500", "deadline"))
            if "429" in str(exc) and attempt == 0 and "quota" in str(exc).lower():
                # Distinguish "model has no quota at all" from "slow down": a model
                # that 429s instantly on the first attempt is misconfigured.
                print(f"    ! {model}: quota exhausted on first attempt")
            if not transient or attempt == attempts - 1:
                print(f"    ! {type(exc).__name__}: {str(exc)[:110]}")
                return schema(), Usage(model=model), False
            await asyncio.sleep(base_delay * (2 ** attempt))
    return schema(), Usage(model=model), False


# ─────────────────────────────────────────────────────────────────────────────
# 6. Matching parameters to pages
# ─────────────────────────────────────────────────────────────────────────────

FALLBACK_PAGES = 2   # pages sent to a parameter no page cleared the threshold for


def fallback_pages(
    spec: ParameterSpec,
    tags: dict[int, PageTags],
    candidates: dict[str, set[int]] | None,
    pages: list[Page],
) -> list[int]:
    """No page cleared the relevance threshold — pick the best pages available anyway.

    The spec is explicit that every parameter, including one expected to be absent,
    is sent a prompt AND pages "as usual". An empty page set would make the model
    conclude absence from an empty prompt, which proves nothing. Kept small
    (FALLBACK_PAGES) so a genuinely absent parameter stays cheap.

    Preference order: strongest sub-threshold tagger signal, then lexical-prefilter
    hits, then the document's opening pages.
    """
    scored = [
        (rel.score, number)
        for number, page_tags in tags.items()
        for rel in page_tags.relevance
        if rel.parameter == spec.name and rel.score > 0
    ]
    if scored:
        scored.sort(key=lambda item: (-item[0], item[1]))
        return sorted({number for _, number in scored[:FALLBACK_PAGES]})

    hits = sorted((candidates or {}).get(spec.name, set()))
    if hits:
        return hits[:FALLBACK_PAGES]

    return [p.number for p in pages[:FALLBACK_PAGES]]


# Page-role nudges added to the tagger's relevance score during routing, per family.
# Metadata gets its own column, and it is close to the inverse of the default: with no
# running header to read them from, the publisher and the tender's name live on the
# title page, so a cover outranks a body page. For every other family the binding
# clause is in the body and the cover is a restatement of it, so the default holds.
ROLE_BONUS: dict[str, dict[str, float]] = {
    "metadata": {"cover": 0.9, "body": 0.0, "form": -0.3, "appendix": -0.4, "toc": -1.0},
    "_default": {"body": 0.3, "cover": 0.2, "form": -0.3, "appendix": -0.4, "toc": -1.0},
}


def match_parameters_to_pages(
    tags: dict[int, PageTags],
    specs: list[ParameterSpec],
    pages: list[Page],
    meta: DocumentMeta,
    threshold: int = RELEVANCE_THRESHOLD,
    candidates: dict[str, set[int]] | None = None,
) -> dict[str, list[int]]:
    """parameter -> [2, 3, 7] — a required output of the assignment, not just a step.

    Two routes, chosen by parameter FAMILY (config-driven, never by parameter name):

    * metadata  — answered from the running header when the document HAS one. The
                  stamp repeats, so it is not page content: cite the page it first
                  appears on and send no body pages at all.
    * everything else — ranked by the tagger's relevance score, nudged by page role,
                  then capped. Sending few pages is a graded objective, so this ranks
                  and caps rather than collecting everything plausible.

    An unstamped document takes the second route for metadata too, and that is the
    whole point of the `has_running_header` gate. The tagger has already scored every
    page for client_name and tender_name, so the ranking route needs no extra call —
    and because nothing was stripped from an unstamped document, the tagger saw those
    pages complete. The two paths compose: strip the stamp and read it as metadata, or
    strip nothing and read the pages. What must never happen is the third thing, which
    is what this used to do — route to a header that is not there.

    A parameter with no page over the threshold still gets pages (fallback_pages) —
    never an empty set.
    """
    by_number = {p.number: p for p in pages}

    page_map: dict[str, list[int]] = {}
    for spec in specs:
        if spec.family == "metadata" and meta.has_running_header:
            page_map[spec.name] = [meta.first_page]
            continue

        role_bonus = ROLE_BONUS.get(spec.family, ROLE_BONUS["_default"])
        scored: list[tuple[float, int]] = []
        for number, page_tags in tags.items():
            for rel in page_tags.relevance:
                if rel.parameter != spec.name or rel.score < threshold:
                    continue
                page = by_number[number]
                density = min(len(rel.evidence), 60) / 600      # tie-break on evidence length
                scored.append((rel.score + role_bonus.get(page.page_role, 0.0) + density, number))

        scored.sort(key=lambda item: (-item[0], item[1]))
        chosen = sorted(number for _, number in scored[: spec.max_pages])
        page_map[spec.name] = chosen or fallback_pages(spec, tags, candidates, pages)
    return page_map


def looks_truncated(page: Page, min_chars: int = 0) -> bool:
    """Does this page's text run past the page edge mid-thought?

    Two conditions, both needed. A page ending without terminal punctuation is the
    obvious signal, but on its own it fires on nearly every page — headings, table
    cells and signature lines rarely end in a full stop. So the page must ALSO be
    reasonably full: a short page ending mid-sentence is a layout artefact, not a
    clause continuing overleaf.
    """
    tail = page.raw_text.rstrip()
    if not tail or page.char_count < min_chars:
        return False
    return tail[-1] not in ".:;!?)״\"'"


def starts_new_section(page: Page) -> bool:
    """Does this page open a new clause rather than continue the previous one?

    A numbered heading, a Hebrew letter marker (א. ב. ג.) or a bullet means a fresh
    clause starts here — so the previous page was not cut off mid-thought, whatever
    its last character looked like.
    """
    for line in page.body_text.split("\n"):
        line = line.strip()
        if not line or line.isdigit():        # blank, or a stray page number
            continue
        return bool(re.match(rf"^(\d+(?:\.\d+)*\.?|[{HEBREW_LETTERS}]['\u05f3]?\.|[\u25aa\u2022\-])\s",
                             line + " "))
    return False


def expand_windows_if_truncated(
    page_numbers: Iterable[int],
    pages: list[Page],
    density: float = 0.6,
) -> list[int]:
    """Conditional, never automatic.

    Blanket +/-1 expansion would roughly triple the pages sent per parameter, working
    directly against a goal the assignment grades. Only a page that genuinely looks
    cut off earns its successor.
    """
    by_number = {p.number: p for p in pages}
    sizes = sorted(p.char_count for p in pages)
    typical = sizes[len(sizes) // 2] if sizes else 0
    min_chars = int(typical * density)

    expanded = set(page_numbers)
    for number in list(expanded):
        page = by_number.get(number)
        following = by_number.get(number + 1)
        if (page and following
                and looks_truncated(page, min_chars)
                and not starts_new_section(following)):
            expanded.add(number + 1)
    return sorted(expanded)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Extraction — modular prompts, reused across parameters of the same family
# ─────────────────────────────────────────────────────────────────────────────

class ExtractedValue(BaseModel):
    """What the extractor returns. Constrained by schema, never parsed from free text."""
    status: str = Field(default="not_found", description='"found" או "not_found" בלבד')
    answer: str = Field(default="", description="הערך עצמו, בעברית, תמציתי")
    details: str = Field(default="", description="הרחבה/פרשנות בעברית — לא חזרה על answer")
    quote: str = Field(default="", description="ציטוט מדויק מהמסמך, מילה במילה")
    locator: str = Field(default="", description='מיקום בעמוד: "כותרת" / "סעיף 4.2" / "טבלה"')
    source_pages: list[int] = Field(default_factory=list, description="מספרי העמודים ששימשו")
    confidence: int = Field(default=1, ge=1, le=5, description="1-5, ודאות בערך שחולץ")


# One shared preamble + one short instruction per family. This is the reuse the task
# asks for: seven parameters are served by three prompt bodies, not seven.
EXTRACTION_SHARED = """אתה מחלץ מידע ממסמכי מכרז בעברית.
תקבל קטעי טקסט מתוך מכרז, ועליך לחלץ פרמטר אחד בלבד.

כללים מחייבים:
- ענה אך ורק על סמך הטקסט שניתן לך. אל תשלים מידע מהידע הכללי שלך.
- אם הפרמטר אינו מופיע בטקסט — החזר status="not_found" והשאר answer ו-details ריקים.
  זו תשובה לגיטימית ונכונה. עדיף "לא נמצא" מאשר ניחוש.
- ב-quote העתק ציטוט קצר ומדויק מהטקסט, מילה במילה, ללא שינוי.
- ב-source_pages ציין רק עמודים שמהם באמת נלקח המידע.
- ב-locator ציין מיקום קצר אחד בלבד (למשל "סעיף 5" או "כותרת" או "טבלה").
  אל תכתוב מספרי עמודים ב-locator.
- answer = הערך עצמו, קצר. details = הרחבה, הקשר או תנאים נלווים — לא חזרה על answer.
- confidence: 5 = הערך מפורש וחד-משמעי; 3 = נדרש פירוש; 1 = ספק ממשי."""

FAMILY_INSTRUCTIONS = {
    "metadata": """סוג הפרמטר: פרטי זיהוי של המכרז.
המידע מופיע בעמוד השער, בכותרת חוזרת אם יש כזו, או בסעיף הפתיחה של המכרז.
answer צריך להיות קצר ומדויק — שם הגוף או שם המכרז כפי שהוא מופיע, ללא תוספות.""",

    "atomic": """סוג הפרמטר: עובדה בודדת ומוגדרת (סכום, משך זמן, תאריך, תנאי).
answer צריך להכיל את הערך המספרי/הזמני המדויק כולל יחידות (למשל "24 חודשים", "10,000 ₪").
details יכיל תנאים נלווים: אופציות הארכה, תוקף, סוג הערבות, תנאי מימוש.
אם אותו ערך מופיע גם בנספח וגם בסעיף מחייב — הסתמך על הסעיף המחייב.""",

    "list_or_table": """סוג הפרמטר: רשימה או טבלה מרובת פריטים.
answer יכיל את הרשימה המלאה, כל פריט בשורה נפרדת וממוספר.
אל תשמיט פריטים ואל תסכם — הפריטים הם המהות.
אם המידע פרוס על כמה עמודים, אחד אותו לרשימה אחת רציפה.
עבור שיטת הערכה: ציין את המשקלות באחוזים (למשל "60% איכות, 40% מחיר").""",
}


def render_pages_for_prompt(pages: list[Page], numbers: list[int]) -> str:
    """Concatenate the routed pages with explicit markers so citations stay resolvable."""
    by_number = {p.number: p for p in pages}
    blocks = []
    for number in sorted(numbers):
        page = by_number.get(number)
        if page:
            blocks.append(f"[עמוד {number}]\n{page.body_text.strip()}")
    return "\n\n".join(blocks)


def build_extraction_prompt(
    spec: ParameterSpec,
    pages: list[Page],
    numbers: list[int],
    meta: DocumentMeta,
) -> str:
    """Shared preamble + family instruction + the parameter + the routed text."""
    family = FAMILY_INSTRUCTIONS.get(spec.family, FAMILY_INSTRUCTIONS["atomic"])

    if spec.family == "metadata" and meta.has_running_header:
        header = "\n".join(normalize_hebrew(ln) for ln in meta.header_text.split("\n"))
        # State what the stamp IS — a header on N of M pages. The previous wording,
        # "appears on every page", was a fact about the sample document rather than
        # about documents, and on an unstamped tender it introduced an empty block
        # under a label insisting the answer was in it.
        body = (f"[כותרת חוזרת — מופיעה ב-{meta.pages_covered} מתוך {meta.page_count} "
                f"עמודים, החל מעמוד {meta.first_page}]\n{header}")
        extra = render_pages_for_prompt(pages, numbers)
        if extra:
            body = f"{body}\n\n{extra}"
    else:
        body = render_pages_for_prompt(pages, numbers) or "(לא נמצאו עמודים רלוונטיים)"

    return "\n\n".join([
        EXTRACTION_SHARED,
        family,
        f"הפרמטר לחילוץ: {spec.hebrew_name} ({spec.name})\nהגדרה: {spec.description}",
        "הטקסט:",
        body,
    ])


async def extract_all(
    page_map: dict[str, list[int]],
    pages: list[Page],
    specs: list[ParameterSpec],
    provider: Provider,
    meta: DocumentMeta,
    model: str = MODELS["extractor"],
    concurrency: int = 3,
    fallbacks: list[str] | None = None,
) -> list[Extraction]:
    """One call per parameter, over only its routed pages."""
    semaphore = asyncio.Semaphore(concurrency)
    if fallbacks is None:
        fallbacks = EXTRACTOR_FALLBACKS

    async def extract_one(spec: ParameterSpec) -> Extraction:
        numbers = page_map.get(spec.name, [])
        prompt = build_extraction_prompt(spec, pages, numbers, meta)

        async with semaphore:
            for candidate in [model, *fallbacks]:
                value, usage, ok = await call_with_retry(
                    provider, prompt, ExtractedValue, candidate
                )
                record(usage)
                if ok:
                    break
                print(f"    ! {spec.name}: {candidate} failed, trying fallback")

        if not ok:
            print(f"    !! {spec.name}: extraction FAILED — reported as an error, "
                  f"not as 'not found'")

        found = ok and value.status == "found" and bool(value.answer.strip())
        cited = [n for n in value.source_pages if n in numbers] or numbers
        return Extraction(
            parameter=spec.name,
            status="found" if found else "not_found",
            answer=value.answer.strip() if found else "",
            details=value.details.strip() if found else "",
            pages=cited if found else [],
            locator=value.locator.strip(),
            quote=value.quote.strip(),
            score=value.confidence if found else UNVERIFIED_SCORE,
            error=not ok,
        )

    return list(await asyncio.gather(*(extract_one(spec) for spec in specs)))


# ─────────────────────────────────────────────────────────────────────────────
# 8. Verification — a second, different model, plus a deterministic evidence check
# ─────────────────────────────────────────────────────────────────────────────

def compare_answers(first: Extraction, second: ExtractedValue) -> tuple[str, str]:
    """Deterministic comparison of the two independent runs. Returns (verdict, reason).

    No model ever judges another model's work here — the second run is blind
    (see verify_and_score), and this comparison is plain text arithmetic, so the
    resulting agreement cannot be an artifact of anchoring.
    """
    first_found = first.status == "found" and bool(first.answer.strip())
    second_found = second.status == "found" and bool(second.answer.strip())

    if not first_found and not second_found:
        return "agree", "שתי הריצות קראו את אותם עמודים ולא מצאו את הפרמטר"
    if first_found != second_found:
        which = "השנייה" if second_found else "הראשונה"
        return "disagree", f"רק הריצה {which} מצאה ערך"

    a, b = normalize_hebrew(first.answer), normalize_hebrew(second.answer)
    if a == b or a in b or b in a:
        return "agree", "אותו ערך בשתי הריצות"

    # Tokenise past punctuation: "מחיר," must match "מחיר", "40%" must match "40%".
    token = re.compile(rf"[{HEBREW_LETTERS}A-Za-z0-9%₪]+")
    words_a = {w for w in token.findall(a) if len(w) > 1}
    words_b = {w for w in token.findall(b) if len(w) > 1}
    overlap = len(words_a & words_b) / max(1, min(len(words_a), len(words_b)))
    if overlap >= 0.8:
        return "agree", "אותו ערך במהות, הבדלי ניסוח בלבד"
    if overlap >= 0.4:
        return "partial", "חפיפה חלקית בין שתי הריצות"
    return "disagree", "שתי הריצות מצאו ערכים שונים"


def final_score(item: Extraction, judge_score: int, verdict: str) -> int:
    """The model supplies the number; verifiable evidence caps it.

    For a FOUND value the judge's number is the ceiling and the deterministic checks pull
    it down. For a NOT-FOUND value the score is the agreement between the two models, which
    is what the spec asks for: "comparing the results of the two runs, saving a certainty
    score based on the comparison". A confident, agreed absence is a correct answer and
    scores 5.

    Two things still lower it, and both are about not having looked properly rather than
    about disagreement:
      * coverage_complete False -> some pages never tagged, so routing may have missed the
        page that held the answer. No absence claim deserves a high score. -> 3
      * the call failed -> we never got an answer at all. -> 1
    """
    if item.error:
        return UNVERIFIED_SCORE          # we never got an answer — claim nothing

    if verdict == "unverified":
        # The second run's call failed, so there is no second opinion to score. This is
        # NOT low confidence in the value — it is absence of confirmation, and collapsing
        # the two would let a rate limit masquerade as a quality judgment.
        if item.status != "found":
            return 3                      # cannot confirm an absence on one reading
        return 3 if item.grounded else 2  # extracted, quote checks out, but unconfirmed

    if item.status != "found":
        if not item.coverage_complete:
            return 3                      # routing was built on incomplete tags
        if verdict == "disagree":
            return 2                      # the second run DID find something — real conflict
        # Both models read the routed text and agree the value is absent. That
        # agreement is the score, exactly as it is for a found value.
        #
        # max_relevance is NOT allowed to lower this. A high tagger score means the
        # cheapest model, triaging one page in isolation, thought the answer might be
        # there — while two stronger models that actually read the text disagree. The
        # tagger loses that argument. It is recorded as absence_contested in the
        # diagnostics instead of being hidden inside the number.
        return 5

    score = judge_score
    if verdict == "disagree":
        score = min(score, 2)
    elif verdict == "partial":
        score = min(score, 3)
    if not item.grounded:                # the cited quote is not on the cited page
        score = min(score, 3)
    return max(1, min(5, score))


def check_grounded(
    quote: str,
    pages: list[Page],
    page_numbers: list[int],
    meta: DocumentMeta | None = None,
    min_overlap: float = 0.8,
) -> bool:
    """Does the cited quote actually appear where it says it does?

    Deterministic — no LLM — which is exactly why it can be trusted as a hallucination
    check. Searches raw_text (not body_text) plus the document header, because a quote
    may legitimately come from the running header that body_text has removed.

    Exact substring first; otherwise word overlap, since a model may re-space or clip
    a quote without inventing it.
    """
    if not quote:
        return False

    haystack = normalize_hebrew(" ".join(
        p.raw_text for p in pages if p.number in page_numbers
    ))
    if meta is not None:
        haystack += " " + normalize_hebrew(meta.header_text)
    if not haystack.strip():
        return False

    needle = normalize_hebrew(quote)
    if needle[:40] and needle[:40] in haystack:
        return True

    words = [w for w in needle.split() if len(w) > 1]
    if not words:
        return False
    present = sum(w in haystack for w in words)
    if present / len(words) >= min_overlap:
        return True

    # Last tier: compare letters only, spacing and punctuation discarded on both sides.
    # A tracked-out page that despace_hebrew could not fully close — "בע\n מ \"" keeps its
    # newline, so the page still reads "בע מ" where the model correctly writes "בע\"מ" —
    # otherwise makes a MORE accurate answer look LESS grounded, which is exactly
    # backwards for a hallucination check. Only reached once the stricter tests have
    # failed, so it can turn a false negative into a pass but never invent one.
    squeezed = re.sub(rf"[^{HEBREW_LETTERS}A-Za-z0-9]", "", haystack)
    bare = [w for w in (re.sub(rf"[^{HEBREW_LETTERS}A-Za-z0-9]", "", w) for w in words) if len(w) > 1]
    if not bare:
        return False
    return sum(w in squeezed for w in bare) / len(bare) >= min_overlap


def attach_absence_evidence(
    extractions: list[Extraction],
    tags: dict[int, PageTags],
) -> list[Extraction]:
    """How strongly did ANY page in the document react to this parameter?

    This is what justifies a confident "not found": the whole document was tagged and
    nothing matched — evidence the judge cannot supply, because a not-found parameter
    is handed no pages to read.
    """
    complete = all(page_tags.relevance for page_tags in tags.values())
    for item in extractions:
        item.max_relevance = max(
            (rel.score for page_tags in tags.values()
             for rel in page_tags.relevance if rel.parameter == item.parameter),
            default=0,
        )
        item.coverage_complete = complete
    return extractions


async def verify_and_score(
    extractions: list[Extraction],
    pages: list[Page],
    provider: Provider,
    specs: list[ParameterSpec],
    page_map: dict[str, list[int]],
    meta: DocumentMeta | None = None,
    model: str = MODELS["judge"],
    concurrency: int = 2,
) -> list[Extraction]:
    """Two independent signals per parameter: a blind second run, and a check with no model.

    The second run is BLIND: it gets the identical prompt and the identical pages the
    extractor got — and nothing else. It never sees the first answer, so the agreement
    between the two runs (compare_answers, deterministic) measures extraction
    confidence rather than a model's willingness to endorse what it was shown.
    The second model is a different family from the extractor, so their errors are less
    correlated than a same-model self-check would be — though on one provider this is
    cross-SIZE rather than cross-vendor independence, which is weaker. Stated, not hidden.
    """
    by_name = {spec.name: spec for spec in specs}
    semaphore = asyncio.Semaphore(concurrency)

    async def verify_one(item: Extraction) -> Extraction:
        # Deterministic first — it costs nothing and never fails.
        item.grounded = check_grounded(item.quote, pages, item.pages, meta)

        spec = by_name.get(item.parameter)
        if spec is None or meta is None:
            return item

        # The blind second run: same prompt, same pages, different model.
        prompt = build_extraction_prompt(spec, pages, page_map.get(spec.name, []), meta)
        async with semaphore:
            second, usage, ok = await call_with_retry(provider, prompt, ExtractedValue, model)
        record(usage)

        if not ok:
            item.agreement = "unverified"       # distinct from a real disagreement
            item.score = final_score(item, UNVERIFIED_SCORE, "unverified")
            return item

        verdict, reason = compare_answers(item, second)
        item.agreement = verdict
        item.judge_answer = second.answer.strip()
        item.judge_reason = reason
        item.score = final_score(item, second.confidence, verdict)
        return item

    return list(await asyncio.gather(*(verify_one(item) for item in extractions)))


# ─────────────────────────────────────────────────────────────────────────────
# 9. Output — must match their contract exactly: answer / details / source / score
# ─────────────────────────────────────────────────────────────────────────────

def clean_locator(locator: str) -> str:
    """Keep the locator short and free of page numbers.

    format_source_hebrew() already writes the pages, so a model-supplied locator like
    "עמודים 6-7, סעיף 3" would render as "עמודים 6-7, סעיף 3, עמודים 6-7".
    """
    if not locator:
        return ""
    parts = [seg.strip() for seg in locator.split(",")]
    kept = [seg for seg in parts if seg and not re.match(r"^עמוד(ים)?\s*[\d\s,\-]*$", seg)]
    return kept[0][:40] if kept else ""


def format_source_hebrew(page_numbers: list[int], locator: str = "") -> str:
    """Their example uses a Hebrew phrase, not a list: "עמוד 2, פסקה ראשונה"."""
    if not page_numbers:
        return NOT_FOUND_SOURCE

    ordered = sorted(set(page_numbers))
    if len(ordered) == 1:
        source = f"עמוד {ordered[0]}"
    elif ordered == list(range(ordered[0], ordered[-1] + 1)):
        source = f"עמודים {ordered[0]}-{ordered[-1]}"
    else:
        source = "עמודים " + ", ".join(str(n) for n in ordered)

    locator = clean_locator(locator)
    return f"{source}, {locator}" if locator else source


def build_output(extractions: list[Extraction]) -> dict[str, dict[str, Any]]:
    """Render the internal record down to the four required keys — nothing more."""
    results: dict[str, dict[str, Any]] = {}
    for item in extractions:
        found = item.status != "not_found" and bool(item.answer)
        results[item.parameter] = {
            "answer": item.answer if found else "",
            "details": item.details if found else "",
            "source": format_source_hebrew(item.pages, item.locator) if found else NOT_FOUND_SOURCE,
            "score": item.score,
        }
    return results


def build_diagnostics(
    extractions: list[Extraction],
    page_map: dict[str, list[int]],
    pages: list[Page] | None = None,
    meta: DocumentMeta | None = None,
) -> dict[str, Any]:
    """Kept OUT of results.json so the required format stays exactly as specified."""
    pages_sent = [len(v) for v in page_map.values()] or [0]
    document: dict[str, Any] = {}
    if pages:
        # Structural features are computed during ingest; surfacing them here is what
        # stops them being dead weight, and they are the first thing to check when a
        # document behaves oddly.
        document = {
            "pages": len(pages),
            "rtl_repaired": any(p.rtl_repaired for p in pages),
            "pages_despaced": sum(p.despaced for p in pages),
            "pages_with_section_title": sum(1 for p in pages if p.section_title),
            "pages_with_table": sum(1 for p in pages if p.has_table),
            "page_roles": {role: sum(1 for p in pages if p.page_role == role)
                           for role in sorted({p.page_role for p in pages})},
        }
    if meta is not None:
        # Which route the metadata parameters took, and why. Without this the two
        # routes are indistinguishable in the output, and "no running header" — the
        # single biggest fork in how this document was read — stays invisible.
        document["running_header"] = {
            "detected": meta.has_running_header,
            "pages_covered": meta.pages_covered,
            "coverage": round(meta.coverage, 2),
            "first_page": meta.first_page,
            "metadata_route": "header" if meta.has_running_header else "relevance",
        }
    return {
        "document": document,
        "pages_sent_per_parameter": {k: len(v) for k, v in page_map.items()},
        "avg_pages_sent": round(sum(pages_sent) / len(pages_sent), 2),
        "pages_actually_cited": {e.parameter: len(e.pages) for e in extractions},
        "groundedness": {e.parameter: e.grounded for e in extractions},
        "agreement": {e.parameter: e.agreement for e in extractions},
        "judge_independent_answer": {e.parameter: e.judge_answer[:120] for e in extractions},
        "judge_reason": {e.parameter: e.judge_reason[:160] for e in extractions},
        "final_score": {e.parameter: e.score for e in extractions},
        "max_relevance_any_page": {e.parameter: e.max_relevance for e in extractions},
        # Absent per both models, yet some page looked relevant to the tagger. Not a
        # score penalty — a flag worth eyeballing.
        "absence_contested": [e.parameter for e in extractions
                              if e.status != "found" and e.max_relevance >= 2],
        "extraction_errors": [e.parameter for e in extractions if e.error],
        "llm_calls": len(CALLS),
        "cache_hits": sum(1 for c in CALLS if c.from_cache),
        "input_tokens": sum(c.input_tokens for c in CALLS),
        "output_tokens": sum(c.output_tokens for c in CALLS),
        # Provider-side prompt caching is NOT used — this stays 0 and says so honestly.
        "provider_cached_tokens": sum(c.cached_tokens for c in CALLS),
    }


def record_run(outcome: dict[str, Any], llm: "LLMSetup", pdf_path: Path) -> None:
    """Append one line per run to output/runs.jsonl — the app's Monitoring tab reads it.

    Lives inside process_document, so every run lands here — CLI, Streamlit, and the
    comparison drivers alike — and KPI history accumulates without anyone remembering
    to log. Append-only JSONL: a crashed run simply leaves no line, never a torn file.
    """
    diag = outcome["diagnostics"]
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pdf": pdf_path.name,
        "provider": llm.name,
        "extractor": llm.models.get("extractor", ""),
        "elapsed_s": round(outcome["elapsed"], 1),
        "avg_pages": diag["avg_pages_sent"],
        "grounded": sum(1 for v in diag["groundedness"].values() if v),
        "found": sum(1 for v in outcome["results"].values() if v["answer"]),
        "score_total": sum(diag["final_score"].values()),
        "llm_calls": diag["llm_calls"],
        "cache_hits": diag["cache_hits"],
        "input_tokens": diag["input_tokens"],
        "output_tokens": diag["output_tokens"],
        "errors": len(diag["extraction_errors"]),
        "results": {k: {"answer": v["answer"][:80], "source": v["source"], "score": v["score"]}
                    for k, v in outcome["results"].items()},
    }
    with (OUTPUT_DIR / "runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def save_and_print(results: dict[str, Any], path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n=== {label} -> {path} ===")
    print(json.dumps(results, ensure_ascii=False, indent=2))


def report_kpis(elapsed: float, diagnostics: dict[str, Any]) -> None:
    print("\n=== KPIs ===")
    print(f"  wall clock            : {elapsed:.1f}s")
    print(f"  avg pages / parameter : {diagnostics['avg_pages_sent']}")
    print(f"  LLM calls             : {diagnostics['llm_calls']} "
          f"({diagnostics['cache_hits']} from cache)")
    print(f"  tokens in / out       : {diagnostics['input_tokens']} / {diagnostics['output_tokens']}")
    if diagnostics["extraction_errors"]:
        print(f"  !! FAILED (not 'not found'): {', '.join(diagnostics['extraction_errors'])}")
        print("     These results are unreliable — re-run to retry them.")


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

async def process_document(
    pdf_path: Path = PDF_PATH,
    on_stage: "Callable[[str], None] | None" = None,
    llm: LLMSetup | None = None,
) -> dict[str, Any]:
    """Run the whole pipeline and RETURN the results.

    Separated from run_pipeline() so the same code serves the CLI and the UI —
    computation returns data, presentation is somebody else's job. The UI passes
    its own LLMSetup (provider toggle + key); the CLI resolves one from the env.
    """
    started = time.time()
    say = on_stage or (lambda message: None)

    CALLS.clear()          # per-run ledger: a second document in the same process
                           # must not inherit the first one's token accounting
    conn = init_storage()
    llm = llm or setup_llm()
    provider = llm.provider

    say("reading parameters")
    specs = load_parameters()

    say("reading the PDF")
    pages = load_document(pdf_path)
    boilerplate = detect_boilerplate(pages)
    meta = build_document_meta(pages, boilerplate)
    pages = enrich_pages_structurally(strip_boilerplate(pages, boilerplate))

    say(f"tagging {len(pages)} pages (slowest step — cached after the first run)")
    candidates = lexical_prefilter(pages, specs)
    tags = await tag_pages(pages, specs, candidates, provider, conn, model=llm.models["tagger"])

    say("routing parameters to pages")
    page_map = match_parameters_to_pages(tags, specs, pages, meta, candidates=candidates)
    budgets = {spec.name: spec.max_pages for spec in specs}
    page_map = {
        name: expand_windows_if_truncated(numbers, pages)[: budgets.get(name, MAX_PAGES_PER_PARAM)]
        for name, numbers in page_map.items()
    }

    say("extracting values")
    extractions = await extract_all(page_map, pages, specs, provider, meta,
                                    model=llm.models["extractor"], fallbacks=llm.fallbacks)

    extractions = attach_absence_evidence(extractions, tags)

    say("verifying with a second model")
    extractions = await verify_and_score(extractions, pages, provider, specs, page_map, meta,
                                         model=llm.models["judge"])

    conn.close()
    outcome = {
        "results": build_output(extractions),
        "page_map": page_map,
        "diagnostics": build_diagnostics(extractions, page_map, pages, meta),
        "pages": pages,
        "header": meta.header_text,
        "elapsed": time.time() - started,
    }
    record_run(outcome, llm, pdf_path)
    return outcome


async def run_pipeline(pdf_path: Path = PDF_PATH, prefix: str = "",
                       llm: LLMSetup | None = None) -> None:
    """CLI entry point: run, save, print."""
    print(f"starting... [{pdf_path.name}]")
    outcome = await process_document(pdf_path, on_stage=lambda m: print(f"  {m}"), llm=llm)

    save_and_print(outcome["page_map"], OUTPUT_DIR / f"{prefix}page_map.json", "parameter -> pages")
    save_and_print(outcome["results"], OUTPUT_DIR / f"{prefix}results.json", "results")
    save_and_print(outcome["diagnostics"], OUTPUT_DIR / f"{prefix}diagnostics.json", "diagnostics")
    report_kpis(outcome["elapsed"], outcome["diagnostics"])


# ─────────────────────────────────────────────────────────────────────────────
# Ablation — the experiment that justifies the whole architecture
# ─────────────────────────────────────────────────────────────────────────────

async def run_ablation(pdf_path: Path = PDF_PATH, llm: LLMSetup | None = None) -> None:
    """Compare routed extraction against sending the whole document per parameter.

    Token counts are MEASURED via the provider's tokeniser, not estimated from
    characters — Hebrew tokenises badly under a chars/4 rule. The one-off tagging cost
    is reported separately and honestly: it is real, it is paid once per document, and
    it is amortised across every parameter (and every future parameter).
    """
    print(f"=== ablation: {pdf_path.name} ===\n")

    pages = load_document(pdf_path)
    boilerplate = detect_boilerplate(pages)
    meta = build_document_meta(pages, boilerplate)
    pages = enrich_pages_structurally(strip_boilerplate(pages, boilerplate))
    specs = load_parameters()

    conn = init_storage()
    llm = llm or setup_llm()
    provider = llm.provider
    model = llm.models["extractor"]

    candidates = lexical_prefilter(pages, specs)
    tags = await tag_pages(pages, specs, candidates, provider, conn, model=llm.models["tagger"])
    page_map = match_parameters_to_pages(tags, specs, pages, meta, candidates=candidates)
    budgets = {spec.name: spec.max_pages for spec in specs}
    page_map = {
        name: expand_windows_if_truncated(nums, pages)[: budgets.get(name, MAX_PAGES_PER_PARAM)]
        for name, nums in page_map.items()
    }

    whole_document = "\n\n".join(f"[עמוד {p.number}]\n{p.body_text}" for p in pages)

    rows, routed_total, baseline_total = [], 0, 0
    for spec in specs:
        numbers = page_map.get(spec.name, [])
        routed_prompt = build_extraction_prompt(spec, pages, numbers, meta)
        baseline_prompt = "\n\n".join([
            EXTRACTION_SHARED,
            FAMILY_INSTRUCTIONS.get(spec.family, FAMILY_INSTRUCTIONS["atomic"]),
            f"הפרמטר לחילוץ: {spec.hebrew_name} ({spec.name})",
            "הטקסט:", whole_document,
        ])
        routed = provider.count_tokens(routed_prompt, model)
        baseline = provider.count_tokens(baseline_prompt, model)
        routed_total += routed
        baseline_total += baseline
        rows.append((spec.name, len(numbers), routed, baseline))

    # Measured from the prompts, so the number holds whether or not the cache was warm.
    by_number = {pg.number: pg for pg in pages}
    with_signal = set().union(*candidates.values()) if candidates else set()
    tagging_tokens = 0
    for page in pages:
        prompt = build_tagging_prompt(
            page, specs, by_number.get(page.number - 1), by_number.get(page.number + 1),
            brief=page.number not in with_signal,
        )
        tagging_tokens += provider.count_tokens(prompt, llm.models["tagger"])
    tagging_out = sum(c.output_tokens for c in CALLS) or int(0.17 * tagging_tokens)
    tagging_tokens += tagging_out

    print(f"{'parameter':24}{'pages':>7}{'routed tok':>12}{'whole-doc tok':>15}{'saving':>9}")
    for name, n_pages, routed, baseline in rows:
        saving = 1 - routed / baseline if baseline else 0
        print(f"{name:24}{n_pages:>7}{routed:>12,}{baseline:>15,}{saving:>8.0%}")

    print(f"\n{'TOTAL extraction':24}{'':>7}{routed_total:>12,}{baseline_total:>15,}"
          f"{1 - routed_total / baseline_total:>8.0%}")
    print(f"{'+ one-off tagging':24}{len(pages):>7}{tagging_tokens:>12,}{0:>15,}")
    print(f"{'= end to end':24}{'':>7}{routed_total + tagging_tokens:>12,}{baseline_total:>15,}"
          f"{1 - (routed_total + tagging_tokens) / baseline_total:>8.0%}")

    # Where does routing start paying for itself? Tagging is a fixed cost; each extra
    # parameter costs a few pages routed versus a whole document in the baseline.
    n = len(specs)
    per_param_routed = routed_total / n
    per_param_baseline = baseline_total / n
    margin = per_param_baseline - per_param_routed
    break_even = tagging_tokens / margin if margin > 0 else float("inf")

    print(f"\nDocument: {len(pages)} pages, {n} parameters.")
    print(f"  per parameter : routed {per_param_routed:,.0f} tok vs "
          f"whole-doc {per_param_baseline:,.0f} tok")
    print(f"  break-even    : {break_even:.1f} parameters "
          f"— beyond this, routing is cheaper even counting tagging")
    print(f"  at {n} parameters the routed path saves "
          f"{baseline_total - routed_total - tagging_tokens:,} tokens")
    print("  tagging is paid ONCE per document and reused by every parameter, including")
    print("  parameters added later — the baseline pays a full document every time.")
    print(f"  re-runs hit the page-tag cache, so a second pass costs {routed_total:,} tokens.")
    conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract tender parameters by routing each one to its relevant pages."
    )
    parser.add_argument("--pdf", type=Path, default=PDF_PATH,
                        help="tender PDF to process")
    parser.add_argument("--prefix", default="",
                        help="prefix for output files, e.g. 'test_' for the holdout")
    parser.add_argument("--ablation", action="store_true",
                        help="measure routed vs whole-document cost instead of extracting")
    parser.add_argument("--provider", choices=["gemini", "anthropic", "local"], default=None,
                        help="LLM provider (default: whichever API key the environment has; "
                             "'local' talks to LM Studio at LMSTUDIO_BASE_URL or localhost:1234)")
    return parser.parse_args()


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    args = parse_args()
    llm = setup_llm(args.provider)
    if args.ablation:
        asyncio.run(run_ablation(args.pdf, llm=llm))
    else:
        asyncio.run(run_pipeline(args.pdf, args.prefix, llm=llm))


if __name__ == "__main__":
    main()
