"""Kit de edição de um corte: tudo que é preciso para gerá-lo de novo com ajustes.

Fica em data/work/<id do corte>/ enquanto o corte não for curado (padrão: 7 dias):

  source.ts        trecho original da live (não editar)
  transcript.json  legenda: palavras com início/fim (s) e confiança. EDITÁVEL
  render.json      onde o corte começa e quanto dura dentro do source.ts. EDITÁVEL
  subs.ass         legenda renderizada da última vez (gerada de novo a cada render)

Depois de editar, `uv run cortes rerender <id do corte>` gera o vídeo outra vez, refaz a
classificação da legenda e move o corte para a pasta certa (boa / revisar / ruim).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cortes.domain.models import Transcript, Word

TRANSCRIPT_FILE = "transcript.json"
RENDER_FILE = "render.json"
SOURCE_FILE = "source.ts"

_TRANSCRIPT_HELP = (
    "Legenda do corte. Para corrigir: troque words[].text (uma palavra por item). "
    "Para mudar o tempo: words[].start/end em segundos, relativos ao início do corte. "
    "Palavra corrigida: ponha probability 1.0. Remover = apagar o item; juntar/dividir "
    "palavras = ajustar os itens e os tempos. Depois rode: uv run cortes rerender <id>"
)
_RENDER_HELP = (
    "offset_s = onde o corte começa dentro do source.ts (s); duration_s = duração (s). "
    "Para cortar o começo, aumente offset_s e diminua duration_s na mesma medida "
    "(e subtraia o mesmo valor de start/end das palavras em transcript.json)."
)


@dataclass(frozen=True, slots=True)
class EditKit:
    directory: Path
    transcript: Transcript
    offset_s: float
    duration_s: float

    @property
    def source(self) -> Path:
        return self.directory / SOURCE_FILE


def write_kit(directory: Path, transcript: Transcript, offset_s: float, duration_s: float) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    transcript_data: dict[str, Any] = {
        "_ajuda": _TRANSCRIPT_HELP,
        "language": transcript.language,
        "language_probability": transcript.language_probability,
        "language_forced": transcript.language_forced,
        "dropped_segments": transcript.dropped_segments,
        "max_compression_ratio": transcript.max_compression_ratio,
        "words": [
            {
                "text": w.text,
                "start": round(w.start, 3),
                "end": round(w.end, 3),
                "probability": round(w.probability, 3),
            }
            for w in transcript.words
        ],
    }
    render_data = {"_ajuda": _RENDER_HELP, "offset_s": offset_s, "duration_s": duration_s}
    _write_json(directory / TRANSCRIPT_FILE, transcript_data)
    _write_json(directory / RENDER_FILE, render_data)


def read_kit(directory: Path) -> EditKit:
    missing = [
        name
        for name in (SOURCE_FILE, TRANSCRIPT_FILE, RENDER_FILE)
        if not (directory / name).is_file()
    ]
    if missing:
        raise FileNotFoundError(
            f"kit de edição incompleto em {directory} (faltando: {', '.join(missing)}). "
            "Kits são apagados após worker.edit_kit_days."
        )
    data = json.loads((directory / TRANSCRIPT_FILE).read_text(encoding="utf-8"))
    render = json.loads((directory / RENDER_FILE).read_text(encoding="utf-8"))
    words = tuple(
        Word(
            text=str(item["text"]).strip(),
            start=float(item["start"]),
            end=float(item["end"]),
            probability=float(item.get("probability", 1.0)),
        )
        for item in data.get("words", [])
        if str(item.get("text", "")).strip()
    )
    ratio = data.get("max_compression_ratio")
    language_probability = data.get("language_probability")
    transcript = Transcript(
        language=str(data.get("language") or "pt"),
        text=" ".join(w.text for w in words),
        words=tuple(sorted(words, key=lambda w: w.start)),
        language_probability=float(language_probability)
        if language_probability is not None
        else None,
        language_forced=bool(data.get("language_forced", False)),
        dropped_segments=int(data.get("dropped_segments") or 0),
        max_compression_ratio=float(ratio) if ratio is not None else None,
    )
    offset = float(render["offset_s"])
    duration = float(render["duration_s"])
    if offset < 0 or duration <= 0:
        raise ValueError("render.json: offset_s deve ser >= 0 e duration_s > 0")
    return EditKit(directory=directory, transcript=transcript, offset_s=offset, duration_s=duration)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
