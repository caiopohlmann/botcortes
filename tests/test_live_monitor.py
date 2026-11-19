from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import pytest

from cortes.application.live_monitor import ChannelState, LiveMonitor
from cortes.domain.models import (
    QUALITY_PRESETS,
    Channel,
    Controls,
    LiveStream,
    Platform,
    RecorderStats,
    SessionStats,
)

TW = Channel(login="a")
KI = Channel(login="a", platform=Platform.KICK)  # mesmo login, outra plataforma


def live(channel: Channel, game: str = "GTA VI") -> LiveStream:
    return LiveStream(key=channel.key, user_name=channel.login, title="", game_name=game)


class ScriptedStatus:
    """Devolve um resultado por chamada (Exception = falha da plataforma)."""

    def __init__(self, script: list[Mapping[str, LiveStream | None] | Exception]) -> None:
        self.script = script
        self.calls = 0

    async def get_live_streams(
        self, channels: Sequence[Channel]
    ) -> Mapping[str, LiveStream | None]:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(step, Exception):
            raise step
        return step


class FakeSession:
    def __init__(self, owner: Sessions, key: str, crash: bool) -> None:
        self.owner, self.key, self.crash = owner, key, crash

    async def run(self) -> None:
        self.owner.started.append(self.key)
        if self.crash:
            raise RuntimeError("bug simulado")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.owner.stopped.append(self.key)
            raise

    def snapshot(self) -> SessionStats:
        return SessionStats(
            started_at=0.0,
            recorder=RecorderStats(restarts=0, last_segment_at=None, buffered_s=0.0),
            chat_rate=1.0,
            chat_baseline=1.0,
            chat_score=0.0,
            chat_warm=True,
            last_message_at=None,
            moments_detected=0,
            moments_queued=0,
            last_moment_at=None,
        )


class Sessions:
    def __init__(self, crash: bool = False) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.qualities: list[str | None] = []  # qualidade com que cada sessão foi criada
        self.crash = crash

    def factory(self, channel: Channel, stream: LiveStream) -> FakeSession:
        self.qualities.append(channel.quality)
        return FakeSession(self, channel.key, self.crash)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def run_cycles(monitor: LiveMonitor, polls: int, *, stop: bool = True) -> None:
    async def main() -> None:
        for _ in range(polls):
            await monitor.cycle(poll=True)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        if stop:
            await monitor._stop_all()

    asyncio.run(main())


def test_failed_platform_does_not_stop_its_sessions() -> None:
    twitch = ScriptedStatus([{TW.key: live(TW)}, RuntimeError("rede"), RuntimeError("rede")])
    kick = ScriptedStatus([{KI.key: live(KI)}, {KI.key: None}, {KI.key: None}])
    sessions = Sessions()
    monitor = LiveMonitor(
        [TW, KI],
        {Platform.TWITCH: twitch, Platform.KICK: kick},
        sessions.factory,
        offline_grace_polls=2,
    )

    async def main() -> None:
        for _ in range(3):
            await monitor.cycle(poll=True)
            await asyncio.sleep(0)
        states = {s.channel.key: s.state for s in monitor.snapshot()}
        # Twitch falhou 2x: continua gravando. Kick offline 2x: parou.
        assert states[TW.key] is ChannelState.RECORDING
        assert states[KI.key] is ChannelState.OFFLINE
        await monitor._stop_all()

    asyncio.run(main())
    assert sorted(sessions.started) == sorted([TW.key, KI.key])
    assert sessions.stopped == [KI.key, TW.key]


def test_partial_result_keeps_unknown_channels() -> None:
    other = Channel(login="b")
    status = ScriptedStatus([{TW.key: live(TW), other.key: live(other)}, {other.key: None}])
    sessions = Sessions()
    monitor = LiveMonitor(
        [TW, other], {Platform.TWITCH: status}, sessions.factory, offline_grace_polls=1
    )
    run_cycles(monitor, 2)
    # "a" ficou sem resposta na 2ª rodada: não foi parado antes do _stop_all final.
    assert sessions.stopped[0] == other.key


def test_backoff_skips_failed_platform() -> None:
    twitch = ScriptedStatus([RuntimeError("x")])
    monitor = LiveMonitor([TW], {Platform.TWITCH: twitch}, Sessions().factory)
    run_cycles(monitor, 6)
    # Falhas seguidas: tenta na 1ª, 2ª, pula 1, tenta, pula 3... -> menos chamadas que polls.
    assert twitch.calls < 6
    assert monitor.snapshot()[0].state is ChannelState.UNKNOWN


