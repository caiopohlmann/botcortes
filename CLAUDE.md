# CLAUDE.md: cortesgta6

Bot que monitora lives (Twitch, Kick, YouTube), detecta momentos de hype pelo chat e gera
cortes verticais 9:16 legendados, com um painel web para acompanhar e pausar. Roda 24/7
num PC Windows com GPU NVIDIA. O `README.md` é o manual completo; este arquivo é o resumo
para quem vai trabalhar no código.

## Contexto desta máquina (leia primeiro)

O código foi escrito em outra máquina (Mac), **sem instalar nada e sem rodar contra FFmpeg,
GPU, navegador ou as plataformas reais**. Esta é a primeira máquina onde ele roda de verdade.
A tarefa aqui é terminar a configuração e validar:

1. Siga o **"Roteiro de validação no PC"** do `README.md`, na ordem (passos 0 a 12). Cada
   passo diz o que esperar no log ou na tela.
2. Quando algo não bater, consulte **"Pontos não verificados"** (README): a tabela traz o
   sintoma esperado e o arquivo onde mexer.
3. Antes de mudar configurações de capacidade (canais, qualidade, `worker.*`), levante o
   hardware (passo 0) e use a seção "Capacidade" do README.
4. O que já foi validado no Mac: 119 testes de lógica pura, uma simulação de integração com
   relógio acelerado, compilação/imports/nomes e a estrutura dos templates. **Nunca
   rodaram:** `ruff`, `mypy`, `tests/test_media.py`, `tests/test_config.py`,
   `tests/test_panel_app.py` e `tests/test_claude_code.py`. Ajustes pequenos de tipo e de
   formatação são esperados.

Primeiros comandos:

```powershell
uv sync --extra cuda
uv run pytest
uv run ruff check . ; uv run ruff format --check . ; uv run mypy
copy .env.example .env      # e preencher
uv run cortes check
```

## Regras gerais do dono do projeto

1. **Revisar o fluxo inteiro antes de mexer.** Antes de qualquer tarefa, revisar o fluxo
   completo: quem chama, quem usa, onde a mudança pode afetar (processos, configuração,
   integrações, dados). Não fazer só a tarefa isolada.
2. **Banco de dados: SOMENTE LEITURA.** Só SELECT. Nunca rodar UPDATE, INSERT, DELETE, ALTER,
   migração ou qualquer escrita, em nenhum ambiente, nem com script, nem pedindo permissão.
   Quando precisar alterar dado, entregar o SQL no chat e o usuário roda manualmente. (O
   próprio bot grava no `data/cortes.db` enquanto funciona; a regra vale para comandos e
   scripts rodados à mão.)
3. **Revisar tudo antes de dizer que terminou.** Reler e conferir tudo o que foi feito
   (código alterado, efeitos colaterais, SQL entregue). Só então reportar.

## Mapa do sistema

```
cortes run (entrypoints/supervisor.py) — reinicia quem cair, publica reinícios
├── cortes monitor (entrypoints/monitor.py) — leve, 24/7
│   LiveMonitor (application/live_monitor.py): a cada 2 s lê pausas; a cada 30 s consulta status
│   └── ChannelSession por canal ao vivo (application/channel_session.py)
│       ├── StreamlinkFfmpegRecorder: streamlink → ffmpeg -c copy → .ts de 2 s (buffer ~3 min)
│       ├── chats: Twitch IRC | Kick Pusher | YouTube get_live_chat (+ extra_chats)
│       ├── ChatAnalyzer + MomentDetector (domain/scoring.py, domain/detector.py)
│       └── pico → espera o pós-roll → copia o trecho p/ data/work → moments + jobs (SQLite)
├── cortes worker (entrypoints/worker.py) — GPU
│   ClipPipeline (application/clip_pipeline.py): áudio → nota → Whisper → render NVENC
│   ├── classifica a legenda (domain/caption_quality.py): boa | revisar | ruim
│   ├── data/clips/pending/<criador>/<qualidade>/<id>_notaNN.mp4 + .txt (+ relatório)
│   └── kit de edição em data/work/<id>/ (application/edit_kit.py) → `cortes rerender <id>`
└── cortes panel (panel/app.py) — FastAPI em 127.0.0.1:8080
    ├── lê o banco e as pastas (panel/files.py); escreve só pausas e pedidos ao Claude
    └── fila "Pedir ao Claude" (application/assistant.py) → `claude -p` em segundo plano
        (adapters/assistant/claude_code.py), que edita o kit e roda `cortes rerender`
```

**Onde fica o estado:**
- `data/cortes.db` (SQLite WAL) é a única ponte entre os processos. Tabelas: `moments`, `jobs`
  (fila com lease), `clips` (com `caption_grade/score/notes`), `controls` (pausas do painel),
  `heartbeats` (estado de cada processo, JSON, a cada 5 s), `metrics` (amostras de 1 min,
  7 dias) e `assistant_requests` (pedidos ao Claude).
- **Schema:** ainda não existe banco em uso, então as tabelas mudaram direto no `CREATE
  TABLE`. Depois que o bot rodar no PC, qualquer mudança de schema precisa de migração, e
  pela regra 2 o SQL é entregue no chat para o dono rodar.
- Os batimentos são montados em `entrypoints/status.py` (formato que o painel lê) e
  `panel/views.py` transforma tudo no que a página mostra (funções puras, testadas).
