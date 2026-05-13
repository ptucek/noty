"""Gradio webová aplikace — audio → notový zápis."""

from __future__ import annotations

import atexit
import base64
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import gradio as gr

from transcribe_app.notation import events_to_musicxml
from transcribe_app.render import render_musicxml
from transcribe_app.transcribe import CATEGORIES, transcribe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logger = logging.getLogger("app")

CATEGORY_LABELS = {
    "monofonni": "Monofonní (jedna melodie / zpěv)",
    "klavir": "Klavír / jeden polyfonní nástroj",
    "kapela": "Plná kapela / píseň",
    "vokal": "Jen vokál z písně (izolovat zpěv)",
}
LABEL_TO_CATEGORY = {v: k for k, v in CATEGORY_LABELS.items()}

# Klíče: název (česky + anglicky) → sharps (-7..+7).
KEY_OPTIONS: dict[str, int | None] = {
    "Auto-detekce": None,
    "C dur / a moll (♮)": 0,
    "G dur / e moll (1♯)": 1,
    "D dur / h moll (2♯)": 2,
    "A dur / fis moll (3♯)": 3,
    "E dur / cis moll (4♯)": 4,
    "H dur / gis moll (5♯)": 5,
    "Fis dur / dis moll (6♯)": 6,
    "Cis dur / ais moll (7♯)": 7,
    "F dur / d moll (1♭)": -1,
    "B dur / g moll (2♭)": -2,
    "Es dur / c moll (3♭)": -3,
    "As dur / f moll (4♭)": -4,
    "Des dur / b moll (5♭)": -5,
    "Ges dur / es moll (6♭)": -6,
    "Ces dur / as moll (7♭)": -7,
}
TIME_SIG_OPTIONS = ["Auto-detekce", "2/4", "3/4", "4/4", "6/8"]

PROJECT_ROOT = Path(__file__).parent
FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"
MUTOPIA_DIR = FIXTURES_DIR / "mutopia"
MUTOPIA_INDEX = MUTOPIA_DIR / "INDEX.json"


@dataclass(frozen=True)
class FixtureExample:
    title: str
    category: str
    source: str  # "synth" / "mutopia"
    wav_path: Path
    gt_png: Path | None
    notes_md_text: str  # už předzpracovaná


SYNTH_TITLES = {
    "ovcaci_ctveraci": "Ovčáci čtveráci",
    "pec_nam_spadla": "Pec nám spadla",
    "bach_minuet_g_excerpt": "Bach – Menuet G dur",
    "skakal_pes": "Skákal pes",
}


def _first_png(directory: Path, slug: str) -> Path | None:
    for pattern in (f"{slug}.png", f"{slug}-1.png"):
        candidate = directory / pattern
        if candidate.exists():
            return candidate
    return None


def _load_synthetic_fixtures() -> list[FixtureExample]:
    examples: list[FixtureExample] = []
    for category in ("monofonni", "klavir", "kapela", "vokal"):
        cat_dir = FIXTURES_DIR / category
        if not cat_dir.is_dir():
            continue
        for wav in sorted(cat_dir.glob("*.wav")):
            slug = wav.stem
            png = _first_png(cat_dir, slug)
            notes_md = cat_dir / f"{slug}.notes.md"
            md_text = notes_md.read_text(encoding="utf-8") if notes_md.exists() else ""
            examples.append(
                FixtureExample(
                    title=SYNTH_TITLES.get(slug, slug.replace("_", " ").title()),
                    category=category,
                    source="synth",
                    wav_path=wav,
                    gt_png=png,
                    notes_md_text=md_text,
                )
            )
    return examples


