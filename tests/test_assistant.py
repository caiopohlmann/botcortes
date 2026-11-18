from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cortes.adapters.storage.sqlite_status import SqliteStatusStore
from cortes.application.assistant import ClipContext, assistant_loop, build_prompt


def context(kit: bool = True) -> ClipContext:
    return ClipContext(
        clip_id="20261118-210000-twitch-fulano-aaaa",
        creator="Fulano",
        channel_url="twitch.tv/fulano",
        video_path=Path("data/clips/pending/Fulano/2-legenda-revisar/x_nota70.mp4"),
        description_path=Path("data/clips/pending/Fulano/2-legenda-revisar/x_nota70.txt"),
        kit_dir=Path("data/work/20261118-210000-twitch-fulano-aaaa"),
        kit_available=kit,
        caption_grade="revisar",
        caption_notes=("14% das palavras com confiança baixa",),
        transcript="olha o cara jogou o carro",
    )


def test_prompt_has_clip_files_rules_and_message() -> None:
    prompt = build_prompt(context(), 'troca "jogou" por "pulou"')
    assert 'troca "jogou" por "pulou"' in prompt
    assert "uv run cortes rerender 20261118-210000-twitch-fulano-aaaa" in prompt
    assert "transcript.json" in prompt and "render.json" in prompt
    assert "14% das palavras com confiança baixa" in prompt
    assert "Não mexa em código" in prompt
    no_kit = build_prompt(context(kit=False), "x")
    assert "indisponível" in no_kit


@dataclass
class FakeResult:
    ok: bool
    reply: str
    session_id: str | None
    error: str | None


class FakeRunner:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run(self, prompt: str) -> FakeResult:
        self.prompts.append(prompt)
        if "falhar" in prompt:
            raise RuntimeError("claude não encontrado")
        return FakeResult(True, "Troquei a palavra e gerei de novo.", "abc12345-0000", None)


def test_loop_runs_queue_in_order_and_records_results() -> None:
    store = SqliteStatusStore(Path(tempfile.mkdtemp()) / "cortes.db")
    first = store.add_assistant_request("clip-1", "corrige a legenda")
    second = store.add_assistant_request("clip-2", "isso vai falhar")
    third = store.add_assistant_request("sumiu", "corte que não existe")
    runner = FakeRunner()

    def context_for(clip_id: str) -> ClipContext | None:
        return None if clip_id == "sumiu" else context()

    async def main() -> None:
        task = asyncio.create_task(
            assistant_loop(
                claim=store.claim_assistant_request,
                finish=store.finish_assistant_request,
                context_for=context_for,
                runner=runner,
                poll_s=0.01,
            )
        )
        for _ in range(200):
            await asyncio.sleep(0.01)
            if all(
                (r := store.get_assistant_request(i)) is not None
                and r.status in ("done", "failed")
                for i in (first, second, third)
            ):
                break
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    done = store.get_assistant_request(first)
    assert done is not None and done.status == "done"
    assert done.reply == "Troquei a palavra e gerei de novo." and done.session_id == "abc12345-0000"
    failed = store.get_assistant_request(second)
    assert failed is not None and failed.status == "failed"
    assert "não encontrado" in (failed.error or "")
    missing = store.get_assistant_request(third)
    assert missing is not None and missing.status == "failed"
    assert len(runner.prompts) == 2


def test_interrupted_requests_are_marked_failed() -> None:
    store = SqliteStatusStore(Path(tempfile.mkdtemp()) / "cortes.db")
    store.add_assistant_request("clip-1", "a")
    claimed = store.claim_assistant_request()
    assert claimed is not None and claimed.status == "running"
    assert store.claim_assistant_request() is None  # fila vazia
    assert store.fail_interrupted_requests() == 1
    item = store.get_assistant_request(claimed.id)
    assert item is not None and item.status == "failed" and "reiniciou" in (item.error or "")
    assert [r.id for r in store.assistant_requests(clip_id="clip-1")] == [claimed.id]
