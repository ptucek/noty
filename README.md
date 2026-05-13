# 🎵 Noty — Audio → Notový zápis

Webová aplikace, která z **audio nahrávky** (mp3, wav, mikrofon) vytvoří **notový zápis**
ve formátech **MusicXML, MIDI, PDF, PNG, SVG**.

**Cílový uživatel**: učitelka ZUŠ — nahraje žákovo hraní (nebo vlastní), dostane noty
k vytištění / další úpravě.

---

## Co to umí

- 🎙️ Vstup: upload `.wav`/`.mp3` nebo nahrávka přímo z **mikrofonu** v prohlížeči
- 🎼 Výstup: **MusicXML** (otevře MuseScore/Sibelius/Finale), **MIDI**, **PDF**, **PNG**, **SVG**
- 📊 Kategorie hudby (uživatel volí pro vyšší přesnost):
  - **Monofonní** (jedna melodie / zpěv)
  - **Klavír** / jeden polyfonní nástroj
  - **Plná kapela / píseň**
  - **Jen vokál z písně** (izoluje zpěv přes Mel-Band RoFormer / Demucs)
- 🎯 **Pokročilý override**: pokud znáš tempo / tóninu / takt, zadej je ručně
  (auto-detekce není dokonalá)
- 🤖 **Volitelný LLM cleanup** přes Claude API (Anthropic nebo Microsoft Foundry)
- 📚 **Galerie ukázek** — 4 ručně psané + 43 Mutopia (MuseScore render) + 8 reálných
  (Bach Goldberg Variations, Kimiko Ishizaka, CC0) s referenčním notovým zápisem

---

## Rychlý start (lokálně)

```bash
# 1. Klon
git clone https://github.com/ptucek/noty.git
cd noty

# 2. Závislosti (Python 3.11, MuseScore 4)
brew install --cask musescore         # macOS
brew install uv ffmpeg
uv sync                                # cca 2-3 min, stahuje 2GB ML deps

# 3. Spuštění
uv run python app.py
# → otevři http://localhost:7860
```

