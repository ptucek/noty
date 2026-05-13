# Konfigurace

## Lokální setup (macOS)

```bash
# 1. Systémové
brew install --cask musescore   # MuseScore 4 (~500 MB)
brew install uv ffmpeg          # uv pro Python deps, ffmpeg pro audio-separator

# 2. Klon + deps
git clone https://github.com/ptucek/noty.git
cd noty
uv sync                          # ~2-3 min, 2 GB ML deps

# 3. (volitelně) .env
cp .env.example .env
# uprav podle potřeby

# 4. Spuštění
uv run python app.py             # http://localhost:7860
```

## Lokální setup (Linux)

```bash
# 1. Systémové
sudo apt install musescore3 ffmpeg libsndfile1 git build-essential
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2-4. Stejně jako macOS, jen MuseScore binárka je `mscore3`
export MUSESCORE_PATH=/usr/bin/mscore3
uv run python app.py
```

## Environment variables

### Základní
| Env var | Default | Co dělá |
|---|---|---|
| `MUSESCORE_PATH` | auto | Cesta k binárce. macOS auto: `/Applications/MuseScore 4.app/Contents/MacOS/mscore`. Linux: `/usr/bin/mscore3`. |
| `MAX_AUDIO_SECONDS` | 60 | Hard-cap délky vstupu — delší se ořízne |
| `GRADIO_SERVER_NAME` | `127.0.0.1` | Pro public přístup: `0.0.0.0` |
| `GRADIO_SERVER_PORT` | `7860` | Port |

### Backend volby
| Env var | Default | Možnosti | Co dělá |
|---|---|---|---|
| `SEPARATION_BACKEND` | `roformer` | `roformer`, `demucs` | Pro `vokal` kategorii: RoFormer (lepší, ~600MB) nebo Demucs (rychlejší, ~80MB) |
| `TRANSCRIPTION_BACKEND` | `basic_pitch` | `basic_pitch`, `aria_amt` | Aria-AMT scaffolding (aktuálně raises RuntimeError) |

### LLM cleanup
| Env var | Default | Co dělá |
|---|---|---|
| `LLM_CLEANUP` | `0` | `1` zapne post-process |
| `CLEANUP_MODEL` | `claude-opus-4-6` | Model name — `claude-opus-4-7`, `claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5` |

### Claude API auth (vyber JEDNU cestu)

**A) Microsoft Foundry s API klíčem** (firemní billing přes Azure):
```bash
export ANTHROPIC_FOUNDRY_RESOURCE=ptuc-foundry-test    # resource name
export ANTHROPIC_FOUNDRY_API_KEY=...                    # API key z Foundry portálu
```

**B) Microsoft Foundry s Entra ID** (managed identity / DefaultAzureCredential):
```bash
export ANTHROPIC_FOUNDRY_RESOURCE=ptuc-foundry-test
# bez API key — vyžaduje `az login` lokálně, nebo MI s rolí "Cognitive Services User"
```

**C) First-party Anthropic API** (osobní účet, billing přímo Anthropic):
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

**Priorita auth (pokud více cest):** A > B > C.

## Vytvoření MS Foundry resource pro Claude

1. Azure portál → vytvořit **Azure AI Foundry** resource
   - Region: `East US 2` nebo `Sweden Central` (Claude jen tam)
   - Subscription: musí být Enterprise / MCA-E (ne free/student)
2. V Foundry projektu → Deployments → Add Claude model (Opus 4.7, Haiku 4.5, atd.)
3. Po deployi: Keys & Endpoint → zkopíruj **Resource name** + **Key**

## Test data setup (volitelné)

Pro spuštění correctness testů:

```bash
# Synthetic fixtures jsou v repo (committed)
uv run python tests/test_pipeline.py             # rychlý smoke test
uv run python tests/test_correctness.py          # 47 fixtur, ~5 min

# Mutopia (43 reálných skladeb) — regeneruje na demand
uv run python tests/fixtures/mutopia/download.py # ~10 min, 75 MB

# Open Goldberg (8 reálných nahrávek) — CC0
uv run python tests/fixtures/real/download.py    # ~5 min, 41 MB

# Time signature accuracy (vyžaduje výše stažené)
uv run python tests/test_time_signature.py
```

## Performance tipy

| Co | Default | Tip |
|---|---|---|
| Basic Pitch první run | ~5s (model load) | Cached pro další requesty |
| Demucs první run | ~10-30s (model load + separation) | Vokál izolace je vždy nejpomalejší krok |
| RoFormer první run | ~15-40s (model download + load + separation) | Cached po prvním běhu |
| MuseScore render | ~2-5s | Single-threaded, neoptimalizovatelné |
| LLM cleanup | ~3-10s | Cache control aktivní, druhý call s podobným promptem ~10x rychlejší |
