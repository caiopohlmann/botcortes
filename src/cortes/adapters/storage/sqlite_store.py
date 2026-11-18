"""SQLite (modo WAL) para metadados e fila de jobs.

Com tudo num PC só, uma tabela de jobs com "lease" substitui o Redis: é durável
(sobrevive a crash/reboot), não exige outro serviço rodando e aguenta com folga a
vazão de clipes (dezenas por hora). Se o worker morrer no meio, o lease expira e o job
volta para a fila. Quando o monitor for para a VPS, basta outro adaptador de JobQueue
(Redis Streams) implementando a mesma porta.

Uma conexão por operação: sqlite3 não é thread-safe por conexão e as chamadas vêm de
asyncio.to_thread. Abrir conexão SQLite custa microssegundos.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cortes.domain.models import Clip, ClipStatus, Job, JobStatus, Moment, MomentStatus

_SCHEMA = """
CREATE TABLE IF NOT EXISTS moments (
    id              TEXT PRIMARY KEY,
    channel         TEXT NOT NULL,
    display_name    TEXT NOT NULL,
    peak_ts         REAL NOT NULL,
    event_ts        REAL NOT NULL,
    start_ts        REAL NOT NULL,
    end_ts          REAL NOT NULL,
    chat_score      REAL NOT NULL,
    source_path     TEXT NOT NULL,
    source_start_ts REAL NOT NULL,
    source_end_ts   REAL NOT NULL,
    status          TEXT NOT NULL,
    final_score     REAL,
    error           TEXT,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_moments_channel ON moments(channel, peak_ts);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    moment_id    TEXT NOT NULL REFERENCES moments(id),
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    locked_until REAL,
    error        TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);

CREATE TABLE IF NOT EXISTS clips (
    id               TEXT PRIMARY KEY,
    moment_id        TEXT NOT NULL REFERENCES moments(id),
    channel          TEXT NOT NULL,
    display_name     TEXT NOT NULL,
    video_path       TEXT NOT NULL,
    description_path TEXT NOT NULL,
    score            REAL NOT NULL,
    chat_score       REAL NOT NULL,
    audio_score      REAL NOT NULL,
    language         TEXT NOT NULL,
    transcript       TEXT NOT NULL,
    duration_s       REAL NOT NULL,
    processing_s     REAL NOT NULL,
    status           TEXT NOT NULL,
    created_at       REAL NOT NULL,
    caption_grade    TEXT NOT NULL DEFAULT 'revisar',  -- boa | revisar | ruim
    caption_score    REAL NOT NULL DEFAULT 0,
    caption_notes    TEXT NOT NULL DEFAULT '[]',       -- JSON: motivos da classificação
    doubtful_words   TEXT NOT NULL DEFAULT '[]'        -- JSON: palavras duvidosas
);
CREATE INDEX IF NOT EXISTS idx_clips_status_score ON clips(status, score DESC);
CREATE INDEX IF NOT EXISTS idx_clips_created ON clips(created_at);

