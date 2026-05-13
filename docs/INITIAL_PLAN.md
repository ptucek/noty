# Notová transkripce z písničky — webová aplikace

## Context

Greenfield projekt v prázdném adresáři `noty/`. Cíl: webová aplikace, do které uživatel
nahraje audio (mp3/wav) **nebo** nahraje vstup z mikrofonu, **před spuštěním zvolí kategorii**
hudby (kvůli přesnosti), a dostane zpět notový zápis ve formátech **MusicXML + MIDI + PDF + PNG/SVG**.

Filozofie: **MVP nejdřív** — funkční pipeline za pár hodin postavený na ověřeném open-source.
Žádné AI/LLM v první fázi, žádné placené API. Jakmile MVP běží, iterujeme směrem ke kvalitě
(lepší modely) a UX (in-browser preview not). Cílový deployment: **Azure**.

**Compute předpoklady (potvrzeno):**
- Real-time výstup není potřeba — uživatel počká i několik minut na 60s vstup.
- Lokální vývoj: Apple Silicon (MPS kde to jde, jinak CPU fallback).
- Produkce: Azure Container Apps (CPU) → případně Azure ML GPU endpoint později.
- Computational cost není blocker → můžeme používat i těžší modely bez nutnosti GPU,
  pokud akceptujeme delší dobu zpracování (3-5× real-time na CPU je v pořádku).

---

## Architektura (MVP)

```
┌────────────────────────────────────────────────────────────────┐
│  Gradio UI                                                     │
│  ├─ gr.Audio(sources=["upload","microphone"])  (vstup)         │
│  ├─ gr.Radio(choices=[monofonní, klavír, kapela, vokál])       │
│  └─ Output: 4× gr.File (MusicXML, MIDI, PDF, PNG) + preview    │
└────────────────────────────┬───────────────────────────────────┘
                             │
┌────────────────────────────▼───────────────────────────────────┐
│  Python backend (modules)                                      │
│                                                                │
│  preprocess.py    → načte audio, resample na 22.05/44.1 kHz    │
│        │                                                       │
│        ▼                                                       │
│  transcribe.py    → router podle kategorie:                    │
│        ├─ monofonní  → Basic Pitch (s mono fallback CREPE)     │
│        ├─ klavír     → Basic Pitch (polyfonní mód)             │
│        ├─ kapela     → Basic Pitch na celý mix                 │
│        └─ vokál      → Demucs (htdemucs) izoluje vokál,        │
│                         pak Basic Pitch na izolovanou stopu    │
│        │                                                       │
│        ▼   (MIDI)                                              │
│  notation.py      → music21 načte MIDI, normalizuje na noty,   │
│                     exportuje MusicXML                         │
│        │                                                       │
│        ▼                                                       │
│  render.py        → MuseScore 4 CLI: musescore4 -o out.{pdf,   │
│                     png,svg,mid,mxl} input.musicxml            │
└────────────────────────────────────────────────────────────────┘
```

---

## Tech stack (MVP)

| Vrstva                | Knihovna                         | Lic.       | Proč                                   |
|-----------------------|----------------------------------|------------|----------------------------------------|
| Web UI                | **Gradio 6.x**                   | Apache-2.0 | Mikrofon + upload v jedné komponentě   |
| Audio → MIDI          | **Basic Pitch** (Spotify)        | Apache-2.0 | CPU-friendly, polyfonní, drop-in       |
| Mono fallback         | **CREPE**                        | MIT        | Pro vokál/melodii vyšší přesnost f0    |
| Vokál izolace         | **Demucs htdemucs**              | MIT        | SOTA stem separation co se dá na CPU   |
| MIDI → MusicXML       | **music21**                      | BSD-3      | Standard Python pro symbolickou hudbu  |
| Engraving             | **MuseScore 4 CLI** (`mscore`)   | GPL-3.0    | MusicXML → PDF/PNG/SVG/MIDI headless   |
| Audio I/O             | `librosa`, `soundfile`           | ISC/BSD    | Pomocné                                 |
| Balíčkování           | `uv` + `pyproject.toml`          | —          | Rychlejší než pip, lock soubor          |

