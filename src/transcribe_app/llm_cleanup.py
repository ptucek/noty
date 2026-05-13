"""Volitelný post-process: Claude API navrhne opravy time signature / key / drop notes."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .notation import NoteEvent

logger = logging.getLogger(__name__)

DEFAULT_CLEANUP_MODEL = "claude-opus-4-6"  # Foundry default; první-party umí i 4-7
MAX_EVENTS_TO_SEND = 200  # bezpečnostní limit, aby kontext nevybouchl


class CleanupSuggestion(BaseModel):
    """Strukturovaná odpověď z Claude API — opravy pro transkripci.

    Time signature je úmyslně mimo — empiricky LLM zhoršoval baseline (51% → 35%),
    detekuje 2/4 a 6/8 lépe, ale destruuje časté 4/4 a 3/4. Necháváme librosa heuristiku.
    """

    tempo_bpm: int = Field(
        ge=40, le=240, description="Doporučené tempo v BPM."
    )
    key_sharps: int = Field(
        ge=-7, le=7, description="Předznamenání: -7 (Cb dur / Ab moll) až +7 (C# dur / A# moll)."
    )
    drop_indices: list[int] = Field(
        default_factory=list,
        description=(
            "Indexy not (0-based, viz vstupní seznam), které jsou pravděpodobně chyba "
            "transkripce (oktávové duplikáty, šum, sub-grid artefakty). Konzervativně — "
            "raději neodstranit, než smazat skutečnou notu."
        ),
    )
    rationale: str = Field(
        description="Krátké česky vysvětlení (1-2 věty), proč tyto úpravy."
    )


@dataclass(frozen=True)
class CleanupResult:
    suggestion: CleanupSuggestion
    cost_usd: float
    cache_read_tokens: int
    cache_write_tokens: int


SYSTEM_PROMPT = """Jsi hudební asistent specializovaný na čištění surové automatické \
hudební transkripce. Dostaneš seznam not detekovaných z audia (Basic Pitch model) \
spolu s předpočítanými rytmickými features a metadaty (tempo, tónina). Tvoje úloha \
je tří-složková:

## 1. TÓNINA — pitch class profile

V `rhythm_features.pitch_class_counts` máš počet not pro každou pc (0-11). Tóniny:
- C dur: silné C, E, G, F, D, A, H (0,4,7,5,2,9,11)
- G dur: G, H, D, C, A, E, F#  (sharps=+1)
- A moll: A, C, E, D, G, H, F  (sharps=0, mode=minor)

V `context_from_pipeline.key_candidates_top3` jsou top 3 KS kandidáti se score. Pokud \
detekovaná tonalita sedí na pitch class profil i KS score, ponech. Jinak oprav. \
Konzervativně — pokud si nejsi jistý, drž detekovanou tóninu.

## 2. TEMPO — IOI median + librosa beats

`rhythm_features.median_ioi_s` = medián intervalů mezi onsety. \
`context_from_pipeline.librosa_beat_times_first20` = librosa beat tracking timestamps. \
Pokud detekované tempo neodpovídá průměrnému beat-intervalu (60 / mean_interval), \
oprav na nejbližší celé desítky (60, 70, 80, ..., 200).

## 3. DROP NOTES — konzervativně

Smaž jen jednoznačné artefakty (krátké < 80ms s nízkou velocity, oktávové duplikáty \
ve stejném onsetu). Akordy a chromatické noty NEZAHRNUJ. Raději nic, než smazat \
skutečnou notu.

## NEZASAHUJEŠ do TAKTU

Time signature **neměníš ani neřešíš** — librosa heuristika na to má lepší přístup. \
V odpovědi ho ignoruj (není ani ve schématu).