def test_game_filter_ignores_platforms_without_game() -> None:
    yt = Channel(login="canal", platform=Platform.YOUTUBE)
    status = ScriptedStatus([{yt.key: live(yt, game="")}])
    sessions = Sessions()
    monitor = LiveMonitor(
        [yt], {Platform.YOUTUBE: status}, sessions.factory, game_filter=["Grand Theft Auto VI"]
    )
    run_cycles(monitor, 1)
    assert sessions.started == [yt.key]


def test_other_game_is_filtered() -> None:
    status = ScriptedStatus([{TW.key: live(TW, game="Minecraft")}])
    sessions = Sessions()
    monitor = LiveMonitor([TW], {Platform.TWITCH: status}, sessions.factory, game_filter=["GTA VI"])
    run_cycles(monitor, 1, stop=False)
    assert sessions.started == []
    assert monitor.snapshot()[0].state is ChannelState.FILTERED


def test_pause_and_resume_channel() -> None:
    status = ScriptedStatus([{TW.key: live(TW), KI.key: live(KI)}])
    kick = ScriptedStatus([{KI.key: live(KI)}])
    sessions = Sessions()
    controls = {"value": Controls()}

    async def read_controls() -> Controls:
        return controls["value"]

    monitor = LiveMonitor(
        [TW, KI],
        {Platform.TWITCH: status, Platform.KICK: kick},
        sessions.factory,
        controls=read_controls,
    )

    async def main() -> None:
        await monitor.cycle(poll=True)
        await asyncio.sleep(0)
        assert sorted(sessions.started) == sorted([TW.key, KI.key])

        # Pausa só a Twitch: a sessão dela para no próximo ciclo (sem precisar de poll).
        controls["value"] = Controls(paused_channels=frozenset({TW.key}))
        await monitor.cycle(poll=False)
        assert sessions.stopped == [TW.key]
        states = {s.channel.key: s for s in monitor.snapshot()}
        assert states[TW.key].state is ChannelState.PAUSED
        assert states[TW.key].stream is not None  # continua mostrando a live

        # Retoma: volta a gravar com a info da última consulta, sem esperar o poll.
        controls["value"] = Controls()
        await monitor.cycle(poll=False)
        await asyncio.sleep(0)
        assert sessions.started.count(TW.key) == 2

        # Pausa geral: para tudo.
        controls["value"] = Controls(paused_all=True)
        await monitor.cycle(poll=False)
        assert all(s.state is ChannelState.PAUSED for s in monitor.snapshot())
        await monitor._stop_all()

    asyncio.run(main())


def test_quality_chosen_in_panel_restarts_only_that_channel() -> None:
    status = ScriptedStatus([{TW.key: live(TW), KI.key: live(KI)}])
    kick = ScriptedStatus([{KI.key: live(KI)}])
    sessions = Sessions()
    controls = {"value": Controls()}

    async def read_controls() -> Controls:
        return controls["value"]

    monitor = LiveMonitor(
        [TW, KI],
        {Platform.TWITCH: status, Platform.KICK: kick},
        sessions.factory,
        controls=read_controls,
    )

    async def main() -> None:
        await monitor.cycle(poll=True)
        await asyncio.sleep(0)
        assert sessions.qualities == [None, None]  # sem escolha: a do config

        # 720p no painel só para a Twitch: reinicia a gravação dela, a do Kick segue.
        controls["value"] = Controls(channel_quality={TW.key: "720p"})
        await monitor.cycle(poll=False)
        await asyncio.sleep(0)
        assert sessions.stopped == [TW.key]
        assert sessions.started.count(TW.key) == 2 and sessions.started.count(KI.key) == 1
        assert sessions.qualities[-1] == QUALITY_PRESETS["720p"]
        states = {s.channel.key: s for s in monitor.snapshot()}
        assert states[TW.key].channel.quality == QUALITY_PRESETS["720p"]
        assert states[TW.key].state is ChannelState.RECORDING

        # Mesma escolha nos ciclos seguintes: não reinicia de novo.
        await monitor.cycle(poll=True)
        await monitor.cycle(poll=False)
        assert sessions.started.count(TW.key) == 2

        # "usar o config": volta à qualidade do channels.yaml (reinicia uma vez).
        controls["value"] = Controls()
        await monitor.cycle(poll=False)
        await asyncio.sleep(0)
        assert sessions.started.count(TW.key) == 3 and sessions.qualities[-1] is None
        await monitor._stop_all()

    asyncio.run(main())