Detailní setup viz [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

---

## Použití (UI)

### Záložka **Transkripce**
1. Nahraj audio (drag&drop nebo nahraj z mikrofonu)
2. Vyber **Kategorii hudby**
3. *(Volitelně)* Rozbal **🎯 Pokročilé: ruční override** a zadej známé hodnoty:
   - Tempo (BPM, 40-240)
   - Tónina (15 možností od Ces dur po Cis dur)
   - Takt (2/4, 3/4, 4/4, 6/8)
4. Stiskni **Transkribovat**
5. Po cca 5-30 s (podle délky audia + použitých modelů) dostaneš:
   - **Náhled** v prohlížeči (OSMD)
   - 5 souborů ke stažení: MusicXML, MIDI, PDF, PNG, SVG

### Záložka **Ukázky**
- Galerie referenčních skladeb s ground-truth notovým zápisem
- Klikni na ukázku → přehraje audio, zobrazí očekávané noty
- Pomáhá porovnat náš výstup s tím, co tam *skutečně* je

---

## Architektura (zjednodušeně)

```
Audio → preprocess → [Demucs/RoFormer]? → Basic Pitch → NoteEvents → music21 → MusicXML → MuseScore CLI → PDF/PNG/SVG
                       │ jen pro vokál     │ pitch     │ heuristiky                                          │
                       │ izolace zpěvu     │ detection │ + LLM cleanup (volit.)                            │
                       │                                                                                    └── + Gradio UI
                       └── ↓ paralelně: librosa beat/key/timesig detection
```

Detailní pipeline + module map: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Konfigurace

| Env var | Default | Co dělá |
|---|---|---|
| `MUSESCORE_PATH` | auto | Cesta k `mscore` binárce |
| `SEPARATION_BACKEND` | `roformer` | `roformer` (lepší) nebo `demucs` (rychlejší fallback) |
| `TRANSCRIPTION_BACKEND` | `basic_pitch` | Aktuálně jen Basic Pitch (Aria-AMT scaffolding) |
| `LLM_CLEANUP` | `0` | `1` zapne post-process přes Claude API |
| `CLEANUP_MODEL` | `claude-opus-4-6` | Model name (Opus 4.7, 4.6, Sonnet 4.6, Haiku 4.5) |
| `ANTHROPIC_FOUNDRY_RESOURCE` | — | Pro MS Foundry: jméno resource (např. `ptuc-foundry-test`) |
| `ANTHROPIC_FOUNDRY_API_KEY` | — | Pro MS Foundry: API klíč |
| `ANTHROPIC_API_KEY` | — | Pro first-party Anthropic API |

Plný popis: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

---

## Modely a knihovny

| Co | Knihovna | Licence | Velikost váh |
|---|---|---|---|
| Audio → MIDI (transkripce) | [Basic Pitch](https://github.com/spotify/basic-pitch) (Spotify) | Apache-2.0 | ~30 MB |
| Vokál izolace (default) | Mel-Band RoFormer via [audio-separator](https://github.com/karaokenerds/python-audio-separator) | MIT | ~600 MB |
| Vokál izolace (fallback) | [Demucs](https://github.com/facebookresearch/demucs) htdemucs | MIT | ~80 MB |
| Beat / tempo / key / time sig | [librosa](https://librosa.org) | ISC | — |
| Beat / downbeat / key (alt) | [madmom](https://github.com/CPJKU/madmom) | BSD-3 | ~50 MB |
| MIDI → MusicXML | [music21](https://github.com/cuthbertLab/music21) | BSD-3 | — |
| MusicXML → PDF/PNG/SVG | [MuseScore 4 CLI](https://musescore.org) | GPL-3.0 (externí proces) | — |
| LLM cleanup | [Anthropic SDK](https://github.com/anthropics/anthropic-sdk-python) | MIT | — (API) |
| Web UI | [Gradio](https://gradio.app) | Apache-2.0 | — |
| In-browser notace | [OpenSheetMusicDisplay](https://github.com/opensheetmusicdisplay/opensheetmusicdisplay) (CDN) | BSD-3 | — |

---

## Test data + přesnost (čestně)

**4 ručně psané** + **43 Mutopia** (rendered audio z public-domain MusicXML) +
**8 reálných** (Open Goldberg, Kimiko Ishizaka, CC0).

| Metrika | Baseline | S LLM cleanup |
|---|---|---|
| Pitch class shoda (LCS, threshold 70%) | **~50%** fixtur projde | podobné |
| Time signature accuracy | **51.2%** (43 Mutopia) | **34.9%** (LLM zhoršuje, vypnuto) |
| Tempo (rough) | typicky ±10% od GT, octave error častý | LLM občas pomůže |
| Key signature | KS proxy, často chybí V↔I distinkce | podobné |

**Octave error v tempu** je inherentní hudební ambiguity (Bach Aria 72 BPM nebo 144 BPM
— bez kontextu nelze rozhodnout). Proto je v UI **manuální override**.

Honestly: pitch detection funguje slušně, ale metadata (tempo/klíč/takt) jsou často
špatně — proto je override doporučený když uživatel zná hodnoty.

Detailní test data viz [tests/fixtures/README.md](tests/fixtures/README.md).

---

## Deployment do Azure

Aktuální produkční deploy: **Azure Container Apps** v `swedencentral`.

Stručně:
```bash
az containerapp up \
  --name noty-app \
  --resource-group ptuc-foundry \
  --source . \
  --ingress external \
  --target-port 7860
```

Plný postup + custom doména + managed identity pro Foundry: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## Roadmap

✅ **Hotovo (Phase 1-5 + 4 vylepšení)**:
- Gradio UI, mikrofon + upload, multi-staff render
- Basic Pitch + Demucs/RoFormer + music21 + MuseScore CLI
- librosa key/tempo/time-sig detekce (octave-aware)
- LLM cleanup (Foundry + first-party Anthropic, oba modely)
- Ruční override tempo/klíč/takt
- OSMD in-browser preview
- 55 testovacích fixtur (47 synth + 8 real CC0)
- Azure Container Apps deploy

🚧 **Plánované**:
- **Phase 7**: AI hudební asistent — chat sidebar pro transpozici, extrakci melodie,
  přidání akordových značek, zjednodušení (přes Claude tool-use)
- Lepší rytmická kvantizace (méně tečkovaných osmin v výstupu)
- Multi-staff voice splitting pro Bach kontrapunkt
- Custom doména `noty.davidtucek.cz`

❌ **Zkoušeno a zamítnuto**:
- Aria-AMT / YourMT3+ jako klavírní transkripční backend (CUDA-only, neintegrovatelné na CPU)
- LLM override time signature (-16pp accuracy vs baseline)

---

## Licence + credits

Kód: MIT (TBD).

Test data:
- Synthetic fixtures: vyrobeno z public-domain melodií (české lidovky, Bach)
- Mutopia fixtures: jednotlivé licence per piece, většinou CC-BY-SA / Public Domain
- Real fixtures: Open Goldberg Variations (audio: Kimiko Ishizaka, score: Werner Schweer, **CC0**)

Postaveno s pomocí [Claude Code](https://claude.com/claude-code) (Opus 4.7).

---

## Kontakt

Repo: <https://github.com/ptucek/noty>

Vyrobeno pro mou sestru — učitelku ZUŠ. 🎼