-- Painel: pausas, "batimento" de cada processo e histórico de métricas.
CREATE TABLE IF NOT EXISTS controls (
    key        TEXT PRIMARY KEY,   -- paused_all | worker_paused | channel:<Channel.key>
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeats (
    process    TEXT PRIMARY KEY,   -- supervisor | monitor | worker | panel
    pid        INTEGER NOT NULL,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    data       TEXT NOT NULL       -- JSON com o estado do processo
);

CREATE TABLE IF NOT EXISTS metrics (
    ts      REAL NOT NULL,
    source  TEXT NOT NULL,         -- host | gpu
    data    TEXT NOT NULL          -- JSON
);
CREATE INDEX IF NOT EXISTS idx_metrics_source_ts ON metrics(source, ts);

-- Atalho do painel: pedidos de ajuste em um corte, executados pelo Claude Code.
CREATE TABLE IF NOT EXISTS assistant_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id     TEXT NOT NULL,
    message     TEXT NOT NULL,
    status      TEXT NOT NULL,     -- queued | running | done | failed
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    session_id  TEXT,              -- para continuar no terminal: claude --resume <id>
    reply       TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_assistant_status ON assistant_requests(status, id);
CREATE INDEX IF NOT EXISTS idx_assistant_clip ON assistant_requests(clip_id, id);
"""


class SqliteStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:  # commit no sucesso, rollback em exceção
                yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------ moments
    def add_moment(self, moment: Moment) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO moments (id, channel, display_name, peak_ts, event_ts, start_ts,
                       end_ts, chat_score, source_path, source_start_ts, source_end_ts, status,
                       final_score, error, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    moment.id, moment.channel, moment.display_name, moment.peak_ts,
                    moment.event_ts, moment.start_ts, moment.end_ts, moment.chat_score,
                    str(moment.source_path), moment.source_start_ts, moment.source_end_ts,
                    moment.status.value, moment.final_score, moment.error, moment.created_at,
                ),
            )  # fmt: skip

    def get_moment(self, moment_id: str) -> Moment | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM moments WHERE id = ?", (moment_id,)).fetchone()
        if row is None:
            return None
        return Moment(
            id=row["id"],
            channel=row["channel"],
            display_name=row["display_name"],
            peak_ts=row["peak_ts"],
            event_ts=row["event_ts"],
            start_ts=row["start_ts"],
            end_ts=row["end_ts"],
            chat_score=row["chat_score"],
            source_path=Path(row["source_path"]),
            source_start_ts=row["source_start_ts"],
            source_end_ts=row["source_end_ts"],
            created_at=row["created_at"],
            status=MomentStatus(row["status"]),
            final_score=row["final_score"],
            error=row["error"],
        )

    def set_moment_status(
        self,
        moment_id: str,
        status: MomentStatus,
        *,
        final_score: float | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE moments
                      SET status = ?, final_score = COALESCE(?, final_score), error = ?
                    WHERE id = ?""",
                (status.value, final_score, error, moment_id),
            )

    # ------------------------------------------------------------------ clips
    def add_clip(self, clip: Clip) -> None:
        with self._connect() as conn:
            # OR REPLACE: um job refeito (falhou depois de gravar o clipe) não quebra aqui.
            conn.execute(
                """INSERT OR REPLACE INTO clips (id, moment_id, channel, display_name, video_path,
                       description_path, score, chat_score, audio_score, language, transcript,
                       duration_s, processing_s, status, created_at, caption_grade,
                       caption_score, caption_notes, doubtful_words)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    clip.id, clip.moment_id, clip.channel, clip.display_name,
                    str(clip.video_path), str(clip.description_path), clip.score,
                    clip.chat_score, clip.audio_score, clip.language, clip.transcript,
                    clip.duration_s, clip.processing_s, clip.status.value, clip.created_at,
                    clip.caption_grade, clip.caption_score,
                    json.dumps(list(clip.caption_notes), ensure_ascii=False),
                    json.dumps(list(clip.doubtful_words), ensure_ascii=False),
                ),
            )  # fmt: skip

    def get_clip(self, clip_id: str) -> Clip | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        if row is None:
            return None
        return Clip(
            id=row["id"],
            moment_id=row["moment_id"],
            channel=row["channel"],
            display_name=row["display_name"],
            video_path=Path(row["video_path"]),
            description_path=Path(row["description_path"]),
            score=row["score"],
            chat_score=row["chat_score"],
            audio_score=row["audio_score"],
            language=row["language"],
            transcript=row["transcript"],
            duration_s=row["duration_s"],
            processing_s=row["processing_s"],
            created_at=row["created_at"],
            status=ClipStatus(row["status"]),
            caption_grade=row["caption_grade"],
            caption_score=row["caption_score"],
            caption_notes=tuple(json_list(row["caption_notes"])),
            doubtful_words=tuple(json_list(row["doubtful_words"])),
        )

    def count_clips(self, status: ClipStatus | None = None) -> int:
        with self._connect() as conn:
            if status is None:
                row = conn.execute("SELECT COUNT(*) FROM clips").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM clips WHERE status = ?", (status.value,)
                ).fetchone()
        return int(row[0])

    # ------------------------------------------------------------------ fila de jobs
    def enqueue(self, moment_id: str) -> int:
        now = time.time()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO jobs (moment_id, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (moment_id, JobStatus.PENDING.value, now, now),
            )
            return int(cur.lastrowid or 0)

    def claim(self, lease_s: float) -> Job | None:
        """Pega o job pendente mais antigo (ou um RUNNING com lease vencido) de forma atômica."""
        now = time.time()
        with self._connect() as conn:
            row = conn.execute(
                """UPDATE jobs
                      SET status = ?, attempts = attempts + 1, locked_until = ?, updated_at = ?
                    WHERE id = (
                        SELECT id FROM jobs
                         WHERE status = ? OR (status = ? AND locked_until < ?)
                         ORDER BY id LIMIT 1)
                RETURNING id, moment_id, attempts""",
                (
                    JobStatus.RUNNING.value, now + lease_s, now,
                    JobStatus.PENDING.value, JobStatus.RUNNING.value, now,
                ),
            ).fetchone()  # fmt: skip
        if row is None:
            return None
        return Job(id=row["id"], moment_id=row["moment_id"], attempts=row["attempts"])

    def complete(self, job_id: int) -> None:
        self._set_job(job_id, JobStatus.DONE, None)

    def release(self, job_id: int) -> None:
        """Devolve o job à fila sem contar tentativa (ex.: worker sendo desligado)."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE jobs SET status = ?, attempts = MAX(attempts - 1, 0),
                          locked_until = NULL, updated_at = ?
                    WHERE id = ? AND status = ?""",
                (JobStatus.PENDING.value, time.time(), job_id, JobStatus.RUNNING.value),
            )

    def fail(self, job_id: int, error: str, max_attempts: int) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
            attempts = int(row["attempts"]) if row else max_attempts
            status = JobStatus.FAILED if attempts >= max_attempts else JobStatus.PENDING
            conn.execute(
                """UPDATE jobs SET status = ?, error = ?, locked_until = NULL, updated_at = ?
                    WHERE id = ?""",
                (status.value, error[:4000], time.time(), job_id),
            )

    def _set_job(self, job_id: int, status: JobStatus, error: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, error = ?, locked_until = NULL, updated_at = ? "
                "WHERE id = ?",
                (status.value, error, time.time(), job_id),
            )


def json_list(raw: str | None) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return [str(item) for item in data] if isinstance(data, list) else []
