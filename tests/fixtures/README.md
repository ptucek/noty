# Test fixtures

Tento adresář obsahuje kurátorovaný testovací dataset pro ověření správnosti
audio → notace pipeline. Pro každou skladbu máme **ground-truth MusicXML**
(referenční notaci) a **WAV audio** (vstup pro pipeline). Pipeline by měla
z audia vygenerovat notaci podobnou ground truth.

## Co je tady

```
tests/fixtures/
├── README.md                                 ← tento soubor
├── generate_fixtures.py                      ← generátor (spusť pro znovuvytvoření)
├── monofonni/
│   ├── ovcaci_ctveraci.{wav,musicxml,notes.md}
│   └── pec_nam_spadla.{wav,musicxml,notes.md}
├── klavir/
│   └── bach_minuet_g_excerpt.{wav,musicxml,notes.md}
└── vokal/
    └── skakal_pes.{wav,musicxml,notes.md}
```

| Kategorie | Skladba                              | Délka  | Not | Tonalita | Licence       |
|-----------|--------------------------------------|--------|-----|----------|---------------|
| monofonni | Ovčáci čtveráci                      | ~8 s   | 13  | C dur    | Public Domain |
| monofonni | Pec nám spadla                       | ~8 s   | 12  | C dur    | Public Domain |
| klavir    | Bach – Menuet G dur (BWV Anh. 114)   | ~13 s  | 28  | G dur    | Public Domain |
| vokal     | Skákal pes                           | ~4 s   | 13  | C dur    | Public Domain |

**Kategorie `kapela` (full-band) je úmyslně vynechána** – pro full-band není
snadno dostupný malý kurátorovaný PD vzorek se stems + perfektní ground-truth
notací, a syntéza by neměla reprezentativní akustické vlastnosti. Pokud
později najdeme vhodný zdroj (MAESTRO/MusicNet sample, CC-BY licencovaný
záznam s notami), přidáme.

## Licence a zdroje

Všechny použité skladby jsou v **public domain**:

- **Ovčáci čtveráci, Pec nám spadla, Skákal pes** – tradiční české lidové
  písně, autor neznámý, melodie staletí stará → PD.
- **Menuet G dur, BWV Anh. 114** – z *Notenbüchlein für Anna Magdalena Bach*
  (1725); autorství dnes přisuzováno Christianu Petzoldovi (†1733). Skladba
  v PD bez ohledu na atribuci (PD po smrti autora + 70 let).

Audio (WAV) jsme **syntetizovali programaticky** v `generate_fixtures.py`
ze symbolické reprezentace v music21:

- *monofonni* a *klavir* fixtures: součet sinusoid (f0 + 2 harmonické), krátká
  ADSR obálka. Čistý signál vhodný pro Basic Pitch.
- *vokal* fixture: vícefrekvenční zdroj s amplitudovou modulací na formantech
  F1 ≈ 700 Hz, F2 ≈ 1100 Hz, F3 ≈ 2600 Hz (samohláska 'a') + 5 Hz vibrato.
  Demucs by ho měl klasifikovat jako vokál.

Tato strategie zajišťuje:

1. **100% věrnou ground-truth notaci** (nikdo neopisoval z poslechu).
2. **Žádné licenční problémy** – kód generátoru i syntetizované audio jsou v PD.
3. **Malé soubory** (~150–600 kB každý) – vhodné pro commit do gitu.
4. **Reprodukovatelnost** – generátor je deterministický.

## Jak ručně ověřit fixture (poslechem)

1. Otevři `.musicxml` v MuseScore 4 (nebo jiném editoru notace).
2. Spusť přehrávání – uslyšíš referenční verzi.
3. Otevři `.wav` v libovolném audio přehrávači.
4. Oba zvuky by měly hrát stejnou melodii (tempo i barva se mohou lišit;
   syntetické WAV používá čisté sinusoidy / formanty).

## Jak spustit correctness testy

```bash
# Plné testy (volá Basic Pitch a music21 – cca 30–60 s celkem):
uv run --extra dev pytest tests/test_correctness.py -v -s

# Lidsky čitelný side-by-side report:
uv run --extra dev python tests/test_correctness.py

# Přeskočit drahé pipeline testy (jen kontrola existence fixtures):
RUN_HEAVY_TESTS=0 uv run --extra dev pytest tests/test_correctness.py -v
```

