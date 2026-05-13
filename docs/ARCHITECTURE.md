# Architektura

## Hlavní pipeline (audio → notový zápis)

```
                ┌─────────────────────────────────────────────────┐
                │  Gradio Web UI  (app.py)                        │
                │  ├─ Audio input (upload + mikrofon)             │
                │  ├─ Kategorie hudby (monofonni/klavir/...)      │
                │  └─ Volitelný override tempo/klíč/takt          │
                └─────────────────┬───────────────────────────────┘
                                  │ audio_path, category, overrides
                                  ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  transcribe.transcribe()  (src/transcribe_app/transcribe.py)             │
│                                                                          │
│  ┌─ 1. AUDIO ANALÝZA ────────────────────────────────────────────┐      │
│  │   _detect_tempo_and_beats() — librosa.beat (octave-aware ÷2) │      │
│  │   _detect_key_full() — Krumhansl-Schmuckler (top-3 candid.)  │      │
│  │   _detect_time_signature_full() — librosa onset autokorel.   │      │
│  └────────────────────────────────────────────────────────────────┘      │
│                            │                                              │
│  ┌─ 2. VOKÁL IZOLACE (jen "vokal" kategorie) ─────────────────┐         │
│  │   _isolate_vocals()                                          │         │
│  │   ├─ default: audio-separator + BS-Roformer-Viperx-1297     │         │
│  │   └─ fallback: Demucs htdemucs                              │         │
│  └────────────────────────────────────────────────────────────┘         │
│                            │                                              │
│  ┌─ 3. AUDIO → MIDI ─────────────────────────────────────────┐          │
│  │   _basic_pitch_events() — Spotify Basic Pitch ONNX        │          │
│  │   → list[NoteEvent(pitch_midi, start_s, end_s, velocity)] │          │
│  └────────────────────────────────────────────────────────────┘          │
│                            │                                              │
│  ┌─ 4. POST-PROCESS HEURISTIKY ─────────────────────────────┐            │
│  │   _clean_events()                                          │            │
│  │   ├─ filter krátké noty < 50ms                            │            │
│  │   ├─ filter velocity < 15                                  │            │
│  │   ├─ suprese oktávových duplikátů (harmoniky)             │            │
│  │   └─ monofonni: keep highest per onset                    │            │
│  └────────────────────────────────────────────────────────────┘            │
│                            │                                              │
│  ┌─ 5. LLM CLEANUP (volitelný, LLM_CLEANUP=1) ────────────┐              │
│  │   cleanup_with_llm()  (llm_cleanup.py)                  │              │
│  │   ├─ build Claude client (Foundry/Anthropic/Entra ID)   │              │
│  │   ├─ posílá: events + rhythm features + librosa contraste│              │
│  │   ├─ Claude API (Opus/Sonnet/Haiku, structured output)  │              │
│  │   └─ vrací: tempo correction, key, drop_indices         │              │
│  └────────────────────────────────────────────────────────────┘              │
│                            │                                              │
│  ┌─ 6. MANUAL OVERRIDE ────────────────────────────────────┐              │
│  │   tempo_override / key_sharps_override /                │              │
│  │   time_signature_override → vždy přepíše auto-detekci    │              │
│  └──────────────────────────────────────────────────────────┘              │
│                            │                                              │
│                  TranscriptionResult(events, tempo, key, timesig)         │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  notation.events_to_musicxml()  (src/transcribe_app/notation.py)         │
│                                                                          │
│  ├─ bucket events by onset (akordové grouping, 50ms okno)               │
│  ├─ pokud široký pitch range: split na bass (< C4) + treble (≥ C4)      │
│  ├─ build music21 Score → Part(s) → Measures                            │
│  ├─ insert KeySignature, TimeSignature, MetronomeMark                   │
│  ├─ snap durations na 16th note grid                                    │
│  ├─ pad shortest part rests aby všechny party měly stejnou délku        │
│  └─ write 'musicxml'                                                     │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  render.render_musicxml()  (src/transcribe_app/render.py)                │
│                                                                          │
│  subprocess `mscore -o <out>.<fmt> <input>.musicxml`                    │
│  pro formáty: PDF, PNG (per-page), SVG, MID                              │
│  → dict[format → Path]                                                   │
└──────────────────────────────────┬───────────────────────────────────────┘
                                   │
                                   ▼
                Gradio UI vrací: 5 souborů + OSMD preview HTML
```

## Module map

```
src/transcribe_app/
├── __init__.py          # Monkey-patches pro madmom (collections + numpy)
├── preprocess.py        # librosa.load wrapper (nepoužíván aktuálně; transcribe to dělá inline)
├── transcribe.py        # HLAVNÍ orchestrator (řádky 42-100 = transcribe() funkce)
├── notation.py          # Note events → music21 → MusicXML
├── render.py            # MusicXML → MuseScore CLI → PDF/PNG/SVG/MID
├── llm_cleanup.py       # Claude API post-process (volitelný)
└── aria_amt.py          # Scaffolding pro budoucí Aria-AMT integraci (raises RuntimeError)

tests/
├── test_pipeline.py         # End-to-end smoke test (C-dur stupnice)
├── test_correctness.py      # LCS pitch class shoda vs ground truth
├── test_time_signature.py   # Time sig accuracy (Mutopia + Open Goldberg)
└── fixtures/
    ├── monofonni/, klavir/, kapela/, vokal/   # 4 syntetické fixtury (sine + harmonics)
    ├── mutopia/                                # 43 fixtur (MuseScore-rendered)
    │   ├── INDEX.json                          # registry
    │   └── download.py                         # idempotent regenerator
    └── real/                                   # 8 reálných (Open Goldberg, CC0)
        ├── INDEX.json
        └── download.py

app.py                       # Gradio UI, 2 záložky (Transkripce + Ukázky)
Dockerfile                   # Python 3.11-slim + MuseScore 3 + ffmpeg
```

## Dataflow během requestu (Gradio click → output)

1. **User upload** WAV → Gradio přijme do `/tmp/gradio/...`
2. **run_pipeline()** (app.py): nový workdir `/tmp/noty_<random>/`
3. **transcribe()** → audio → events + metadata
4. **events_to_musicxml()** → `workdir/transcription.musicxml`
5. **render_musicxml()** → `workdir/transcription.{pdf,png,svg,mid}`
6. Gradio vrátí 5 paths + HTML s OSMD render (base64-embedded musicxml)
7. Po skončení sessionu Gradio temp soubory promažou (TTL)

## Komunikace s externími službami

| Služba | Kdy | Auth |
|---|---|---|
| Anthropic API (first-party) | LLM_CLEANUP=1 + ANTHROPIC_API_KEY | API key |
| Microsoft Foundry | LLM_CLEANUP=1 + ANTHROPIC_FOUNDRY_RESOURCE | API key nebo Entra ID (DefaultAzureCredential) |
| HuggingFace (model váhy) | Při prvním stažení basic-pitch / demucs / madmom | Public |
| MVSEP (RoFormer váhy) | Při prvním vokal použití | Public |

Vše ostatní běží **lokálně** (Basic Pitch / music21 / MuseScore).
