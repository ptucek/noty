"""Time-signature accuracy benchmark: Mutopia GT vs naše detekce.

Spuštění:
    LLM_CLEANUP=1 CLEANUP_MODEL=claude-opus-4-7 \\
    ANTHROPIC_FOUNDRY_RESOURCE=... ANTHROPIC_FOUNDRY_API_KEY=... \\
    uv run python tests/test_time_signature.py

Volitelně omez počet fixtur:
    NTS_LIMIT=10 uv run python tests/test_time_signature.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from transcribe_app.transcribe import transcribe  # noqa: E402

FIXTURES_DIR = ROOT / "tests" / "fixtures"
MUTOPIA_INDEX = FIXTURES_DIR / "mutopia" / "INDEX.json"
REAL_INDEX = FIXTURES_DIR / "real" / "INDEX.json"

# Mapování ground-truth time-signature stringů na náš detekovaný formát.
NORMALIZE = {
    "2/4": "2/4", "3/4": "3/4", "4/4": "4/4", "6/8": "6/8",
    "C": "4/4",  # common time
    "2/2": "4/4",  # alla breve mapuje na 4/4 v našem výstupu
    "3/8": "3/4",  # 3/8 → 3/4 (přibližně)
    "6/4": "6/8",
    "12/8": "6/8",
    "9/8": "6/8",  # devítka mapuje na 6/8 (oba compound triple-like)
    "18/16": "6/8",  # Variation 26 outlier
}


def _collect_pieces() -> list[tuple[str, dict]]:
    """Sesbírá pieces z obou INDEX.json (Mutopia + real/OpenGoldberg).

    Vrací list (dataset_label, piece_dict). dataset_label je "mutopia" nebo "real".
    """
    out: list[tuple[str, dict]] = []
    if MUTOPIA_INDEX.exists():
        data = json.loads(MUTOPIA_INDEX.read_text(encoding="utf-8"))
        for p in data.get("pieces", []):
            out.append(("mutopia", p))
    if REAL_INDEX.exists():
        data = json.loads(REAL_INDEX.read_text(encoding="utf-8"))
        for p in data.get("pieces", []):
            out.append(("real", p))
    return out


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    pieces = _collect_pieces()
    if not pieces:
        print("Žádné INDEX.json fixtures nejsou k dispozici")
        return

    limit = int(os.environ.get("NTS_LIMIT", "0"))
    if limit:
        pieces = pieces[:limit]

    print(f"=== Testuji {len(pieces)} fixtur ===\n")
    results: list[tuple[str, str, str, str, str, bool]] = []
    for dataset, p in pieces:
        wav = FIXTURES_DIR / dataset / p["wav"]
        if not wav.exists():
            continue
        gt_raw = (p.get("time_signature") or "").strip()
        gt = NORMALIZE.get(gt_raw, gt_raw)
        with tempfile.TemporaryDirectory(prefix=f"nts_{p['slug']}_") as tmp:
            try:
                result = transcribe(wav, p["category"], Path(tmp))
            except Exception as exc:
                print(f"  [ERR] [{dataset}] {p['slug']}: {exc!r}")
                continue
            pred = result.time_signature
            match = pred == gt
            results.append((dataset, p["category"], p["slug"], gt, pred, match))
            mark = "✓" if match else "✗"
            print(
                f"  {mark} [{dataset:7s}/{p['category']:9s}] {p['slug']:40s}  "
                f"GT={gt:5s}  PRED={pred:5s}"
            )

    print("\n=== SHRNUTÍ ===")
    total = len(results)
    correct = sum(1 for r in results if r[5])
    print(f"  Správně: {correct}/{total} ({100*correct/max(total,1):.1f} %)")
    # Per-dataset breakdown
    by_ds: dict[str, tuple[int, int]] = {}
    for ds, _, _, _, _, m in results:
        ok, n = by_ds.get(ds, (0, 0))
        by_ds[ds] = (ok + int(m), n + 1)
    print("  Podle datasetu:")
    for ds, (ok, n) in sorted(by_ds.items()):
        print(f"    {ds:8s}: {ok}/{n} ({100*ok/n:.0f} %)")
    by_gt: dict[str, tuple[int, int]] = {}
    for _, _, _, gt, _, m in results:
        ok, n = by_gt.get(gt, (0, 0))
        by_gt[gt] = (ok + int(m), n + 1)
    print("  Podle GT taktu:")
    for gt, (ok, n) in sorted(by_gt.items()):
        print(f"    {gt:5s}: {ok}/{n} ({100*ok/n:.0f} %)")


if __name__ == "__main__":
    main()