> Pozn. k licencím: MuseScore a LilyPond jsou GPL-3.0 — voláme je jen jako externí binárky
> přes `subprocess`, neimportujeme jejich kód, takže naše appka může zůstat permisivní.

---

## Soubory v projektu (MVP)

```
noty/
├── pyproject.toml                # uv-managed deps
├── README.md
├── .env.example                  # např. MUSESCORE_PATH
├── app.py                        # Gradio entrypoint
├── src/transcribe_app/
│   ├── __init__.py
│   ├── preprocess.py             # load + resample audio
│   ├── transcribe.py             # router → Basic Pitch / Demucs+BP
│   ├── notation.py               # music21: MIDI → MusicXML + quantize
│   └── render.py                 # MuseScore CLI wrapper
├── tests/
│   ├── fixtures/                 # pár krátkých vzorků (CC0 hudba)
│   ├── test_transcribe.py
│   └── test_notation.py
├── Dockerfile                    # pro Azure Container Apps
└── .dockerignore
```

Žádné předčasné abstrakce — každý modul ~1 funkce s jasným I/O. `transcribe.py` je
nejvíc kódu (~80 řádků), zbytek je tenký glue.

---

## Klíčové funkce, které se použijí (nepsat znovu)

- `basic_pitch.inference.predict(audio_path)` → vrací `(model_output, midi_data, note_events)`
- `demucs.api.Separator().separate_audio_file(path)` → dict stop `{vocals, drums, bass, other}`
- `music21.converter.parse(midi_path)` → `Stream`
- `Stream.write('musicxml', fp=...)` a `.write('midi', fp=...)`
- `Stream.makeMeasures()` + `Stream.quantize()` pro úklid not před exportem
- Beat/tempo prior: `librosa.beat.beat_track()` — předáme jako `quarterLengthDivisors` do `quantize()`

---

## Deployment do Azure

Tři realistické varianty (od nejlevnější):

1. **Azure Container Apps (CPU)** — Doporučeno pro MVP.
   - Plán: Consumption, scale-to-zero (platíš jen za běh).
   - Image: Dockerfile s Python 3.12 + MuseScore 4 + naše deps.
   - Basic Pitch + Demucs běží na CPU (Demucs na 30s klipu cca 10-30s na 4 vCPU — únosné).
   - Bind volumes nepotřebujeme, vše stateless, výstupy do `/tmp`.
   - Cena: ~0 Kč v klidu, pár Kč/h při běhu. Ideální pro hobby.

2. **Azure App Service for Linux (B1 / B2 plán)** — Pokud chceš "vždy zapnuté".
   - Pevná měsíční cena, jednodušší deploy přes `az webapp up`.
   - Stejné CPU limity, žádný scale-to-zero.

3. **Azure ML Online Endpoint / VM s GPU (NC-series)** — Volitelně pro Phase 3.
   - **Dostupné typy v Azure** (West Europe): `Standard_NC4as_T4_v3` (1×T4, ~$0.50/h),
     `Standard_NC6s_v3` (1×V100, ~$3/h), `Standard_NC24ads_A100_v4` (1×A100, ~$3.7/h).
   - Pro Mel-Band RoFormer stačí T4. YourMT3+ poběží na T4 také, A100 jen pokud zkrátíme čekání.
   - Strategie: **zapínat na vyžádání**. Frontend pošle požadavek do Azure Function, ta probudí
     spot VM s GPU, model jednorázově zpracuje, vrátí výsledek, VM se vypne. Cena na transkripci
     pak vyjde na pár centů.
   - **Pro hobby a CPU-tolerantní použití to není nutné** — uživatel potvrdil, že real-time
     nepotřebuje, takže CPU stačí pro celou roadmap, GPU je čistě "rychlejší zpracování".