def test_quality_chosen_while_paused_applies_on_next_start() -> None:
    status = ScriptedStatus([{TW.key: live(TW)}])
    sessions = Sessions()
    controls = {"value": Controls(paused_channels=frozenset({TW.key}))}

    async def read_controls() -> Controls:
        return controls["value"]

    monitor = LiveMonitor([TW], {Platform.TWITCH: status}, sessions.factory, controls=read_controls)

    async def main() -> None:
        await monitor.cycle(poll=True)
        assert sessions.started == []
        controls["value"] = Controls(
            paused_channels=frozenset({TW.key}), channel_quality={TW.key: "720p"}
        )
        await monitor.cycle(poll=False)
        assert sessions.started == []  # pausado: só guarda a escolha
        assert monitor.snapshot()[0].channel.quality == QUALITY_PRESETS["720p"]

        controls["value"] = Controls(channel_quality={TW.key: "720p"})  # Retomar
        await monitor.cycle(poll=False)
        await asyncio.sleep(0)
        assert sessions.qualities == [QUALITY_PRESETS["720p"]]
        await monitor._stop_all()

    asyncio.run(main())


def test_channels_yaml_change_adds_and_removes_without_touching_others() -> None:
    new = Channel(login="novo")
    status = ScriptedStatus([{TW.key: live(TW), new.key: live(new)}])
    kick = ScriptedStatus([{KI.key: live(KI)}])
    sessions = Sessions()
    monitor = LiveMonitor([TW], {Platform.TWITCH: status}, sessions.factory)

    async def main() -> None:
        await monitor.cycle(poll=True)
        await asyncio.sleep(0)
        assert sessions.started == [TW.key]
        polls = status.calls

        # Canal novo na Twitch: entra já no próximo ciclo (consulta sem esperar os 30 s).
        monitor.set_channels([TW, new])
        await monitor.cycle(poll=False)
        await asyncio.sleep(0)
        assert status.calls == polls + 1
        assert sessions.started == [TW.key, new.key] and sessions.stopped == []

        # Plataforma nova sem provedor: fica de fora (com erro no log) até ter o provedor.
        monitor.set_channels([TW, new, KI])
        await monitor.cycle(poll=False)
        assert KI.key not in {s.channel.key for s in monitor.snapshot()}
        monitor.add_providers({Platform.KICK: kick})
        monitor.set_channels([TW, new, KI])
        await monitor.cycle(poll=False)
        await asyncio.sleep(0)
        assert KI.key in sessions.started

        # Removido do YAML: para só ele e some da lista.
        monitor.set_channels([new, KI])
        await monitor.cycle(poll=False)
        assert sessions.stopped == [TW.key]
        assert {s.channel.key for s in monitor.snapshot()} == {new.key, KI.key}

        # Config de um canal gravando mudou (ex.: nome): grava de novo com a config nova.
        renamed = Channel(login="novo", display_name="Novo Nome")
        monitor.set_channels([renamed, KI])
        await monitor.cycle(poll=False)
        await asyncio.sleep(0)
        assert sessions.stopped == [TW.key, new.key] and sessions.started.count(new.key) == 2
        await monitor._stop_all()

    asyncio.run(main())


def test_crashed_session_waits_before_retry() -> None:
    status = ScriptedStatus([{TW.key: live(TW)}])
    sessions = Sessions(crash=True)
    mono = Clock()
    monitor = LiveMonitor([TW], {Platform.TWITCH: status}, sessions.factory, monotonic=mono)

    async def main() -> None:
        await monitor.cycle(poll=True)
        await asyncio.sleep(0)  # a sessão roda e quebra
        await monitor.cycle(poll=False)  # recolhe a sessão quebrada
        snap = monitor.snapshot()[0]
        assert snap.state is ChannelState.RETRYING and snap.error
        await monitor.cycle(poll=False)
        assert sessions.started == [TW.key]  # ainda esperando
        mono.t += 31
        await monitor.cycle(poll=False)
        await asyncio.sleep(0)
        assert sessions.started == [TW.key, TW.key]
        await monitor._stop_all()

    asyncio.run(main())


def test_missing_provider_is_an_error() -> None:
    with pytest.raises(ValueError):
        LiveMonitor([KI], {Platform.TWITCH: ScriptedStatus([{}])}, Sessions().factory)