- Configuração: `config/settings.yaml` (+ `settings.<perfil>.yaml`), `config/channels.yaml` e
  `.env`, carregados por `config.py` (pydantic, `extra="forbid"`: chave errada no YAML dá erro).

**Identidade dos canais:** `Channel.key` = `plataforma-login` (ex.: `kick-fulano`). Ela é usada
em logs, nomes de arquivo, no banco (`moments.channel`, `clips.channel`), nas pausas e no painel.

## Decisões já tomadas (e por quê)

- **Tudo num PC Windows, sem Docker/Redis:** pedido do dono. SQLite com lease substitui o
  Redis. Os pontos de troca para uma VPS são as interfaces de `domain/ports.py`.
- **Twitch pela API oficial (Helix); Kick e YouTube "como navegador":** sem chave de API, cota
  nem conta, usando os mesmos endpoints que a página usa, no ritmo de um espectador. Kick tem
  a API oficial como reserva, se o Cloudflare bloquear. Formatos isolados em
  `adapters/*/parsing.py`.
- **Status "desconhecido" ≠ offline:** se uma plataforma falha, nada é parado por engano.
- **Processos separados + Job Object no Windows** (`entrypoints/process_guard.py`): matar um
  processo mata os filhos dele (streamlink/FFmpeg), sem processos órfãos.
- **Anúncios da Twitch:** `stall_timeout_s` de 120 s, para não reiniciar no meio do anúncio;
  `TWITCH_OAUTH_TOKEN` opcional (conta Turbo/sub) elimina os anúncios.
- **Chat do YouTube chega em lotes:** usa o horário do servidor (`LagTracker`) e a avaliação
  roda `youtube.chat_delay_s` atrasada.
- **Queda do chat congela a baseline** (`ScoringParams.gap_s`): a volta do chat não vira
  pico falso.
- **Multistream:** `extra_chats` grava uma plataforma e soma os chats; o config recusa o mesmo
  canal duplicado.
- **Painel sem dependências de front-end:** Jinja2 + JS mínimo + SVG gerado no servidor
  (funciona sem internet). Na rede, só com `PANEL_PASSWORD`; POST de outra origem é bloqueado.
- **Disco:** abaixo de `paths.min_free_gb`, para de capturar momentos novos; `data/work` é
  limpo após 24 h (kits de edição: 7 dias). Os clipes prontos nunca são apagados (curadoria
  do dono). A live inteira nunca é salva: só o buffer de ~3 min e os trechos dos picos.
- **Qualidade da legenda pela confiança do Whisper** (sem gabarito): pastas boa/revisar/ruim,
  com o motivo na pasta (`_LEIA-ME.txt`), no fim do `.txt` e no painel.
- **"Pedir ao Claude" sem terminal:** `claude -p` com o pedido pelo stdin (nunca em argv: no
  Windows o `claude.cmd` interpretaria texto livre), permissões limitadas (`--allowedTools`
  só com `uv run cortes rerender`, ffmpeg e ffprobe; sem web) e `session_id` salvo para
  continuar no terminal (`claude --resume`). É um atalho para ajustar UM corte, não um chat
  geral com o projeto.

## Convenções do projeto

- Arquitetura hexagonal: `domain/` puro (sem I/O e sem dependências externas),
  `application/` orquestra via `domain/ports.py`, `adapters/` implementa, `entrypoints/`
  monta. Formatos de Kick/YouTube ficam isolados em `adapters/*/parsing.py` (funções puras,
  testadas em `tests/test_*_parsing.py`).
- Depois de qualquer mudança: `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .` e `uv run mypy` (strict).
- Textos, logs explicativos e comentários em português; nomes de eventos de log em inglês
  `snake_case` (o README lista os principais).
- Nada de publicação automática: os clipes só vão para `data/clips/pending/`.
- Todo clipe mantém o crédito do canal (na tela e no `.txt`).
- O painel só lê o banco, exceto `controls` (pausas) e `assistant_requests` (pedidos ao
  Claude, que o próprio painel executa). Estado novo para o painel: inclua no batimento do
  processo (`entrypoints/status.py`) e mostre via `panel/views.py`. Não faça o painel chamar
  os outros processos.
- Um corte é editado sempre pelo kit + `cortes rerender` (mantém banco, pasta e relatório
  coerentes). Não mexa nos `.mp4` prontos por fora disso.
- Painel aberto na rede (`panel.host` diferente de 127.0.0.1) sempre com `PANEL_PASSWORD`.
- Segredos (Twitch, Kick, `TWITCH_OAUTH_TOKEN`, `PANEL_PASSWORD`) só no `.env`. Nunca em
  `config/*.yaml`, que vai para o git. `data/` e `.env` estão no `.gitignore`.
- Use contas criadas só para o bot, nunca as pessoais do dono.
- Commit e push só quando o usuário pedir.

## Próximas etapas combinadas

- **2c. Curadoria no painel:** aprovar/descartar corte, mover para
  `data/clips/approved/<criador>/` (mesma estrutura por criador; a coluna `clips.status` já
  existe: `pending_review`, `approved`, `discarded`). Ao aprovar/descartar, apagar o kit de
  edição do corte.
- **Calibração** dos limites de detecção com lives reais (`logging.signal_log_interval_s: 5`).
- **4. VPS**, se o PC ficar pesado: ver "Quando passar para uma VPS" no README.