**Konkrétní postup pro MVP**:
```bash
az containerapp up \
  --name noty-app --resource-group noty-rg --location westeurope \
  --source . --ingress external --target-port 7860
```
Gradio defaultně poslouchá na 7860.

---

## Phased roadmap (po MVP)

| Fáze | Co přidat | Proč | Náklad |
|------|-----------|------|--------|
| **MVP** | Basic Pitch + Demucs + music21 + MuseScore + Gradio + Azure CA | Funguje, je to zadarmo | ~1 den |
| **2. Kvalita separace** | Swap `htdemucs` → **Mel-Band RoFormer** (`audio-separator` PyPI) | Méně artefaktů ve vokálu → lepší transkripce | ~půl dne, na CPU 3-5× pomalejší než htdemucs ale akceptovatelné |
| **3. Lepší transkripce** | Přidat **YourMT3+** (multi-instrument) jako volitelný backend pro klavír/kapelu | Basic Pitch je z 2022, YourMT3+ je SOTA | 1-2 dny, na CPU pomalé, **Azure NC-series VM** na vyžádání |
| **4. Music priors** | Essentia/librosa → klíč, tempo, takt → zlepší music21 quantize a key signature | Méně manuálního dolaďování | ~půl dne |
| **5. In-browser preview** | **OpenSheetMusicDisplay** (npm) renderuje MusicXML v Gradio HTML komponentě před stažením | UX skok — uživatel vidí výsledek hned | ~půl dne |
| **6. AI cleanup** | Claude API: MIDI→ABC→prompt na cleanup→zpět MusicXML | Quantize/key/beam korekce mimo dosah deterministických algoritmů | ~1 den, $ za tokeny |
| **7. ⭐ AI hudební asistent (priorita pro uživatele)** | Chat sidebar nad notovým zápisem: "transponuj do G dur", "vytáhni jen melodii", "přidej akordové značky", "zjednoduš pro začátečníka", "vysvětli ten ii-V-I" | **Hlavní produktový diferenciátor** — to, co Music.AI ani Klangio nenabízí | 2-3 dny, Claude API |

---

## ⭐ AI hudební asistent — proč na něj nesmíme zapomenout

Uživatel explicitně zmínil, že **AI asistent na transpozici a podobné úpravy je hodně lákavá feature**.
I když začínáme bez něj, **musí zůstat v každé další iterativní revizi plánu jako hvězdný cíl**.

**Co to konkrétně bude umět** (až přijdeme k Phase 7):
- **Transpozice**: "transponuj do G dur" → LLM upraví MusicXML, vrátí diff, uživatel preview + apply
- **Extrakce hlasů**: "zachovej jen melodii", "smaž bicí", "ukaž jen akordy"
- **Harmonická analýza**: "přidej akordové značky nad takty", "označ kadence", "zvýrazni modulace"
- **Zjednodušení**: "verze pro začátečníka", "transponuj do snadné tóniny pro kytaru", "rozšiř akordy do arpeggia"
- **Výuková vysvětlení**: "co je tohle za progresi?", "proč zní toto dissonantně?", "navrhni cvičení"
- **Generování doprovodu**: "přidej basovou linku k téhle melodii", "navrhni druhý hlas"
- **Cross-modal**: vstup textem "zahraj mi Skákal pes" → výstup notového zápisu (až v daleké budoucnosti)

**Technický náčrt**:
- Frontend: chat panel vedle OSMD preview (Phase 5)
- Backend: každá zpráva → tool-use Claude API s nástroji `transpose`, `extract_voice`, `add_chords`,
  `simplify`, `explain`. Tooly operují nad music21 `Stream` a vrací nový MusicXML.
- Cache: každý stav skladby uložit (lokálně do sessionu nebo do SQLite), aby šlo dělat undo/redo.
- Format pro LLM: **ABC notation** (zhuštěný, LLM-friendly) místo MusicXML pro chat kontext.

