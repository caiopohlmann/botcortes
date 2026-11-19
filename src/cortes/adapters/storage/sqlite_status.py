"""Estado para o painel no mesmo SQLite: pausas, batimentos, métricas e contagens.

Cada processo (supervisor, monitor, worker) grava o próprio "batimento" a cada poucos
segundos; o painel só lê. Assim o painel não depende de estar no mesmo processo (nem na
mesma máquina, no futuro) e pode cair sem afetar a gravação.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cortes.adapters.storage.sqlite_store import SqliteStore, json_list
from cortes.domain.models import QUALITY_PRESETS, Controls, JobStatus

_SYSTEM_PAUSED = "system_paused"
_PAUSED_ALL = "paused_all"
_WORKER_PAUSED = "worker_paused"
_CHANNEL_PREFIX = "channel:"
_QUALITY_PREFIX = "quality:"  # valor = nome em QUALITY_PRESETS (qualidade escolhida no painel)
_RESTART_PREFIX = "restart:"  # valor = horário (epoch) do pedido de reinício


@dataclass(frozen=True, slots=True)
class Heartbeat:
    process: str
    pid: int
    started_at: float
    updated_at: float
    data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AssistantRequest:
    id: int
    clip_id: str
    message: str
    status: str  # queued | running | done | failed
    created_at: float
    started_at: float | None
    finished_at: float | None
    session_id: str | None
    reply: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ClipRow:
    id: str
    channel: str
    display_name: str
    video_path: Path
    description_path: Path
    score: float
    chat_score: float
    audio_score: float
    duration_s: float
    processing_s: float
    transcript: str
    created_at: float
    caption_grade: str = "revisar"
    caption_score: float = 0.0
    caption_notes: tuple[str, ...] = ()
    doubtful_words: tuple[str, ...] = ()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str, separators=(",", ":"))


class SqliteStatusStore(SqliteStore):
    # ------------------------------------------------------------------ pausas
    def get_controls(self) -> Controls:
        with self._connect() as conn:
            rows = conn.execute("SELECT key, value FROM controls").fetchall()
        values = {row["key"]: row["value"] for row in rows}
        return Controls(
            system_paused=values.get(_SYSTEM_PAUSED) == "1",
            paused_all=values.get(_PAUSED_ALL) == "1",
            worker_paused=values.get(_WORKER_PAUSED) == "1",
            paused_channels=frozenset(
                key[len(_CHANNEL_PREFIX) :]
                for key, value in values.items()
                if key.startswith(_CHANNEL_PREFIX) and value == "1"
            ),
            channel_quality={
                key[len(_QUALITY_PREFIX) :]: value
                for key, value in values.items()
                if key.startswith(_QUALITY_PREFIX) and value in QUALITY_PRESETS
            },
        )

    def _set_flag(self, key: str, on: bool) -> None:
        with self._connect() as conn:
            if on:
                conn.execute(
                    """INSERT INTO controls (key, value, updated_at) VALUES (?, '1', ?)
                       ON CONFLICT(key) DO UPDATE
                       SET value = '1', updated_at = excluded.updated_at""",
                    (key, time.time()),
                )
            else:
                conn.execute("DELETE FROM controls WHERE key = ?", (key,))

    def set_system_paused(self, paused: bool) -> None:
        self._set_flag(_SYSTEM_PAUSED, paused)

    def resume_all(self) -> None:
        """Botão "Iniciar tudo": tira a pausa do sistema e as gerais (as de canal ficam)."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM controls WHERE key IN (?, ?, ?)",
                (_SYSTEM_PAUSED, _PAUSED_ALL, _WORKER_PAUSED),
            )

    def set_paused_all(self, paused: bool) -> None:
        self._set_flag(_PAUSED_ALL, paused)

    def set_worker_paused(self, paused: bool) -> None:
        self._set_flag(_WORKER_PAUSED, paused)

    def set_channel_paused(self, channel_key: str, paused: bool) -> None:
        self._set_flag(f"{_CHANNEL_PREFIX}{channel_key}", paused)

    def set_channel_quality(self, channel_key: str, preset: str | None) -> None:
        """Qualidade escolhida no painel (nome em QUALITY_PRESETS); None volta à do config."""
        key = f"{_QUALITY_PREFIX}{channel_key}"
        with self._connect() as conn:
            if preset is None:
                conn.execute("DELETE FROM controls WHERE key = ?", (key,))
                return
            if preset not in QUALITY_PRESETS:
                raise ValueError(f"qualidade desconhecida: {preset}")
            conn.execute(
                """INSERT INTO controls (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE
                   SET value = excluded.value, updated_at = excluded.updated_at""",
                (key, preset, time.time()),
            )

    # ------------------------------------------------------------------ reinício pelo painel
    def request_restart(self, process: str, at: float | None = None) -> None:
        """Pede ao processo que saia sozinho (o supervisor o sobe de novo).

        Guarda o horário do pedido: o processo só obedece a pedidos mais novos que o próprio
        início, então o registro pode ficar no banco sem causar reinícios em sequência.
        """
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO controls (key, value, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE
                   SET value = excluded.value, updated_at = excluded.updated_at""",
                (f"{_RESTART_PREFIX}{process}", repr(at or time.time()), time.time()),
            )

    def restart_requested_at(self, process: str) -> float | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM controls WHERE key = ?", (f"{_RESTART_PREFIX}{process}",)
            ).fetchone()
        if row is None:
            return None
        try:
            return float(row["value"])
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ batimentos
    def write_heartbeat(
        self, process: str, pid: int, started_at: float, data: dict[str, Any]
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO heartbeats (process, pid, started_at, updated_at, data)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(process) DO UPDATE SET pid = excluded.pid,
                       started_at = excluded.started_at, updated_at = excluded.updated_at,
                       data = excluded.data""",
                (process, pid, started_at, time.time(), _json(data)),
            )

    def heartbeats(self) -> dict[str, Heartbeat]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM heartbeats").fetchall()
        beats: dict[str, Heartbeat] = {}
        for row in rows:
            try:
                data = json.loads(row["data"])
            except ValueError:
                data = {}
            beats[row["process"]] = Heartbeat(
                process=row["process"],
                pid=int(row["pid"]),
                started_at=float(row["started_at"]),
                updated_at=float(row["updated_at"]),
                data=data if isinstance(data, dict) else {},
            )
        return beats

    # ------------------------------------------------------------------ métricas
    def add_metric(self, source: str, data: dict[str, Any], ts: float | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO metrics (ts, source, data) VALUES (?, ?, ?)",
                (time.time() if ts is None else ts, source, _json(data)),
            )

    def metrics_since(self, source: str, since_ts: float) -> list[tuple[float, dict[str, Any]]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, data FROM metrics WHERE source = ? AND ts >= ? ORDER BY ts",
                (source, since_ts),
            ).fetchall()
        samples: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            try:
                data = json.loads(row["data"])
            except ValueError:
                continue
            if isinstance(data, dict):
                samples.append((float(row["ts"]), data))
        return samples

    def prune_metrics(self, older_than_ts: float) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM metrics WHERE ts < ?", (older_than_ts,))
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------------ contagens
    def clips_by_channel(self, since_ts: float) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT channel, COUNT(*) AS n FROM clips WHERE created_at >= ? GROUP BY channel",
                (since_ts,),
            ).fetchall()
        return {row["channel"]: int(row["n"]) for row in rows}

    def count_clips_since(self, since_ts: float) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM clips WHERE created_at >= ?", (since_ts,)
            ).fetchone()
        return int(row[0])

    def clip_times(self, since_ts: float) -> list[float]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT created_at FROM clips WHERE created_at >= ? ORDER BY created_at",
                (since_ts,),
            ).fetchall()
        return [float(row[0]) for row in rows]

    def moment_counts(self, since_ts: float) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM moments WHERE created_at >= ? GROUP BY status",
                (since_ts,),
            ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def moments_by_channel(self, since_ts: float) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT channel, COUNT(*) AS n FROM moments WHERE created_at >= ? GROUP BY channel",
                (since_ts,),
            ).fetchall()
        return {row["channel"]: int(row["n"]) for row in rows}

    def job_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs WHERE status IN (?, ?) GROUP BY status",
                (JobStatus.PENDING.value, JobStatus.RUNNING.value),
            ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def recent_clips(self, limit: int = 20) -> list[ClipRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM clips ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_clip_row(row) for row in rows]

    def caption_counts(self, since_ts: float) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT caption_grade, COUNT(*) AS n FROM clips WHERE created_at >= ? "
                "GROUP BY caption_grade",
                (since_ts,),
            ).fetchall()
        return {row["caption_grade"]: int(row["n"]) for row in rows}

    def clips_by_paths(self, paths: list[str]) -> dict[str, ClipRow]:
        """Cortes pelo caminho do vídeo (como gravado no banco) -> ClipRow."""
        if not paths:
            return {}
        marks = ",".join("?" * len(paths))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM clips WHERE video_path IN ({marks})", paths
            ).fetchall()
        return {row["video_path"]: _clip_row(row) for row in rows}

    def clips_by_ids(self, clip_ids: list[str]) -> dict[str, ClipRow]:
        if not clip_ids:
            return {}
        marks = ",".join("?" * len(clip_ids))
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM clips WHERE id IN ({marks})", clip_ids).fetchall()
        return {row["id"]: _clip_row(row) for row in rows}

    # ------------------------------------------------------------------ pedidos ao Claude
    def add_assistant_request(self, clip_id: str, message: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO assistant_requests (clip_id, message, status, created_at)
                   VALUES (?, ?, 'queued', ?)""",
                (clip_id, message, time.time()),
            )
            return int(cur.lastrowid or 0)

    def claim_assistant_request(self) -> AssistantRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                """UPDATE assistant_requests SET status = 'running', started_at = ?
                    WHERE id = (SELECT id FROM assistant_requests WHERE status = 'queued'
                                 ORDER BY id LIMIT 1)
                RETURNING *""",
                (time.time(),),
            ).fetchone()
        return _assistant_row(row) if row else None

    def finish_assistant_request(
        self,
        request_id: int,
        *,
        ok: bool,
        reply: str | None,
        session_id: str | None,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE assistant_requests
                      SET status = ?, finished_at = ?, reply = ?, session_id = ?, error = ?
                    WHERE id = ?""",
                (
                    "done" if ok else "failed",
                    time.time(),
                    reply,
                    session_id,
                    error[:2000] if error else None,
                    request_id,
                ),
            )

    def fail_interrupted_requests(self) -> int:
        """Pedidos que estavam rodando quando o painel caiu: marca como falha (reenviar)."""
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE assistant_requests SET status = 'failed', finished_at = ?,
                          error = 'interrompido: o painel reiniciou no meio. Envie de novo.'
                    WHERE status = 'running'""",
                (time.time(),),
            )
            return int(cur.rowcount or 0)

    def assistant_requests(
        self, *, clip_id: str | None = None, limit: int = 20
    ) -> list[AssistantRequest]:
        with self._connect() as conn:
            if clip_id is None:
                rows = conn.execute(
                    "SELECT * FROM assistant_requests ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM assistant_requests WHERE clip_id = ? ORDER BY id DESC LIMIT ?",
                    (clip_id, limit),
                ).fetchall()
        return [_assistant_row(row) for row in rows]

    def get_assistant_request(self, request_id: int) -> AssistantRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM assistant_requests WHERE id = ?", (request_id,)
            ).fetchone()
        return _assistant_row(row) if row else None

    def get_clip_row(self, clip_id: str) -> ClipRow | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        return _clip_row(row) if row else None


def _assistant_row(row: Any) -> AssistantRequest:
    return AssistantRequest(
        id=int(row["id"]),
        clip_id=row["clip_id"],
        message=row["message"],
        status=row["status"],
        created_at=float(row["created_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        session_id=row["session_id"],
        reply=row["reply"],
        error=row["error"],
    )


def _clip_row(row: Any) -> ClipRow:
    return ClipRow(
        id=row["id"],
        channel=row["channel"],
        display_name=row["display_name"],
        video_path=Path(row["video_path"]),
        description_path=Path(row["description_path"]),
        score=float(row["score"]),
        chat_score=float(row["chat_score"]),
        audio_score=float(row["audio_score"]),
        duration_s=float(row["duration_s"]),
        processing_s=float(row["processing_s"]),
        transcript=row["transcript"],
        created_at=float(row["created_at"]),
        caption_grade=row["caption_grade"],
        caption_score=float(row["caption_score"]),
        caption_notes=tuple(json_list(row["caption_notes"])),
        doubtful_words=tuple(json_list(row["doubtful_words"])),
    )