Test `test_pipeline_correctness`:

- pro každý fixture spustí `transcribe()` + `events_to_musicxml()`
- porovná pitch-class posloupnost přes **nejdelší společnou podsekvenci (LCS)**
- ratio = `len(LCS) / len(ground_truth)` → akceptační práh **70 %**
- vytiskne side-by-side ground truth vs. predikce (prvních 32 not)

LCS je odolná vůči:

- *přidaným* notám v predikci (Basic Pitch často duplikuje noty na začátku/konci)
- *vynechaným* notám (krátké tóny se občas ztratí)
- *enharmonice* (porovnáváme pitch class 0–11, ne nameWithOctave)

## Známé výsledky (na referenčním macOS prostředí, květen 2026)

| Fixture                          | GT not | PRED not | LCS shoda | Tonalita |
|----------------------------------|-------:|---------:|----------:|----------|
| ovcaci_ctveraci (monofonni)      | 13     | 21       | 100 %     | OK       |
| pec_nam_spadla (monofonni)       | 12     | 22       | 100 %     | OK       |
| bach_minuet_g_excerpt (klavir)   | 28     | 45       | 75 %      | OK       |
| skakal_pes (vokal)               | 13     | –        | SKIP*     | –        |

\* SKIP: aktuální `demucs 4.0.1` v projektovém venv nemá modul `demucs.api`;
test se v takovém případě přeskočí. Po opravě (např. upgrade demucs nebo
přidání chybějícího modulu) test poběží automaticky.

Vyšší počet predikovaných not než ground-truth je normální – Basic Pitch
často vrací několik kandidátů na jeden onset; LCS shoda zachycuje, že
**správné noty jsou ve správném pořadí**. Pokud chceš počet not poladit,
prahuj v `transcribe_app/transcribe.py` nebo přidej post-processing.

## Jak přidat nový fixture

1. Otevři `generate_fixtures.py`.
2. Přidej novou funkci `make_<name>() -> stream.Score` vracející music21
   stream. Klidně vytvoř víceparťový score (klavír levá/pravá ruka).
3. Přidej položku do seznamu `FIXTURES` s `category`, `name`, `title`,
   `source`, `license`, `make`, `timbre` ("sine" nebo "vowel") a
   `notes_human` (krátký lidsky čitelný popis not).
4. Spusť `uv run python tests/fixtures/generate_fixtures.py`.
5. Přidej fixture do `tests/test_correctness.py::FIXTURES` tuple.
6. Spusť testy a ověř, že shoda ≥ 70 %.

**Doporučení pro výběr melodie:**

- Dej přednost public-domain skladbám (lidové, baroko před 1923, …).
- Krátké fráze 8–30 not – stačí na test, drží audio < 1 MB.
- Pro monofonní fixtures: jednoduché diatonické melodie v běžné poloze
  (C4–C6) – Basic Pitch je tam nejpřesnější.
- Pro klavír: vyhni se rychlým pasážím a triolám, pipeline kvantuje na
  16th + triolu. Jednoduchý 3/4 nebo 4/4 takt v tempu 80–120 BPM.

## Zdroje, ze kterých lze čerpat externí (real-world) reference

Pokud chceš v budoucnu doplnit *reálné* (nesyntetické) audio:

- **MAESTRO v3** (Magenta, CC BY-NC-SA 4.0) – klasický klavír + aligned MIDI:
  <https://magenta.tensorflow.org/datasets/maestro>
- **MusicNet** (CC BY 4.0) – komorní hudba + aligned annotations:
  <https://homes.cs.washington.edu/~thickstn/musicnet.html>
- **Mutopia Project** (CC / PD sheet music + MIDI):
  <https://www.mutopiaproject.org/>
- **IMSLP** (public-domain partitury) + vlastní render přes MuseScore CLI:
  <https://imslp.org/>
- **Open Goldberg Variations** (CC0 audio + score):
  <https://www.opengoldbergvariations.org/>
- **basic_pitch test fixtures** (Apache-2.0):
  <https://github.com/spotify/basic-pitch/tree/main/tests/data>

Pro `kapela` (full-band): zvaž `MUSDB18` (research-only) nebo CC-licencované
multitrack stems z `cambridge-mt.com/ms/mtk/`.