**Proč to není v MVP**: vyžaduje funkční MusicXML pipeline + frontend preview, aby měl asistent
co editovat. Bez Phase 5 (OSMD preview) je chat editace slepá. Bez Phase 1-4 je vstupní materiál
nekvalitní, takže by asistent jen leštil odpadky.

---

## Co je Music.AI (paid API) a dokážeme to sami?

Music.AI ($0.08/min transcription, $0.10/min stems) je v podstatě **hosted wrapper nad
stejnými OSS modely**, které stavíme:

| Music.AI feature      | Pod kapotou (jejich + naše ekvivalence)                |
|-----------------------|--------------------------------------------------------|
| Stem separation       | Demucs / RoFormer — **postavíme**                     |
| Chord recognition     | madmom / chord-extractor — **postavíme** (Phase 4)    |
| Lyrics transcription  | Whisper + alignment — **postavíme** (Phase 4 volit.)  |
| Beat/tempo            | librosa.beat / madmom — **postavíme** (Phase 4)       |
| Key detection         | Essentia KeyExtractor — **postavíme** (Phase 4)       |
| Transcription → MIDI  | nezveřejněno, pravděpodobně Basic Pitch / vlastní tlustá síť |

**Verdikt**: Ano, dokážeme to sami. Music.AI prodává:
1. Hosting + scaling (vyřeší se Azure Container Apps + případně Azure ML endpoint).
2. Stabilita modelů (drobně lepší než naivně použité OSS).
3. Žádná správa GPU.

**Jediné, co Music.AI nedělá lépe než my, je MusicXML export** — to oni neumějí vůbec.
Takže pro náš use-case (cílem je notový zápis, ne stems pro DJ) nemá Music.AI smysl.

Klangio (klang.io/api) by stál za zvážení **jen v Phase 6+** jako "premium quality" toggle,
protože jejich Piano2Notes/Melody Scanner produkují výrazně čistší MusicXML než cokoliv OSS.
Ale pricing je contact-sales → pro hobby skip.

---

## Verifikace MVP

1. **Smoke test lokálně**:
   ```bash
   uv run python app.py
   # otevři http://localhost:7860
   ```
   - Nahraj `tests/fixtures/scale_piano.wav` (C dur stupnice na klavír).
   - Kategorie: "klavír".
   - Očekávaný výstup: 4 soubory ke stažení, MIDI obsahuje C4-C5, MusicXML otevře MuseScore.

2. **Jednotkové testy** (`pytest tests/`):
   - `test_transcribe.py::test_monophonic_scale_basic_pitch` — Basic Pitch detekuje 8 not z C-dur stupnice (tolerance ±1 půltón na okrajích).
   - `test_notation.py::test_midi_to_musicxml_roundtrip` — `music21` načte MIDI a exportuje MusicXML, který se parsuje zpět bez chyb.
   - `test_render.py::test_musescore_produces_pdf` — mockuj `subprocess`, ověř argumenty.

3. **Mikrofon flow** (manuálně v prohlížeči):
   - Nahraj 5s pískání "Ovčáci čtveráci", kategorie monofonní.
   - Očekávaný výstup: 5-6 not, MusicXML obsahuje rozumný takt 4/4.

4. **Azure deployment smoke test**:
   - Po `az containerapp up` otevři FQDN, opakuj test 1.
   - Cold start by měl být < 30s.

5. **Sanity check kategorií**:
   - Stejný soubor přes všechny 4 kategorie → každá produkuje validní MusicXML, jen různě bohatý.

---

## Otevřené otázky / pre-implementační rozhodnutí

- **Délka vstupu**: Limit 60s na MVP (Demucs na delším audiu na CPU je pomalý). Hard-cap v UI.
- **Concurrency**: Gradio default queue=1 — pro hobby Azure CA stačí.
- **Storage**: nic, vše stateless v `/tmp`. Případný history feature je Phase 5+.
- **Auth**: žádný — Azure CA může běžet public, případně přes Azure Front Door s basic auth pro privátní použití.
