# Aria-AMT & YourMT3+ integrace – průzkum a blokery

Tento dokument shrnuje pokus o integraci SOTA neural music transcription jako
volitelného backendu vedle Spotify Basic Pitch. **Výsledek: integrace
neproběhla**; v projektu zůstává Basic Pitch jako default i v praxi jediný
funkční backend. Architektura ale připravená: `TRANSCRIPTION_BACKEND=aria_amt`
přepne dispatch, který v případě chyby zaloguje warning a spadne zpět na Basic
Pitch.

## 1. Aria-AMT (loubbrad / EleutherAI)

Repozitář: <https://github.com/EleutherAI/aria-amt>. Seq2seq transformer
trénovaný na MAESTRO + Aria-MIDI (~60k hodin pian). Piano-only, F1 ≈ 0.96 na
MAESTRO.

### Co jsem zkusil

1. Hledat PyPI wheel: **neexistuje**. Instalace je `git clone && pip install -e .`.
2. Hledat platform-agnostic Python API: README odkazuje na "HuggingFace page"
   pro CPU inference, ale `loubb/aria-medium-transcription` model card vrací 401
   (a profil `loubb` v současnosti vystavuje jen `aria-medium-base`,
   `aria-medium-embedding` – generativní modely, ne transkripční).
3. `uv pip install --dry-run "aria-amt @ git+https://github.com/EleutherAI/aria-amt.git"`:

   ```text
   - torch==2.12.0
   + torch==2.5.0
   - torchaudio==2.11.0
   + torchaudio==2.5.0
   + ariautils @ git+https://github.com/EleutherAI/aria-utils.git
   + safetensors==0.7.0
   ```

   → **Forced downgrade torch 2.12 → 2.5** (jejich pin `torchaudio<=2.5`).
   Tohle by rozbilo basic-pitch i demucs, které ladíme proti aktuálnímu torch.
   Task brief explicitně zakazuje měnit verzi torche → hard blocker.

### Strukturní blokery (i kdybychom verzi torche vyřešili)

- **CUDA-only inference.** `amt/inference/transcribe.py` (1200 LOC) má hardcoded
  `.cuda()` na audio segmentech, KV-cache, masku, tensor positions; multi-process
  GPU manager s `torch.cuda.set_device`; `optional_bf16_autocast` používá
  `torch.cuda.is_bf16_supported`; statický mask se vytváří přes
  `torch.arange(3419, device="cuda")`. Žádná CPU větev. Adaptace by znamenala
  přepsat `process_segments`, `prefill`, `decode_token`, `gpu_manager`
  (cca 400 LOC) na device-agnostic + sehnat / vyrobit fp32 verzi vah
  (modely jsou bf16, MPS bf16 podporuje, čistě CPU PyTorch bf16 ale ne).

- **`ariautils` dependence.** Tokenizer (`amt/tokenizer.py`) volá
  `ariautils.midi.MidiDict` a `ariautils.tokenizer.Tokenizer` – další
  GitHub-only balík, takže nutno reinstalovat z gitu i ten.

- **Velikost vah.** `piano-medium-double-1.0.safetensors` ~1.5 GB, hostováno
  v `loubb/aria-midi` HF datasetu. Stažení v rámci limitu (do 2 GB), ale na CPU
  je inferenční rychlost odhadem 30-100× real-time (autoregressive decoder
  s ~3400 token vocab) – nad task limit "5-10× real-time".

### Verdikt

Integrace by si vyžádala (a) fork s odstraněným pinováním torche, (b) ~400 LOC
CPU port inference loopu, (c) konverze vah na fp32 / verifikace bf16 na CPU,
(d) ariautils tokenizér zaintegrovat. Mimo rozsah jednorázové integrační úlohy.

## 2. YourMT3+ (mimbres)

Repozitář: <https://github.com/mimbres/YourMT3>. Multi-instrument SOTA AMT,
různé ensembles, MLSP 2024.

### Co jsem zkusil

1. Hledat kód v GitHub repu: **README + LICENSE only**. Skutečný kód je v
   HuggingFace Space `mimbres/YourMT3` jako pre-release.
2. Stáhnout `app.py` ze Space (12 kB): kód importuje `amt/src/...` (lokální tree)
   a checkpointy `*.ckpt` o cca 1-3 GB.
3. Klíčový řádek z `app.py`:

   ```python
   precision = '16'  # bf16-mixed/fp16
   model = load_model_checkpoint(args=args, device="cpu")
   model.to("cuda")  # hardcoded
   ```

   → také CUDA-only, navíc fp16 (ne fp32) checkpointy → CPU rebox by chtěl
   manuální konverzi.
4. Závislosti: pytorch-lightning, perceiver-tf, T5 z `transformers`, MoE blocks
   – decentní stack, ale neexistuje PyPI wheel ani modulární `pip install`
   instrukce mimo HuggingFace Space. README explicitně píše "pre-release code"
   a "YouTube currently blocked". Production-ready integraci to není.

### Verdikt

Stejné blokery jako Aria-AMT, plus nemá ani řádný GitHub release. Skipped.

## 3. Co jsem v projektu udělal

- Přidal env-var-driven dispatcher v `src/transcribe_app/transcribe.py`
  (`_run_transcription_backend`): respektuje `TRANSCRIPTION_BACKEND` (default
  `basic_pitch`), pro `klavir` + `aria_amt` se pokusí o Aria-AMT, jinak Basic
  Pitch. Pokud Aria-AMT volání vyhodí výjimku (což aktuálně dělá), loguje
  warning a vrátí Basic Pitch výstup. Pro non-klavir kategorie s
  `aria_amt` rovnou Basic Pitch (Aria je piano-only).
- Přidal modul `src/transcribe_app/aria_amt.py` s funkcí `aria_amt_events`,
  která hodí `RuntimeError` s informativní zprávou. Až bude CPU-friendly cesta
  (např. ONNX export, upstream PR, nebo whisper.cpp-like port), stačí přepsat
  tělo funkce a integrace je hotová.
- Žádné nové runtime dependence se neinstalovaly (Basic Pitch default pipeline
  jede dál, všechny existující testy procházejí).

## 4. Doporučení dál

1. **Sledovat upstream PR / fork** Aria-AMT za CPU podporu (issue tracker na
   EleutherAI/aria-amt). Alternativně použít koncept "ONNX export jen pro
   encoder + decoder" a nahradit autoregressive sampling v `process_segments`
   čistě onnxruntime kódem (model.py je převážně standard transformer, takže by
   to bylo zhruba realistické).
2. **Alternativní piano SOTA**: Bytedance "high-resolution piano transcription"
   (`piano-transcription-inference`, PyPI, MIT, F1 ≈ 0.97, podporuje
   `device="cpu"`). Není to nejnovější, ale pip-installable, CPU-friendly a
   prokazatelně lepší než Basic Pitch na klavíru. Pokud cílem je win na
   `klavir` kategorii, tohle je realističtější další krok než dolaďovat Aria-AMT.
3. **MT3 (Magenta)**: <https://github.com/magenta/mt3> – multi-instrument, ale
   také původně TensorFlow + GPU. Existuje neoficiální PyTorch port, znovu
   ale problematické dependency-wise.
