"""Análise do chat em janelas deslizantes.

Ideia central: cada canal tem seu próprio "normal". Um chat de 2 msg/s que vai para
20 msg/s é tão relevante quanto um de 50 que vai para 300. Por isso comparamos a taxa
atual com uma média móvel exponencial (EWMA) do próprio canal, via z-score.

Custo: O(mensagens na janela) por avaliação (1x/s). Com 5 s de janela e chats de até
~100 msg/s são ~500 itens por segundo por canal, desprezível.
"""

from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from cortes.domain.models import ChatMessage, ChatSignal

_STRIP_CHARS = ".,!?:;()[]{}\"'“”‘’"


@dataclass(frozen=True, slots=True)
class HypeRule:
    """Regex aplicada token a token (fullmatch, sem diferenciar maiúsculas)."""

    pattern: str
    weight: float


@dataclass(frozen=True, slots=True)
class ScoringParams:
    window_s: float = 5.0
    baseline_half_life_s: float = 300.0
    warmup_s: float = 120.0
    min_rate: float = 0.5  # abaixo disso (msg/s) o score é 0: chat morto não gera clipe
    min_std: float = 0.25  # piso do desvio-padrão, evita z gigante em chat quase parado
    z_min: float = 2.0  # z a partir do qual a taxa começa a pontuar
    z_max: float = 7.0  # z em que a componente de taxa satura em 1.0
    hype_saturation: float = 0.5  # hype médio por mensagem que já conta como 100%
    diversity_saturation: float = 0.6  # autores únicos / msgs que já conta como 100%
    weight_rate: float = 0.5
    weight_hype: float = 0.4
    weight_diversity: float = 0.1
    # Silêncio "impossível" (chat caiu, modo só-inscritos...): congela a baseline para a
    # volta ao normal não parecer um pico. Só vale quando a baseline previa pelo menos
    # gap_min_expected mensagens nesse intervalo (chat pequeno pode ficar quieto de verdade).
    gap_s: float = 30.0
    gap_min_expected: float = 15.0
    hype_tokens: dict[str, float] = field(default_factory=dict)
    hype_patterns: tuple[HypeRule, ...] = ()


def _clamp01(value: float) -> float:
    return 0.0 if value <= 0.0 else 1.0 if value >= 1.0 else value


class HypeMatcher:
    """Pontua uma mensagem pela presença de emotes/palavras de hype.

    Tokens exatos usam dict (O(1)); regex só roda para tokens que não bateram exato.
    Cada token distinto conta uma vez por mensagem ("KEKW KEKW KEKW" == "KEKW"), e o
    total por mensagem é limitado a 1.0.
    """

    def __init__(self, tokens: Mapping[str, float], rules: Sequence[HypeRule]) -> None:
        self._tokens = {k.casefold(): float(v) for k, v in tokens.items()}
        self._rules = [(re.compile(r.pattern, re.IGNORECASE), float(r.weight)) for r in rules]

    def score(self, text: str) -> float:
        total = 0.0
        seen: set[str] = set()
        for raw in text.split():
            token = raw.strip(_STRIP_CHARS).casefold()
            if not token or token in seen:
                continue
            seen.add(token)
            weight = self._tokens.get(token)
            if weight is None:
                weight = next((w for rx, w in self._rules if rx.fullmatch(token)), 0.0)
            total += weight
            if total >= 1.0:
                return 1.0
        return total


@dataclass(slots=True)
class _Entry:
    ts: float
    author: str
    hype: float