Vrať VÝHRADNĚ JSON podle schématu. V `rationale` shrň své rozhodnutí o tempu, klíči \
a důvody pro drop_indices."""


def cleanup_with_llm(
    events: "list[NoteEvent]",
    tempo_bpm: float,
    key_sharps: int,
    key_name: str,
    time_signature: str,
    category: str,
    extra_context: dict | None = None,
) -> CleanupResult | None:
    """Pošle souhrn transkripce do Claude API, vrátí strukturovaný cleanup návrh.

    Vrací ``None`` pokud:
    - není nastaven ``ANTHROPIC_API_KEY``
    - není nastaven ``LLM_CLEANUP=1`` (opt-in default)
    - API volání selže
    """
    if os.environ.get("LLM_CLEANUP", "0") != "1":
        return None
    client, provider = _build_client()
    if client is None:
        return None

    if len(events) > MAX_EVENTS_TO_SEND:
        logger.info("Příliš mnoho not (%d > %d) → LLM cleanup přeskočen", len(events), MAX_EVENTS_TO_SEND)
        return None

    user_payload = {
        "category": category,
        "detected_tempo_bpm": round(tempo_bpm, 1),
        "detected_key": key_name,
        "detected_key_sharps": key_sharps,
        "detected_time_signature": time_signature,
        "note_count": len(events),
        "rhythm_features": _compute_rhythm_features(events, tempo_bpm),
        "context_from_pipeline": extra_context or {},
        "notes": [
            {
                "i": i,
                "midi": e.pitch_midi,
                "start_s": round(e.start_s, 3),
                "end_s": round(e.end_s, 3),
                "velocity": e.velocity,
            }
            for i, e in enumerate(events)
        ],
    }
    user_text = "Tady je transkripce ke kontrole:\n\n" + json.dumps(user_payload, indent=2)

    cleanup_model = os.environ.get("CLEANUP_MODEL", DEFAULT_CLEANUP_MODEL)
    try:
        response = client.messages.parse(
            model=cleanup_model,
            max_tokens=2048,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_text}],
            output_format=CleanupSuggestion,
        )
    except Exception as exc:
        logger.warning("Claude API selhala: %s → LLM cleanup přeskočen", exc)
        return None

    suggestion = response.parsed_output
    usage = response.usage
    cost_usd = _estimate_cost(usage)
    logger.info(
        "LLM cleanup (%s): tempo %d, klíč %+d, drop %d not (cost ~$%.4f, cache read %d)",
        provider, suggestion.tempo_bpm, suggestion.key_sharps,
        len(suggestion.drop_indices), cost_usd, getattr(usage, "cache_read_input_tokens", 0) or 0,
    )
    return CleanupResult(
        suggestion=suggestion,
        cost_usd=cost_usd,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


def _compute_rhythm_features(events: "list[NoteEvent]", tempo_bpm: float) -> dict:
    """Pre-computuje rytmické features pro LLM — místo aby je počítal v hlavě.

    Vrací:
    - median_ioi_s: medián intervalu mezi onsety
    - strong_onset_count: počet silných onsetů (velocity >= 70)
    - meter_histograms: pro každý kandidátní takt (2/4, 3/4, 4/4, 6/8) histogram
      silných onsetů modulo délka taktu — když jeden bin dominuje, sedí takt.
    - downbeat_contrast: pro každý kandidát, contrast = bin[0] / mean(bin[1:]).
      Vyšší = lepší shoda.
    - pitch_class_counts: počet not pro každou pitch class 0-11
    """
    import statistics

    if not events:
        return {
            "median_ioi_s": 0.0,
            "strong_onset_count": 0,
            "meter_histograms": {},
            "downbeat_contrast": {},
            "pitch_class_counts": [0] * 12,
        }

    sorted_starts = sorted(e.start_s for e in events)
    iois = [sorted_starts[i + 1] - sorted_starts[i] for i in range(len(sorted_starts) - 1)]
    iois = [x for x in iois if x > 1e-3]
    median_ioi = float(statistics.median(iois)) if iois else 0.0

    velocity_threshold = 70
    strong_onsets = sorted(e.start_s for e in events if e.velocity >= velocity_threshold)
    seconds_per_beat = 60.0 / tempo_bpm

    meter_histograms: dict[str, list[int]] = {}
    downbeat_contrast: dict[str, float] = {}
    candidates = {"2/4": 2, "3/4": 3, "4/4": 4, "6/8": 6}
    for label, beats in candidates.items():
        measure_s = beats * seconds_per_beat
        if label == "6/8":
            # 6/8 je často počítané jako dva tříosminové trsy → 2 silné doby
            buckets = [0] * 2
            bins = 2
            bin_width = measure_s / bins
        else:
            buckets = [0] * beats
            bins = beats
            bin_width = seconds_per_beat
        for onset in strong_onsets:
            pos_in_measure = onset % measure_s
            idx = min(bins - 1, int(pos_in_measure / bin_width))
            buckets[idx] += 1
        meter_histograms[label] = buckets
        if len(buckets) >= 2:
            others = [b for b in buckets[1:]] or [0]
            avg_others = sum(others) / len(others) if others else 0
            contrast = buckets[0] / avg_others if avg_others > 0 else float(buckets[0])
            downbeat_contrast[label] = round(contrast, 2)

    pc_counts = [0] * 12
    for e in events:
        pc_counts[e.pitch_midi % 12] += 1

    return {
        "median_ioi_s": round(median_ioi, 3),
        "strong_onset_count": len(strong_onsets),
        "meter_histograms": meter_histograms,
        "downbeat_contrast": downbeat_contrast,
        "pitch_class_counts": pc_counts,
    }


def _build_client() -> tuple[object | None, str]:
    """Postaví Claude API klienta podle dostupných env vars. Vrací (client, label) nebo (None, '').

    Priorita:
      1. ANTHROPIC_FOUNDRY_RESOURCE + ANTHROPIC_FOUNDRY_API_KEY  → Foundry (API key)
      2. ANTHROPIC_FOUNDRY_RESOURCE bez API key                  → Foundry (DefaultAzureCredential)
      3. ANTHROPIC_API_KEY                                       → first-party Anthropic
    """
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic SDK není dostupný → LLM cleanup přeskočen")
        return None, ""

    foundry_resource = os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE")
    foundry_key = os.environ.get("ANTHROPIC_FOUNDRY_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")

    if foundry_resource and foundry_key:
        return (
            anthropic.AnthropicFoundry(resource=foundry_resource, api_key=foundry_key),
            f"Foundry/{foundry_resource}",
        )
    if foundry_resource:
        try:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        except ImportError:
            logger.warning(
                "ANTHROPIC_FOUNDRY_RESOURCE nastaven, ale azure-identity chybí → `uv add azure-identity`"
            )
            return None, ""
        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://ai.azure.com/.default"
        )
        return (
            anthropic.AnthropicFoundry(
                resource=foundry_resource, azure_ad_token_provider=token_provider
            ),
            f"Foundry/{foundry_resource} (Entra ID)",
        )
    if anthropic_key:
        return anthropic.Anthropic(api_key=anthropic_key), "Anthropic (first-party)"

    logger.info(
        "Žádný API klíč nenalezen (ANTHROPIC_FOUNDRY_RESOURCE nebo ANTHROPIC_API_KEY) → LLM cleanup přeskočen"
    )
    return None, ""


def apply_cleanup(
    events: "list[NoteEvent]",
    suggestion: CleanupSuggestion,
) -> "list[NoteEvent]":
    """Aplikuje drop_indices na events. Vrátí nový seznam."""
    drop_set = set(suggestion.drop_indices)
    return [e for i, e in enumerate(events) if i not in drop_set]


def _estimate_cost(usage) -> float:
    """Opus 4.7: $5/1M input, $25/1M output. Cache read ~$0.50/1M, cache write ~$6.25/1M."""
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (
        input_tokens * 5.0e-6
        + output_tokens * 25.0e-6
        + cache_read * 0.5e-6
        + cache_write * 6.25e-6
    )