def _load_mutopia_fixtures() -> list[FixtureExample]:
    if not MUTOPIA_INDEX.exists():
        return []
    try:
        data = json.loads(MUTOPIA_INDEX.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    examples: list[FixtureExample] = []
    for p in data.get("pieces", []):
        wav = MUTOPIA_DIR / p["wav"]
        if not wav.exists():
            continue
        piece_dir = wav.parent
        png = _first_png(piece_dir, p["slug"])
        md = (
            f"**Skladatel:** {p.get('composer', '?')}\n\n"
            f"**Skladba:** {p.get('title', '?')}\n\n"
            f"**Tónina:** {p.get('key', '?')}  ·  **Takt:** {p.get('time_signature', '?')}  ·  **Tempo:** {p.get('bpm', '?')}\n\n"
            f"**Délka:** {p.get('duration_s', '?')} s  ·  **Not:** {p.get('expected_notes_count', '?')}\n\n"
            f"**Licence:** {p.get('license', '?')}  ·  **Zdroj:** {p.get('source_url', '?')}"
        )
        examples.append(
            FixtureExample(
                title=p.get("title", p["slug"]),
                category=p["category"],
                source="mutopia",
                wav_path=wav,
                gt_png=png,
                notes_md_text=md,
            )
        )
    return examples


def discover_fixtures() -> list[FixtureExample]:
    return _load_synthetic_fixtures() + _load_mutopia_fixtures()


OSMD_CDN_URL = "https://unpkg.com/opensheetmusicdisplay@1.9.0/build/opensheetmusicdisplay.min.js"

OSMD_EMPTY_HTML = (
    '<div style="padding: 1em; color: #888; font-style: italic;">'
    "Náhled OSMD se zobrazí po transkripci."
    "</div>"
)


def _build_osmd_html(musicxml_path: Path) -> str:
    """Vrátí HTML, ve kterém OSMD v prohlížeči vykreslí MusicXML soubor.

    MusicXML se base64-enkóduje a v JS dekóduje přes ``atob`` — vyhneme se tak
    escape problémům s backticky / ``${`` v template literálu.
    """
    try:
        xml_text = musicxml_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("OSMD: nepodařilo se přečíst MusicXML (%s): %s", musicxml_path, exc)
        return (
            '<div style="padding: 1em; color: #c00;">'
            "OSMD náhled není dostupný — MusicXML soubor nelze přečíst."
            "</div>"
        )

    xml_b64 = base64.b64encode(xml_text.encode("utf-8")).decode("ascii")
    # Unikátní ID pro případ, že by se HTML znovu vložilo do stejné stránky.
    container_id = f"osmd-container-{abs(hash(xml_b64)) % (10**8)}"

    return f"""
<div id="{container_id}" style="width: 100%; height: 600px; overflow: auto; border: 1px solid #ddd; background: #fff;"></div>
<script src="{OSMD_CDN_URL}"></script>
<script>
(function() {{
  function renderOSMD() {{
    if (typeof opensheetmusicdisplay === "undefined") {{
      setTimeout(renderOSMD, 100);
      return;
    }}
    var container = document.getElementById("{container_id}");
    if (!container) {{ return; }}
    try {{
      // Dekóduj base64 → UTF-8 string (atob vrátí latin1, převedeme).
      var b64 = "{xml_b64}";
      var binary = atob(b64);
      var bytes = new Uint8Array(binary.length);
      for (var i = 0; i < binary.length; i++) {{ bytes[i] = binary.charCodeAt(i); }}
      var xml = new TextDecoder("utf-8").decode(bytes);
      var osmd = new opensheetmusicdisplay.OpenSheetMusicDisplay(container, {{
        drawTitle: false,
        drawComposer: false,
        drawSubtitle: false,
        drawPartNames: false,
        backend: "svg",
        autoResize: true,
      }});
      osmd.load(xml).then(function() {{ osmd.render(); }}).catch(function(err) {{
        container.innerHTML = '<div style="padding:1em;color:#c00;">OSMD render selhal: ' + err + '</div>';
      }});
    }} catch (e) {{
      container.innerHTML = '<div style="padding:1em;color:#c00;">OSMD chyba: ' + e + '</div>';
    }}
  }}
  renderOSMD();
}})();
</script>
""".strip()


def run_pipeline(
    audio_input: str | None,
    category_label: str,
    tempo_override: float | None = None,
    key_label: str = "Auto-detekce",
    time_sig_label: str = "Auto-detekce",
) -> tuple:
    """Hlavní pipeline: audio → MIDI → MusicXML → PDF/PNG/SVG."""
    if not audio_input:
        raise gr.Error("Nejdřív nahraj nebo zaznamenej audio.")
    if category_label not in LABEL_TO_CATEGORY:
        raise gr.Error(f"Neznámá kategorie: {category_label}")

    category = LABEL_TO_CATEGORY[category_label]
    audio_path = Path(audio_input)

    # DoS prevence: omezit délku vstupu.
    try:
        import soundfile as sf
        info = sf.info(str(audio_path))
        duration = info.frames / info.samplerate
        if duration > 120:
            raise gr.Error(f"Audio příliš dlouhé ({duration:.0f}s). Maximum 120s.")
    except gr.Error:
        raise
    except Exception as exc:
        logger.warning("Audio info read failed: %s — pokračuji", exc)

    logger.info("=== Start pipeline: %s (kategorie: %s) ===", audio_path.name, category)

    workdir = Path(tempfile.mkdtemp(prefix="noty_"))
    # Workdir cleanup po skončení procesu (Gradio drží file paths v request queue,
    # takže nemůžeme mazat hned). Pro long-running container Apps to znamená,
    # že disk se naplní jen pokud běží bez restartu dlouho.
    atexit.register(lambda d=workdir: shutil.rmtree(d, ignore_errors=True))
    logger.info("Workdir: %s", workdir)

    key_override = KEY_OPTIONS.get(key_label) if key_label != "Auto-detekce" else None
    timesig_override = time_sig_label if time_sig_label != "Auto-detekce" else None
    tempo_val = float(tempo_override) if tempo_override and tempo_override > 0 else None

    result = transcribe(
        audio_path, category, workdir,
        tempo_override=tempo_val,
        key_sharps_override=key_override,
        time_signature_override=timesig_override,
    )
    if not result.events:
        raise gr.Error("V audiu nebyly nalezeny žádné noty. Zkus jiný soubor nebo kategorii.")
    logger.info(
        "Detekováno %d not, tempo %.1f BPM, tónina %s",
        len(result.events), result.tempo_bpm, result.key_name,
    )
    musicxml_path = events_to_musicxml(
        result.events, workdir,
        bpm=result.tempo_bpm,
        key_sharps=result.key_sharps,
        time_signature=result.time_signature,
    )
    rendered = render_musicxml(musicxml_path, workdir)

    preview = rendered.get("png")
    osmd_html = _build_osmd_html(musicxml_path)
    return (
        str(musicxml_path),
        str(rendered.get("mid")) if "mid" in rendered else None,
        str(rendered.get("pdf")) if "pdf" in rendered else None,
        str(rendered.get("png")) if "png" in rendered else None,
        str(rendered.get("svg")) if "svg" in rendered else None,
        str(preview) if preview else None,
        osmd_html,
    )


def build_ui() -> gr.Blocks:
    fixtures = discover_fixtures()
    logger.info("Načteno %d ukázek", len(fixtures))

    with gr.Blocks(title="Audio → Notový zápis") as demo:
        gr.Markdown(
            "# 🎵 Audio → Notový zápis\n"
            "Nahraj audio soubor nebo zaznamenej z mikrofonu, "
            "vyber kategorii a stáhni si notový zápis."
        )
        with gr.Tabs():
            with gr.Tab("Transkripce"):
                _build_transcribe_tab(fixtures)
            with gr.Tab(f"Ukázky ({len(fixtures)} skladeb)"):
                _build_examples_tab(fixtures)
    return demo


def _build_transcribe_tab(fixtures: list[FixtureExample]) -> None:
    with gr.Row():
        with gr.Column(scale=1):
            audio = gr.Audio(
                sources=["upload", "microphone"],
                type="filepath",
                label="Audio vstup (max 2 min)",
            )
            category = gr.Radio(
                choices=list(CATEGORY_LABELS.values()),
                value=CATEGORY_LABELS["klavir"],
                label="Kategorie hudby",
            )
            with gr.Accordion(
                "🎯 Pokročilé: ruční override (pokud znáš správné hodnoty)",
                open=False,
            ):
                gr.Markdown(
                    "Auto-detekce je nedokonalá — pokud víš tempo / tóninu / takt skladby, "
                    "zadej je sem a pipeline auto-detekci přeskočí."
                )
                tempo_input = gr.Number(
                    label="Tempo (BPM)",
                    value=None,
                    minimum=40,
                    maximum=240,
                    step=1,
                    info="Nech prázdné pro auto-detekci. Klasická hudba typicky 60-140.",
                )
                key_input = gr.Dropdown(
                    label="Tónina",
                    choices=list(KEY_OPTIONS.keys()),
                    value="Auto-detekce",
                    info="Předznamenání. Auto-detekce přes Krumhansl-Schmuckler.",
                )
                timesig_input = gr.Dropdown(
                    label="Takt",
                    choices=TIME_SIG_OPTIONS,
                    value="Auto-detekce",
                    info="Auto-detekce přes librosa onset autocorrelation.",
                )
            run_btn = gr.Button("Transkribovat", variant="primary")
        with gr.Column(scale=1):
            preview = gr.Image(label="Náhled (PNG první strany výstupu)", type="filepath")
            osmd_preview = gr.HTML(
                label="Náhled v prohlížeči (OSMD)",
                value=OSMD_EMPTY_HTML,
            )
            with gr.Group():
                gr.Markdown("### Soubory ke stažení")
                f_xml = gr.File(label="MusicXML")
                f_mid = gr.File(label="MIDI")
                f_pdf = gr.File(label="PDF")
                f_png = gr.File(label="PNG")
                f_svg = gr.File(label="SVG")

    run_btn.click(
        fn=run_pipeline,
        inputs=[audio, category, tempo_input, key_input, timesig_input],
        outputs=[f_xml, f_mid, f_pdf, f_png, f_svg, preview, osmd_preview],
    )

    if fixtures:
        gr.Markdown("### Ukázky pro rychlý test (klikni → naplní formulář)")
        with gr.Tabs():
            for cat_key, cat_label in CATEGORY_LABELS.items():
                cat_fixtures = [fx for fx in fixtures if fx.category == cat_key]
                if not cat_fixtures:
                    continue
                with gr.Tab(f"{cat_label.split('(')[0].strip()} ({len(cat_fixtures)})"):
                    gr.Examples(
                        examples=[[str(fx.wav_path), CATEGORY_LABELS[fx.category]] for fx in cat_fixtures],
                        inputs=[audio, category],
                        examples_per_page=15,
                        label=None,
                    )

    gr.Markdown(
        "ℹ️ Max. délka vstupu 60 s. Pro **vokál** se izoluje zpěv přes Demucs (pomalé na CPU). "
        "Ostatní kategorie jdou rovnou přes Basic Pitch."
    )


def _build_examples_tab(fixtures: list[FixtureExample]) -> None:
    gr.Markdown(
        "Pro každou ukázku vidíš **referenční notový zápis** (jak by to mělo vypadat) "
        "a slyšíš odpovídající audio. Tyhle skladby používáme jako testovací sadu — víme, "
        "jak by výsledek měl vypadat, takže můžeš porovnat."
    )
    if not fixtures:
        gr.Markdown("⚠️ Žádné fixtury v `tests/fixtures/` nenalezeny.")
        return

    with gr.Tabs():
        for cat_key, cat_label in CATEGORY_LABELS.items():
            cat_fixtures = [fx for fx in fixtures if fx.category == cat_key]
            if not cat_fixtures:
                continue
            with gr.Tab(f"{cat_label.split('(')[0].strip()} ({len(cat_fixtures)})"):
                for fx in cat_fixtures:
                    badge = "🧪 syntetické" if fx.source == "synth" else "📚 Mutopia"
                    with gr.Accordion(f"🎼 {fx.title}  ·  {badge}", open=False):
                        with gr.Row():
                            with gr.Column(scale=1):
                                gr.Audio(value=str(fx.wav_path), label="Audio", interactive=False)
                                if fx.notes_md_text:
                                    gr.Markdown(fx.notes_md_text)
                            with gr.Column(scale=1):
                                if fx.gt_png:
                                    gr.Image(value=str(fx.gt_png), label="Referenční noty")
                                else:
                                    gr.Markdown("*(PNG nedostupné — MusicXML je v `tests/fixtures/`)*")


def main() -> None:
    demo = build_ui()
    # Single concurrency — 2 GiB container + paralelní velké tensory (Basic Pitch, RoFormer)
    # = OOM. Queue serializuje requesty.
    demo.queue(default_concurrency_limit=1, max_size=10)
    demo.launch(server_name="0.0.0.0", server_port=7860)


if __name__ == "__main__":
    main()