class ChatAnalyzer:
    """Mantém a janela de mensagens e a linha de base do canal.

    Uso: `add()` a cada mensagem; `evaluate(now)` em intervalo fixo (ex.: 1 s).
    Mensagens com ts > now (chegaram antes de serem "avaliáveis", ex.: avaliação com
    atraso para chats por polling) ficam guardadas e entram nas próximas avaliações.
    """

    def __init__(self, params: ScoringParams, matcher: HypeMatcher | None = None) -> None:
        self._p = params
        self._matcher = matcher or HypeMatcher(params.hype_tokens, params.hype_patterns)
        self._window: deque[_Entry] = deque()
        self._started_at: float | None = None
        self._mean: float | None = None
        self._var = 0.0
        self._last_update: float | None = None
        self._last_message_ts: float | None = None
        weights = params.weight_rate + params.weight_hype + params.weight_diversity
        if weights <= 0:
            raise ValueError("A soma dos pesos de scoring deve ser > 0")
        self._weight_sum = weights

    @property
    def baseline(self) -> float:
        return self._mean or 0.0

    def add(self, message: ChatMessage) -> None:
        self._window.append(_Entry(message.ts, message.author, self._matcher.score(message.text)))
        if self._last_message_ts is None or message.ts > self._last_message_ts:
            self._last_message_ts = message.ts

    def evaluate(self, now: float) -> ChatSignal:
        p = self._p
        if self._started_at is None:
            self._started_at = now
        elapsed = now - self._started_at

        cutoff = now - p.window_s
        window = self._window
        while window and window[0].ts < cutoff:
            window.popleft()
        # A deque está em ordem de chegada; com timestamps do servidor pode haver itens
        # levemente fora de ordem, então o filtro confere os dois limites.
        entries = [e for e in window if cutoff <= e.ts <= now]

        count = len(entries)
        rate = count / p.window_s
        # Antes de a primeira janela encher, a taxa é subestimada: não alimenta a baseline.
        z = self._update_baseline(rate, now, elapsed) if elapsed >= p.window_s else 0.0
        warm = elapsed >= p.warmup_s

        if count:
            hype = sum(e.hype for e in entries) / count
            diversity = len({e.author for e in entries}) / count
        else:
            hype = diversity = 0.0

        score = 0.0
        if rate >= p.min_rate and warm:
            rate_c = _clamp01((z - p.z_min) / (p.z_max - p.z_min))
            hype_c = _clamp01(hype / p.hype_saturation)
            div_c = _clamp01(diversity / p.diversity_saturation)
            # A taxa é condição necessária (multiplica tudo); hype e diversidade modulam.
            # Assim um chat normal cheio de KEKW não pontua sem um pico real de volume.
            mix = p.weight_rate + p.weight_hype * hype_c + p.weight_diversity * div_c
            score = 100.0 * rate_c * mix / self._weight_sum

        return ChatSignal(
            ts=now,
            rate=rate,
            baseline=self.baseline,
            z=z,
            hype=hype,
            diversity=diversity,
            score=score,
            warm=warm,
        )

    def _std(self) -> float:
        mean = max(self._mean or 0.0, 0.0)
        # Contagem de mensagens ~ Poisson: desvio da taxa numa janela W é sqrt(mean/W).
        poisson = math.sqrt(mean / self._p.window_s)
        return max(math.sqrt(self._var), poisson, self._p.min_std)

    def _update_baseline(self, rate: float, now: float, elapsed: float) -> float:
        p = self._p
        if self._mean is None or self._last_update is None:
            self._mean = rate
            self._var = 0.0
            self._last_update = now
            return 0.0

        dt = max(now - self._last_update, 0.0)
        self._last_update = now
        std = self._std()
        z = (rate - self._mean) / std

        if self._in_gap(now):
            return z  # baseline congelada enquanto o chat está "fora do ar"

        alpha = 1.0 - 0.5 ** (dt / p.baseline_half_life_s)
        if elapsed < p.warmup_s:
            # Aquecimento: média acumulada (converge rápido para o nível real do canal).
            alpha = max(alpha, dt / max(elapsed, dt, 1e-9))
            sample = rate
        else:
            # Winsorização: um pico não "contamina" a baseline e mascara o próximo pico.
            sample = min(rate, self._mean + 2.0 * std)

        diff = sample - self._mean
        self._mean += alpha * diff
        self._var = (1.0 - alpha) * (self._var + alpha * diff * diff)
        return z

    def _in_gap(self, now: float) -> bool:
        p = self._p
        if p.gap_s <= 0 or self._mean is None or self._started_at is None:
            return False
        last = self._last_message_ts if self._last_message_ts is not None else self._started_at
        silent_for = now - max(last, self._started_at)
        return silent_for >= p.gap_s and self._mean * p.gap_s >= p.gap_min_expected


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    chat_weight: float = 0.7
    audio_weight: float = 0.3
    min_final_score: float = 40.0


def combine_scores(chat_score: float, audio_score: float, weights: ScoreWeights) -> float:
    """Nota final 0..100. chat_score em 0..100, audio_score em 0..1."""
    total = weights.chat_weight + weights.audio_weight
    if total <= 0:
        raise ValueError("A soma dos pesos chat/áudio deve ser > 0")
    chat = _clamp01(chat_score / 100.0)
    audio = _clamp01(audio_score)
    return 100.0 * (weights.chat_weight * chat + weights.audio_weight * audio) / total
