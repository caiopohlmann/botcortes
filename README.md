# cortesgta6

Monitora lives da **Twitch, Kick e YouTube**, detecta os momentos de hype pelo chat (e
confirma pelo áudio) e gera cortes verticais 9:16 com legenda automática, prontos para
TikTok, Reels e Facebook. **Nada é publicado automaticamente**: os clipes vão para uma
pasta de curadoria e você escolhe o que postar.

Roda **tudo num PC Windows** com GPU NVIDIA, ligado 24/7 como servidor, com um
**painel web** para acompanhar o PC, os canais e os cortes, e pausar quando quiser.

---

## ⚠️ Leia primeiro: estado atual

O código foi escrito num Mac, **sem instalar nada e sem rodar contra as plataformas de
verdade**. Ou seja, nunca rodou com FFmpeg, streamlink, Whisper, GPU nem com Twitch, Kick
ou YouTube reais. A primeira execução de verdade acontece no PC.

| O que já foi validado no Mac | O que só dá para validar no PC |
|---|---|
| 119 testes de lógica pura: detecção, legendas, qualidade da legenda, linha do tempo, parsers Twitch/Kick/YouTube, monitor (com pausas), banco do painel, telas e pastas do painel, pipeline de cortes (pastas, kit de edição, rerender), fila do "Pedir ao Claude" | `tests/test_media.py`, `tests/test_config.py`, `tests/test_panel_app.py` e `tests/test_claude_code.py` (precisam de numpy/pydantic/fastapi) |
| simulação acelerada da sessão (captura, live acabando no meio do hype, multistream, monitor com falha de rede, pausa/retomada) | `ruff` e `mypy` (nunca rodaram) |
| compilação, imports e nomes de todos os módulos; estrutura dos templates do painel | gravação, render, Whisper na GPU, NVENC; o painel no navegador |
| | formatos reais das páginas/APIs do Kick e do YouTube |

**O roteiro para terminar a configuração está em
[Roteiro de validação no PC](#roteiro-de-validação-no-pc)**, na ordem certa e com o que
esperar de cada passo. Se você é o Claude Code rodando no PC, comece por ali (e veja também
o `CLAUDE.md` na raiz).

## Status das etapas

| Etapa | O que entrega | Status |
|---|---|---|
| 1. MVP | canal(is) → detecção pelo chat → clipe 9:16 legendado em `data/clips/pending` | ✅ código pronto, falta validar no PC |
| 1b. Plataformas | Kick e YouTube; multistream (um clipe, chats somados) | ✅ código pronto, falta validar no PC |
| 1c. Servidor 24/7 | início automático, proteção de disco, limpeza, processos órfãos | ✅ código pronto, falta validar no PC |
| 2a. Painel de acompanhamento | saúde do PC, canais/plataformas, cortes gerados, pausas, assistir/baixar cortes | ✅ código pronto, falta validar no PC |
| 2b. Organização e ajustes | pastas por criador e por qualidade da legenda, navegação nas pastas no painel, kit de edição + `cortes rerender`, atalho "Pedir ao Claude" | ✅ código pronto, falta validar no PC |
| 2c. Curadoria | aprovar/descartar no painel, mover para `approved/` | próximo |
| 3. Escala | métricas agregadas, ajuste fino por canal | parcial (já roda N canais) |
| 4. VPS | monitor na VPS + Redis Streams; Docker | futuro |

---

## Como funciona

```
cortes run  (supervisor: sobe os três processos e reinicia quem cair)
├── cortes panel    ── painel web em http://127.0.0.1:8080 (acompanha, pausa, pastas, "Pedir ao Claude")
├── cortes monitor  ── leve, 24/7
│     status (a cada 30 s): Twitch = API Helix | Kick e YouTube = como navegador
│     canal ao vivo? → ChannelSession por canal:
│       ├─ streamlink → ffmpeg -c copy → segmentos .ts de 2 s (buffer circular ~3 min)
│       ├─ chat(s): Twitch IRC anônimo | Kick WebSocket | YouTube chat da página
│       │    → janelas de 5 s → z-score vs. média do canal + hype + diversidade
│       └─ detector (limiar + cooldown) → espera o pós-roll → copia o trecho → fila (SQLite)
└── cortes worker  ── pesado, GPU
      job → extrai áudio 16 kHz (1x) → nota final (chat + volume) → descarta se baixa
          → faster-whisper (palavras com timestamp e confiança) → legenda ASS estilo shorts
          → classifica a legenda: boa / revisar / ruim
          → FFmpeg: fundo desfocado + gameplay + facecam + legenda + crédito → NVENC
          → data/clips/pending/<criador>/<qualidade>/<id>_notaNN.mp4 + .txt
          → guarda o kit de edição (trecho + legenda editável) por 7 dias
```

**O bot assiste a live inteira, mas só guarda os cortes.** Da live, fica no disco apenas um
buffer dos últimos ~3 minutos, que vai sendo apagado e regravado sem parar. Quando o chat
explode, o bot copia só aquele trecho (~55 s) para a fila. Se a nota final for baixa, o trecho
é apagado; se virar corte, o trecho fica 7 dias no kit de edição (para ajustes) e depois é
apagado. A live completa nunca é salva.

- **Monitor**, **worker** e **painel** são processos separados: se o Whisper/driver travar,
  a gravação continua; se o painel cair, nada mais para. A fila entre eles é uma tabela
  SQLite (`data/cortes.db`), que sobrevive a reinícios.
- Cada processo publica o próprio estado no banco a cada 5 s (um "batimento"). O painel só lê
  esses batimentos: se um processo travar, o batimento envelhece e o painel mostra.
- Cada live é uma tarefa independente. Gravação e chat se reconectam sozinhos.
- Um **momento** é um pico do chat. Ele vira **job** na fila, e o worker transforma o job em
  **clipe** (ou descarta, se a nota final for baixa).

### Decisões de performance

- **Gravação sem reencode**: `-c copy` em segmentos de 2 s, quase zero de CPU por live.
- **Snapshot por concatenação binária** dos `.ts` (MPEG-TS concatena por bytes): o trecho
  do clipe sai do buffer em milissegundos e não é apagado pela limpeza.
- **Linha do tempo por PTS**: o horário de cada segmento vem do PTS do stream mais o menor
  atraso observado, não da hora de chegada (que oscila em rajadas). Isso alinha o vídeo
  com o horário das mensagens do chat.
- **Descarte antes da GPU**: a nota de áudio é calculada com numpy (~1 ms), e só momentos
  acima do mínimo vão para o Whisper e o render.
- **Um decode de áudio, dois usos**: o PCM 16 kHz alimenta a análise de volume e o Whisper,
  em memória.
- **Whisper residente**: o modelo é carregado uma vez por worker (`large-v3-turbo` fp16, ~3 GB
  de VRAM).
- **Render numa passada**: corte, layout, legenda e crédito num único filtergraph, com decode
  NVDEC, encode NVENC e blur do fundo em 1/8 da resolução (~64x mais barato).
- **Concorrência controlada**: semáforos separados para GPU (Whisper) e NVENC, com vários jobs
  em paralelo para sobrepor as etapas de CPU e GPU.
- **Falhas isoladas**: se uma plataforma cair, as outras seguem, e as gravações dela não são
  paradas por engano ("status desconhecido" é diferente de "offline").
- **Sem processos órfãos no Windows**: cada processo do bot fica num *Job Object*. Se ele
  morrer, o Windows encerra junto o streamlink e o FFmpeg que ele abriu.

---

## Setup no PC (Windows 10/11)

Requisitos: GPU NVIDIA com NVENC (GTX 16xx/RTX 20xx ou mais nova é o ideal) e pelo menos
6 GB de VRAM para o Whisper `large-v3-turbo`, 16 GB de RAM, SSD com 100+ GB livres e
internet cabeada. Ver [Capacidade](#capacidade-quantos-canais-e-por-quanto-tempo).

### 1. Programas

```powershell
winget install --id Git.Git -e
winget install --id astral-sh.uv -e
winget install --id Gyan.FFmpeg -e        # build "full": NVENC + libass
```

Feche e reabra o terminal para o PATH atualizar. Confira com `ffmpeg -version`, `uv --version`
e `nvidia-smi` (este vem com o driver NVIDIA; atualize o driver se o comando não existir).

### 2. Projeto

```powershell
git clone <url-do-repo> cortesgta6
cd cortesgta6
uv sync --extra cuda          # cria .venv com Python 3.12 + libs CUDA/cuDNN via pip
copy .env.example .env
notepad .env                  # credenciais (ver abaixo)
notepad config\channels.yaml  # seus canais
```

**Credenciais** (`.env`):

| Plataforma | Precisa? | Como conseguir |
|---|---|---|
| Twitch | sim, se houver canal da Twitch | https://dev.twitch.tv/console/apps → *Register Your Application* (Redirect URL `http://localhost`, categoria *Application Integration*) → Client ID + Client Secret |
| Twitch (sem anúncios) | opcional | `TWITCH_OAUTH_TOKEN` de uma conta Turbo/inscrita, ver [Anúncios](#anúncios-da-twitch) |
| Kick | não (opcional, só se o Cloudflare bloquear) | ver [Kick bloqueado](#kick-bloqueado-http-403--kick_blocked-no-log) |
| YouTube | não | — |

Use **contas criadas só para o bot** em todas as plataformas, nunca as pessoais. O bot só
precisa de chaves de app e tokens, nunca de usuário e senha.

### 3. Verificar o ambiente

```powershell
uv run cortes check
```

Confere FFmpeg, libass, um encode NVENC real, NVDEC, streamlink, GPU, CUDA do faster-whisper,
espaço em disco e consulta de verdade o status dos canais de cada plataforma. Tudo deve
aparecer como `OK`.

### 4. Testar o corte com um vídeo local (sem live)

```powershell
uv run cortes render-file C:\videos\vod.mp4 --start 120 --duration 55 --channel exemplo_streamer
# canal de outra plataforma: --channel kick-fulano  |  --channel youtube-fulano
```

O resultado vai para `data\clips\pending\<criador>\`. A primeira execução baixa o modelo do Whisper
(~1,6 GB) para o cache do HuggingFace.

### 5. Rodar

```powershell
uv run cortes run
```

Abra o painel em **http://127.0.0.1:8080** ([Painel](#painel)).

Para depurar, dá para rodar separado em terminais: `uv run cortes monitor`,
`uv run cortes worker` e `uv run cortes panel`. Para deixar como servidor, ver
[Rodando 24/7](#rodando-247-o-pc-como-servidor).

---

## Roteiro de validação no PC

Siga na ordem. Cada passo diz o que esperar. Se algo não bater, os arquivos envolvidos estão
em [Pontos não verificados](#pontos-não-verificados).

**0. Levantar o hardware.** As configurações dependem dele:

```powershell
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory
Get-PSDrive C
```

Meça também a velocidade da internet (download). Com isso, ajuste `worker.*`, a qualidade
de gravação e o número de canais conforme [Capacidade](#capacidade-quantos-canais-e-por-quanto-tempo).
Com menos de 6 GB de VRAM, use `transcription.model: medium` ou `compute_type: int8_float16`.

**1. Dependências e qualidade do código.** Nada disso rodou ainda:

```powershell
uv sync --extra cuda
uv run pytest            # todos devem passar (test_media e test_config rodam pela 1ª vez)
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

É provável que o `mypy` e o `ruff` apontem ajustes pequenos (tipos de bibliotecas de
terceiros, formatação). Corrija sem mudar comportamento e rode os testes de novo.

**2. `uv run cortes check`.** Tudo `OK`. Um canal "sem resposta" no status do Kick ou do
YouTube indica bloqueio ou mudança de formato da página (ver passo 7).

**3. Render local** (`render-file`, passo 4 do setup). Abra o vídeo e confira:
- layout 1080x1920: fundo desfocado, gameplay centralizado, crédito do canal embaixo;
- legenda palavra por palavra com destaque amarelo, na fonte certa (sem fonte trocada);
- áudio com volume normal;
- no log `clip_rendered`: `transcribe_s`, `render_s` e `realtime_factor` (quanto maior,
  melhor; abaixo de 1 o worker não acompanha o tempo real).

**4. Gravação de uma live da Twitch.** Ponha um canal que esteja ao vivo no
`channels.yaml`, use `logging.signal_log_interval_s: 5` e rode `uv run cortes monitor`.
Espere ver:
- `channel_live` → `session_started` → `recorder_started` → `chat_connected`;
- arquivos `seg_*.ts` aparecendo em `data\buffers\twitch-<canal>\<data>\run001\` a cada ~2 s,
  e os antigos sumindo depois de ~3 min;
- **`segments.csv` crescendo uma linha por segmento** (este é o ponto mais crítico: se ele só
  for escrito quando o FFmpeg fecha, nenhum clipe terá vídeo e aparecerá
  `moment_without_video`);
- `chat_signal` a cada 5 s com `rate` > 0.

**5. Forçar um clipe para testar o fluxo inteiro.** Baixe os limites temporariamente num
`config\settings.dev.yaml` ou direto no `settings.yaml` (e volte depois):

```yaml
detection: {warmup_s: 30}
detector: {trigger_score: 5, release_score: 2, z_trigger: 0.5, cooldown_s: 60}
scoring: {min_final_score: 0}
```

Rode `uv run cortes run` e espere `moment_detected` → `moment_queued` → `job_started` →
`moment_scored` → `clip_rendered`. Confira se o clipe mostra o trecho certo da live (se o
lance estiver deslocado, ajuste `clip.reaction_offset_s`).

**6. Proteção contra processos órfãos.** Com o monitor gravando, mate o processo `python`
do monitor no Gerenciador de Tarefas: os processos `ffmpeg` e o `python -m streamlink` dele
precisam sumir junto. Se `process_guard_unavailable` aparecer no log, a proteção não ativou
(ver `src/cortes/entrypoints/process_guard.py`).

**7. Kick e YouTube.** Adicione um canal ao vivo de cada plataforma:
- Kick: `chat_connected` com `chatroom=<id>`, e `chat_signal` com `rate` > 0. Se aparecer
  `kick_blocked` ou `Pusher recusou`, ver [Plataformas](#plataformas).
- YouTube: `channel_live` com o título da live, `chat_connected` com `video=<id>`, e
  `chat_signal` com `rate` > 0. Compare o ritmo do chat com o da página aberta no navegador.

**8. Multistream** (se algum streamer transmitir em duas plataformas): configure `extra_chats`
e confira `session_started` com `chats=2` ou mais.

**9. Teste de longa duração.** Deixe `cortes run` rodando algumas horas com os canais reais.
No Gerenciador de Tarefas, a memória do monitor e do worker deve ficar estável. Nos logs, não
deve haver repetição de `recorder_failed` nem de `chat_error`.

**10. Painel.** Com `cortes run` rodando, abra http://127.0.0.1:8080 e confira:
- "Tudo estável" no topo, e monitor e worker como "rodando" em *PC e processos*;
- os canais com o estado certo ("Gravando" para quem está ao vivo), a plataforma, o título
  da live e o chat em msg/s;
- CPU, RAM, disco, download e GPU com números, e os gráficos preenchendo (um ponto por minuto);
- **Pausar** um canal: em até ~2 s ele vai para "Pausado" e os `ffmpeg`/`streamlink` dele
  somem; **Retomar** volta a gravar. Teste também "Pausar tudo" e "Pausar cortes";
- um corte gerado aparece em *Últimos cortes* e toca no navegador;
- mate o worker no Gerenciador de Tarefas: em até 30 s o painel mostra "Problema", e depois
  o supervisor o reinicia (o painel mostra o reinício na última hora).

Se for usar pelo celular, siga [Acessar pelo celular](#acessar-pelo-celular).

**10b. Pastas e "Pedir ao Claude".**
- Em *Pastas de cortes*, navegue em `pending › <criador> › 1-legenda-boa` (ou revisar/ruim):
  os vídeos tocam ali, com a nota, a classificação da legenda e os motivos.
- Confira um `_LEIA-ME.txt` e o fim do `.txt` de um corte (relatório da legenda).
- Instale o Claude Code no PC, rode `claude` uma vez no terminal e faça login (o atalho usa
  esse login).
- Escolha um corte da pasta "revisar" e peça algo simples ("troca a palavra X por Y").
  Em 1-3 min o pedido deve ficar "Pronto", com a resposta do Claude; o vídeo é gerado de novo
  e, se a legenda ficou boa, o corte muda para `1-legenda-boa`.
- Teste "Abrir conversa no terminal do PC": abre uma janela com a mesma conversa.
- Se o Claude responder que não teve permissão para algo, veja `assistant` no README
  (permissões) e ajuste `adapters/assistant/claude_code.py` (`build_args`).

**11. Servidor.** Registre o início automático e reinicie o PC para testar
([Rodando 24/7](#rodando-247-o-pc-como-servidor)).

**12. Calibrar** com as primeiras lives reais ([Calibrando a detecção](#calibrando-a-detecção)).

### Pontos não verificados

Pontos que foram escritos com base em documentação ou conhecimento prévio, sem poder testar:

| Ponto | Sintoma se estiver errado | Onde ajustar |
|---|---|---|
| FFmpeg grava o `segments.csv` a cada segmento | `moment_without_video` em todo momento | `adapters/recording/streamlink_ffmpeg.py`; alternativa: `-segment_list_type m3u8 -segment_list_flags +live` e ler o `.m3u8` |
| Nome do modelo `large-v3-turbo` no faster-whisper | erro ao carregar o modelo | `transcription.model` (alternativas: `turbo`, `large-v3`) |
| DLLs CUDA/cuDNN via pip no Windows | `cublas64_12.dll`/`cudnn` não encontrada | `adapters/transcription/whisper.py` (`register_cuda_dlls`) |
| Fonte `Arial Black` disponível para o libass | legenda com outra fonte | `caption_style.font` ou `render.fonts_dir` |
| Campo `utilization.encoder` do `nvidia-smi` | `gpu_stats_failed` no log (nível debug) | `observability.py` |
| Job Object do Windows (ctypes) | `process_guard_unavailable`, ou órfãos no passo 6 | `entrypoints/process_guard.py` |
| Tolerância a anúncios da Twitch (120 s) | gravação reiniciando em loop durante anúncios | `recording.stall_timeout_s`, [Anúncios](#anúncios-da-twitch) |
| JSON público do Kick (`kick.com/api/v2/channels/<slug>`) | `kick_status_failed`, `kick_blocked` | `adapters/kick/parsing.py`, `adapters/kick/api.py` |
| API oficial do Kick (`api.kick.com/public/v1/channels`) | erro no status com credenciais definidas | `adapters/kick/parsing.py` (`parse_official_channels`) |
| Chave/URL do WebSocket (Pusher) do chat do Kick | `chat_error` com `Pusher recusou` | `kick.pusher_url` no `settings.yaml` |
| Página `/live` do YouTube (`ytInitialPlayerResponse`) | `youtube_status_failed` / `PageFormatError` | `adapters/youtube/parsing.py` (`parse_live_page`) |
| Chat interno do YouTube (`get_live_chat`) | `chat_error` no canal do YouTube | `adapters/youtube/parsing.py` e `chat.py` |
| streamlink abre `kick.com/<canal>` e `youtube.com/@canal/live` | `recorder_failed` / "No plugin can handle URL" | atualizar o streamlink; ou trocar `Channel.watch_url` em `domain/models.py` |
| Tarefa agendada reinicia o bot se ele sair com erro | bot não volta sozinho | `scripts/registrar-tarefa.ps1` |
| Templates do painel (Jinja2) renderizam | erro 500 no painel | `src/cortes/panel/templates/`, `tests/test_panel_app.py` |
| Contagem de processos filhos/RAM via `psutil` no Windows | números vazios em *PC e processos* | `observability.py` (`HostSampler`) |
| Confiança por palavra do faster-whisper (`word.probability`) | toda legenda cai em "boa" ou em "ruim" | `adapters/transcription/whisper.py`, limites em `caption_quality` |
| Limites da classificação boa/revisar/ruim | pasta "ruim" com legendas boas (ou o contrário) | `caption_quality` no `settings.yaml` |
| Flags do Claude Code em modo não interativo (`-p`, `--output-format json`, `--permission-mode acceptEdits`, `--allowedTools`, prompt pelo stdin) | pedido falha com "saída inesperada" ou sem permissão | `adapters/assistant/claude_code.py`; confira com `claude --help` |
| `claude` instalado como `.cmd` (npm) ou `.exe` (instalador) no Windows | "não encontrado no PATH" | `assistant.claude_path` |
| Abrir terminal com `claude --resume` pelo painel | o botão não abre janela | `ClaudeCodeRunner.open_terminal` |

---

## Rodando 24/7 (o PC como servidor)

O bot foi feito para ficar ligado direto. Para o PC funcionar como servidor:

**1. Energia: nunca suspender** (PowerShell como administrador):

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change monitor-timeout-ac 15    # desligar só a tela pode
```

**2. Iniciar sozinho com o Windows:**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\registrar-tarefa.ps1
Start-ScheduledTask -TaskName cortesgta6
```

A tarefa roda `uv run cortes run` no logon do usuário, numa janela de console, sem limite de
tempo. Para o PC voltar sozinho depois de queda de energia ou atualização do Windows:
- ative o login automático do usuário (`netplwiz`);
- na BIOS, ligue "Restore on AC Power Loss" / "Power On after power failure", se existir.

Comandos úteis: `Stop-ScheduledTask -TaskName cortesgta6` para parar e
`Unregister-ScheduledTask -TaskName cortesgta6` para remover.

**3. Windows Update:** defina o "horário ativo" (Configurações → Windows Update) para os
horários das lives, para ele não reiniciar o PC no meio delas.

**4. Rede:** cabo em vez de Wi-Fi. A gravação baixa vídeo sem parar.

**5. Manutenção.** O que o bot já faz sozinho e o que é com você:

| Item | Automático | Com você |
|---|---|---|
| Buffer das lives (`data/buffers`) | fica em ~3 min por canal, sempre apagado | — |
| Sobras de jobs com erro (`data/work`) | apagadas após 24 h (`worker.work_retention_h`) | — |
| Logs (`data/logs`) | rotacionam: 20 MB × 5 arquivos por processo | — |
| Banco (`data/cortes.db`) | cresce pouco (KB por clipe) | — |
| **Clipes (`data/clips/pending`)** | **não são apagados** | mover/apagar após a curadoria |
| Disco cheio | abaixo de `paths.min_free_gb` (20 GB) para de capturar clipes novos e registra `moment_skipped_disk_full`; avisa antes com `disk_space_low` | liberar espaço |
| Processo travou/caiu | o supervisor reinicia monitor e worker; a tarefa agendada reinicia o supervisor | — |
| Mudança no Kick/YouTube | não | ajustar o parser (ver [Pontos não verificados](#pontos-não-verificados)) |
| Histórico do painel (`metrics` no banco) | guarda 7 dias, apaga o resto (`panel.metrics_retention_days`) | — |

**Dá para usar o PC enquanto o bot roda?** Sim. A gravação quase não usa CPU. Jogar ao mesmo
tempo disputa a GPU com o Whisper e o NVENC: os clipes demoram mais, mas nada se perde (ficam
na fila).

---

## Painel

`cortes run` sobe o painel junto. Abra **http://127.0.0.1:8080** no navegador do PC.

| Seção | O que mostra |
|---|---|
| Topo | **Tudo estável / Atenção / Problema**, com o motivo (processo parado, reinícios ou erros na última hora, disco enchendo, RAM alta, GPU quente, gravação sem vídeo novo). Botões **Pausar tudo** e **Pausar cortes** |
| Números | cortes hoje, canais gravando, fila, há quanto tempo o PC está ligado |
| Canais | cada canal configurado: plataforma, estado (gravando, offline, pausado, outro jogo, erro, sem resposta), título/jogo/espectadores, chat (msg/s e o "normal" do canal), saúde da gravação, picos e cortes de hoje, chats extras. Botão **Pausar/Retomar** por canal |
| Cortes | hoje, 7 dias, total, legendas boas/revisar/ruins, picos detectados, descartados por nota baixa, erros; gráfico de cortes por hora; últimos cortes com player, classificação da legenda, download, descrição e link para a pasta |
| Pedir ao Claude | escolha um corte, escreva o que quer mudar e acompanhe o pedido (na fila, trabalhando, pronto) com a resposta do Claude |
| PC e processos | monitor e worker (rodando há quanto tempo, reinícios, avisos/erros, RAM), CPU, RAM, disco, download, streamlink/FFmpeg abertos, GPU, VRAM, temperatura, gráficos de 24 h, jobs em processamento e os últimos avisos/erros |

A aba **Pastas de cortes** é um explorador das pastas reais de `data/clips`: Cortes ›
pending › criador › qualidade. Em cada pasta de qualidade aparece o `_LEIA-ME.txt` explicando
o critério; cada corte mostra o vídeo, a nota, a classificação da legenda com os motivos e as
palavras duvidosas, e um campo para pedir ajuste ao Claude. Se você mover ou apagar arquivos
no Windows, o painel mostra o que está no disco.

**O que cada pausa faz** (fica salva no banco: sobrevive a reinícios):

| Botão | Efeito |
|---|---|
| Pausar (canal) | para de gravar e de ler o chat daquele canal em ~2 s. Retomar volta na hora, se ele ainda estiver ao vivo |
| Pausar tudo | o mesmo para todos os canais (economiza internet). Os cortes da fila continuam sendo gerados |
| Pausar cortes | o worker termina o corte atual e não pega o próximo; a gravação continua e a fila espera. Útil para liberar a GPU (jogar, por exemplo) |

### Acessar pelo celular

Por padrão o painel só abre no próprio PC. Para abrir no celular, na mesma rede de casa:

1. No `.env`: `PANEL_PASSWORD=<uma senha forte>`
2. Em `config/settings.yaml`: `panel.host: 0.0.0.0`
3. Libere a porta no Firewall do Windows (PowerShell como administrador):
   `New-NetFirewallRule -DisplayName cortesgta6-painel -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow -Profile Private`
4. No celular: `http://<IP do PC>:8080` (o IP aparece em `ipconfig`), usuário qualquer + a senha.

Sem senha, o painel se recusa a abrir na rede. **Não abra a porta no roteador** para acessar
de fora de casa. Para isso, use uma VPN como o Tailscale (grátis): instale no PC e no
celular e acesse pelo endereço do Tailscale, sem expor nada na internet.

### Quando passar para uma VPS

O painel mostra o que vai acabar primeiro: download perto do limite da internet, CPU ou
RAM altas o tempo todo, GPU sem dar conta (fila crescendo), ou o PC fazendo falta para outras
coisas. Como cada processo publica o estado no banco e o painel só lê, dá para mudar tudo de
máquina sem mudar o desenho:

- **Tudo numa VPS com GPU:** o mesmo projeto, com `CORTES_PROFILE=prod` e o painel em
  `0.0.0.0` com senha (ou atrás do Tailscale). Basta copiar `config/`, o `.env` e, se quiser o
  histórico, `data/cortes.db`.
- **Gravação numa VPS barata (sem GPU) e cortes no PC:** separa monitor e worker em máquinas
  diferentes. Exige trocar o SQLite por um banco em rede (Postgres) e a fila por Redis, e
  enviar os trechos de vídeo para o worker. Os pontos de troca já são interfaces
  (`domain/ports.py`). É a etapa 4.

---

## Capacidade: quantos canais e por quanto tempo

**Tempo:** sem limite de projeto. Tudo que cresce é limitado ou limpo (ver a tabela de
manutenção acima), exceto os clipes prontos, que esperam a sua curadoria. Na prática, o que
interrompe é externo: queda de energia ou internet, atualização do Windows, ou uma mudança
no site do Kick/YouTube.

**Canais configurados vs. ao vivo:** dá para configurar dezenas de canais. Checar quem está
ao vivo é barato (Twitch: 1 consulta para até 100 canais; Kick e YouTube: 1 consulta por
canal a cada 30-60 s). O que pesa são os canais **ao vivo ao mesmo tempo**, porque cada um é
gravado sem parar.

**O limite real costuma ser a internet** (download). Cada live gravada ocupa:

| Qualidade (`recording.quality` ou `quality` do canal) | Banda por live |
|---|---|
| 1080p60 | ~6-8 Mbps |
| 720p60 | ~3-4,5 Mbps |

Estimativa usando ~70% da internet, para sobrar folga:

| Internet | Lives simultâneas em 1080p60 | em 720p60 |
|---|---|---|
| 50 Mbps | ~4 | ~7 |
| 100 Mbps | ~8 | ~15 |
| 300 Mbps | ~20 (aí o limite passa a ser RAM, GPU e a sua curadoria) | ~20+ |

**720p quase não perde nitidez no vertical:** o gameplay 16:9 ocupa 1080 px de largura no
clipe, e um vídeo 720p tem 1280 px. Ou seja, ainda é *reduzido*, não ampliado. Só a facecam
(um recorte pequeno ampliado) fica um pouco mais suave. Sugestão: 720p60 como padrão e
`quality: "1080p60,1080p,best"` só nos canais com facecam.

**Demais recursos** (estimativas, confirme no passo 9 do roteiro):

| Recurso | Consumo | Observação |
|---|---|---|
| RAM | ~200 MB por live (streamlink + FFmpeg) + ~3 GB do worker | 16 GB dão para ~15+ lives |
| CPU | quase zero por live (sem reencode) | o render usa CPU nos filtros, só durante o clipe |
| VRAM | ~3 GB (Whisper) + ~0,5 GB por encode NVENC | fixo, não cresce com o número de canais |
| GPU (clipes/hora) | estimado em 20-40 s de processamento por clipe de 55 s | 100+ clipes/hora; um canal gera em média 2-10 momentos/hora (no máximo ~40 por causa do `cooldown_s`). Veja `realtime_factor` no log `clip_rendered` |
| Disco (escrita) | ~3,6 GB/hora por live em 1080p60 (o buffer é regravado; ocupa só ~180 MB) | SSD comum aguenta anos nesse ritmo |
| Disco (espaço) | ~40-100 MB por clipe pronto | 50 clipes/dia ≈ 3-5 GB/dia |
| Franquia de internet | ~3,6 GB/hora por live em 1080p60 (~1,8 GB em 720p) | banda larga fixa no Brasil normalmente é ilimitada |

**Regra prática:** comece com 5 canais, rode uma semana acompanhando o painel (download,
CPU, RAM, GPU, fila) e os logs (`clip_rendered` com `realtime_factor`), e vá aumentando. Para muitos canais em 1080p, a internet
acaba antes do PC.

---

## Plataformas

| | Status da live | Chat | Gravação | Credencial |
|---|---|---|---|---|
| Twitch | API oficial Helix | IRC anônimo (igual a quem não está logado) | streamlink | app da Twitch (grátis) |
| Kick | JSON da página do canal; API oficial como reserva | WebSocket da página (Pusher) | streamlink | nenhuma (opcional: app do Kick) |
| YouTube | página `youtube.com/@canal/live` | o chat da página (sem API, sem cota) | streamlink | nenhuma |

Kick e YouTube usam os mesmos endereços que o site usa para quem assiste, no ritmo de um
espectador. Não é API oficial: se o site mudar, o ajuste fica em
`src/cortes/adapters/kick/parsing.py` ou `src/cortes/adapters/youtube/parsing.py`.

```yaml
# config/channels.yaml
channels:
  - login: fulano              # twitch.tv/fulano
  - login: ciclano
    platform: kick             # kick.com/ciclano
  - login: Beltrano
    platform: youtube          # youtube.com/@Beltrano (ou o ID "UC...")
    quality: "720p60,720p,best"
```

O nome interno de cada canal é `plataforma-login` (ex.: `kick-ciclano`). Ele aparece nos
logs, nos nomes dos clipes e em `--channel` do `render-file`.

### Multistream (mesmo streamer em várias plataformas)

Liste o streamer **uma vez**, na plataforma que vai ser gravada, e some os outros chats:

```yaml
  - login: fulano
    platform: twitch
    extra_chats:
      - {platform: kick, login: fulano}
      - {platform: youtube, login: FulanoOficial}
```

Sai um clipe só, com crédito da plataforma gravada, e o pico é medido com o chat de todas
(sinal mais forte). Não liste o mesmo canal também como canal separado: o bot recusa a
configuração para não gerar clipe duplicado. Quando a plataforma gravada não está ao vivo,
nada é gravado, mesmo que as outras estejam.

### Anúncios da Twitch

Com `twitch.disable_ads: true` o bot pula os anúncios, e o buffer fica sem vídeo durante
eles (um lance no meio do anúncio não vira clipe). Para não ter anúncio nenhum, use uma conta
**com Turbo ou inscrita no canal**, criada só para o bot:

1. Entre na twitch.tv com essa conta no navegador.
2. DevTools (F12) → *Application* → *Cookies* → `https://www.twitch.tv` → copie o valor de `auth-token`.
3. No `.env`: `TWITCH_OAUTH_TOKEN=<auth-token>` (o bot passa para o streamlink sozinho).

Esse token dá acesso à conta: fica só no `.env`, que não vai para o git. Não compartilhe.

### Kick bloqueado (HTTP 403 / `kick_blocked` no log)

O Kick usa Cloudflare, que às vezes recusa acessos que não vêm de navegador. Nesse caso:

1. Crie um app em kick.com → *Configurações* → *Desenvolvedor* (precisa de 2FA na conta) e
   coloque `KICK_CLIENT_ID` e `KICK_CLIENT_SECRET` no `.env`. O status passa a usar a API
   oficial só para os canais bloqueados.
2. A API oficial não informa o id do chat. Abra `https://kick.com/api/v2/channels/<canal>`
   no navegador, procure `"chatroom":{"id":...}` e coloque em `chat_id` no `channels.yaml`.

Se o chat do Kick parar com `Pusher recusou`, o Kick trocou a chave do WebSocket: abra
kick.com com o DevTools (*Network* → *WS*), copie a URL do `ws-*.pusher.com` e cole em
`kick.pusher_url`.

---

## Configuração

| Arquivo | O que tem |
|---|---|
| `.env` | credenciais, senha do painel e perfil (`CORTES_PROFILE=prod` no PC). Nunca vai para o git |
| `config/settings.yaml` | tudo comentado: plataformas, gravação, detecção, pesos, pré/pós-roll, render, worker, publicação |
| `config/settings.<perfil>.yaml` | sobrescreve só as chaves que definir (`settings.dev.yaml` = Mac/CPU) |
| `config/channels.yaml` | canais, plataforma, idioma, qualidade, facecam e chats extras |

### Calibrando a detecção

Nas primeiras lives, defina `logging.signal_log_interval_s: 5`. O monitor passa a registrar
`chat_signal` (taxa, baseline, z, hype, score). Compare os valores nos momentos bons e
ruins e ajuste `detector.trigger_score`, `detector.z_trigger` e `scoring.min_final_score`.
Os padrões são um ponto de partida, não valores calibrados com dados reais. No YouTube, a
latência varia por live: se os clipes saírem deslocados, ajuste `youtube.chat_delay_s` e
`clip.reaction_offset_s`.

### Facecam

Abra um frame da live, meça a câmera e informe a região em **frações** do quadro:

```yaml
- login: fulano
  facecam: {x: 0.0, y: 0.70, w: 0.22, h: 0.30}   # canto inferior esquerdo
```

Com `facecam`, o layout fica: câmera no topo, gameplay embaixo e legenda abaixo.

### Como os cortes são organizados

Uma pasta por criador e, dentro dela, uma pasta por **qualidade da legenda**:

```
data/clips/pending/
├── Fulano/
│   ├── 1-legenda-boa/
│   │   ├── _LEIA-ME.txt                                   ← explica o critério da pasta
│   │   ├── 20261118-213045-twitch-fulano-a3f9_nota72.mp4
│   │   └── 20261118-213045-twitch-fulano-a3f9_nota72.txt
│   ├── 2-legenda-revisar/
│   │   └── 20261118-224510-kick-fulano-1b2c_nota64.mp4 (+ .txt)
│   └── 3-legenda-ruim/
│       └── 20261118-183344-twitch-fulano-5be0_nota69.mp4 (+ .txt)
└── Ciclano/
    └── 1-legenda-boa/
        └── 20261118-221502-youtube-ciclano-07bc_nota55.mp4 (+ .txt)
```

**Como a legenda é classificada.** O Whisper dá uma confiança (0-100%) para cada palavra.
Sem um gabarito não dá para saber se a legenda está certa, mas dá para saber quando o modelo
estava inseguro ou "alucinou":

| Pasta | Quando | O que fazer |
|---|---|---|
| `1-legenda-boa` | até 10% de palavras duvidosas (confiança < 50%) e média ≥ 80% | pode postar |
| `2-legenda-revisar` | 10-30% de palavras duvidosas, média 60-80%, idioma detectado errado e corrigido, trecho descartado como alucinação, ou corte sem fala | assista antes; corrija se precisar |
| `3-legenda-ruim` | mais de 30% duvidosas, média < 60%, mesma palavra 5x seguidas ou texto repetitivo (Whisper em loop) | refaça a legenda ou descarte |

Os limites são ajustáveis em `caption_quality` no `settings.yaml`; calibre olhando as pastas
nas primeiras semanas. Os nomes das pastas estão em `publish.quality_folders`.

**Avisos:** o motivo de cada corte aparece em três lugares: na pasta, no fim do `.txt`
(abaixo da linha "qualidade da legenda (não copiar daqui para baixo)", com as palavras
duvidosas) e no painel.

- **Pasta:** o `display_name` do canal (ou o login, se não tiver). O mesmo criador em duas
  plataformas com o mesmo nome cai na mesma pasta. Para escolher o nome, use `folder:` no
  `channels.yaml`. Caracteres proibidos no Windows (`: / ? * |` …) viram `_`.
- **Arquivo:** `data-hora do pico` + `plataforma-canal` + código curto + `notaNN` (0-100,
  chat 70% + subida do áudio 30%). Ordenar por nome = ordem cronológica.
- **`.txt`:** título (a primeira frase falada), créditos com o link do canal e hashtags,
  pronto para colar no post, e no fim o relatório da legenda. Pode editar o texto do post à
  vontade (a parte acima da linha do relatório).
- **A legenda fica gravada no vídeo**, mas pode ser corrigida: ver
  [Corrigir um corte](#corrigir-um-corte). O estilo dela (fonte, cor, palavras por linha) e o
  modelo da descrição valem para os próximos cortes (`caption_style`, `captions` e `publish`
  no `settings.yaml`).
- Picos descartados (nota baixa) não geram arquivo: só ficam no banco para as estatísticas.

### Corrigir um corte

Cada corte pronto guarda um **kit de edição** em `data/work/<id do corte>/` por 7 dias
(`worker.edit_kit_days`, ~45 MB por corte):

| Arquivo | O que é |
|---|---|
| `source.ts` | o trecho original da live (não editar) |
| `transcript.json` | a legenda, palavra por palavra, com início/fim (s) e confiança. **Editável** |
| `render.json` | onde o corte começa e quanto dura dentro do `source.ts`. **Editável** |

Duas formas de corrigir:

1. **Pelo painel, com o Claude** (mais fácil): escolha o corte em *Pedir ao Claude* (ou no
   próprio corte, em *Pastas de cortes*) e escreva o que quer: "troca 'jogou' por 'pulou'",
   "corta os 3 primeiros segundos", "muda o título para…". O painel chama o Claude Code deste
   PC em segundo plano (`claude -p`, sem abrir terminal). Ele edita o kit, roda o `rerender`
   e responde o que fez. Para acompanhar ou continuar a conversa, use "Abrir conversa no
   terminal do PC" (ou `claude --resume <id>`).
2. **À mão:** edite `transcript.json` e/ou `render.json` (as instruções estão no campo
   `_ajuda` de cada um) e rode:

   ```powershell
   uv run cortes rerender 20261118-213045-twitch-fulano-a3f9
   ```

O `rerender` gera o vídeo de novo (sem rodar o Whisper), refaz a classificação da legenda,
move o corte para a pasta certa se ela mudar e mantém o texto do post do `.txt`. Palavras
corrigidas devem ficar com `"probability": 1.0`, para contarem como conferidas.

**Permissões do Claude no atalho:** ele pode ler e editar arquivos do projeto e, no terminal,
só rodar `uv run cortes rerender`, `ffmpeg` e `ffprobe`; não acessa a internet. Qualquer outra
coisa é recusada automaticamente (ele explica na resposta). Ele roda com a sua conta do Claude
Code neste PC, então cada pedido consome o seu uso. Configuração em `assistant` no
`settings.yaml` (`enabled: false` desliga o atalho).

### Onde fica cada coisa

| Pasta | Conteúdo |
|---|---|
| `data/clips/pending/<criador>/<qualidade>/` | clipes prontos e a descrição sugerida (`.txt`), por criador e por qualidade da legenda (ver [Como os cortes são organizados](#como-os-cortes-são-organizados)) |
| `data/clips/approved/` | reservada para o painel de curadoria (etapa 2) |
| `data/buffers/` | buffer das lives em gravação (apagado sozinho) |
| `data/work/` | trechos esperando o worker e o kit de edição de cada corte (apagado após 7 dias) |
| `data/logs/` | `run.log`, `monitor.log`, `worker.log`, `panel.log` (JSON, uma linha por evento) |
| `data/cortes.db` | momentos, fila de jobs, clipes (com a classificação da legenda), pausas, batimentos, histórico do painel e pedidos ao Claude (SQLite) |

### Eventos de log principais

| Evento | Significado |
|---|---|
| `channel_live` / `channel_stopping` | canal entrou/saiu do ar (ou do jogo filtrado) |
| `recorder_started` / `recorder_failed` | gravação (re)iniciada / caiu e vai tentar de novo |
| `chat_connected` / `chat_error` | chat conectado / caiu e vai reconectar |
| `chat_signal` | sinal do chat (só com `signal_log_interval_s` > 0) |
| `moment_detected` → `moment_queued` | pico encontrado → trecho copiado e enviado à fila |
| `moment_without_video` | pico sem vídeo no buffer (live recém-iniciada, anúncio, ou problema na gravação) |
| `moment_scored` → `clip_rendered` | nota final calculada → clipe pronto (com tempos) |
| `moment_rejected` | nota final abaixo do mínimo: descartado antes da GPU |
| `job_failed` | erro no processamento (tenta 3 vezes) |
| `status_poll_failed` | uma plataforma não respondeu (as outras seguem) |
| `disk_space_low` / `moment_skipped_disk_full` | disco enchendo / cheio: liberar espaço |
| `process_guard_unavailable` | proteção contra processos órfãos não ativou |
| `channel_stopping` com `reason=pausado` | canal pausado pelo painel |
| `heartbeat_failed` | um processo não conseguiu publicar o estado para o painel (banco ocupado?) |
| `session_crashed` | a sessão de um canal caiu por erro; volta sozinha em 30 s, 60 s, 2 min… |
| `clip_rendered` com `caption=boa/revisar/ruim` | corte pronto e em qual pasta de qualidade caiu |
| `clip_rerendered` | corte gerado de novo pelo `rerender` (`moved=true` se mudou de pasta) |
| `assistant_requested` → `assistant_started` → `assistant_done` / `assistant_failed` | pedido ao Claude pelo painel |

---

## Desenvolvimento

```powershell
uv sync --extra cuda      # no Mac: uv sync (e CORTES_PROFILE=dev no .env)
uv run pytest
uv run ruff check . ; uv run ruff format --check .
uv run mypy
```

Testes cobrem detecção (scoring, detector, queda de chat), legendas, linha do tempo dos
segmentos, parsers de Twitch/Kick/YouTube, monitor de lives (falha parcial de plataforma),
configuração, layout/filtergraph e análise de áudio.

Arquitetura hexagonal: `domain/` não depende de nada externo; `application/` orquestra pelos
contratos de `domain/ports.py`; `adapters/` implementa cada contrato (Twitch, Kick, YouTube,
FFmpeg, Whisper, SQLite); `entrypoints/` monta tudo. Para uma plataforma nova: um provedor de
status (`StreamStatusProvider`), um chat (`ChatSource`) e um `case` em `entrypoints/monitor.py`.

## Estrutura

```
src/cortes/
├── domain/          # regras puras: scoring, detector, legendas, publicação, modelos, portas
├── application/     # casos de uso: LiveMonitor, ChannelSession, ClipPipeline (+ rerender),
│                    #   edit_kit (kit de edição), assistant (fila do "Pedir ao Claude")
├── adapters/
│   ├── twitch/      # Helix, chat IRC
│   ├── kick/        # status (página/API oficial), chat Pusher
│   ├── youtube/     # status pela página /live, chat da página
│   ├── recording/   # streamlink + ffmpeg (buffer circular), linha do tempo
│   ├── media/       # ffmpeg (render 9:16), ASS, análise de áudio
│   ├── transcription/  # faster-whisper
│   ├── storage/     # SQLite: metadados + fila de jobs; estado do painel (sqlite_status)
│   ├── assistant/   # Claude Code em modo não interativo (claude -p)
│   └── browser_http.py # cliente HTTP "como navegador" (Kick/YouTube)
├── panel/           # painel web: app.py (FastAPI), views.py (puro), files.py (pastas),
│                    #   templates/, static/
├── entrypoints/     # cli, monitor, worker, supervisor, status (batimentos), process_guard
├── config.py        # YAML + .env (pydantic)
└── observability.py # logs estruturados, uso de GPU
scripts/registrar-tarefa.ps1   # início automático no Windows
```

## Problemas comuns

| Sintoma | Causa / solução |
|---|---|
| `Could not load library cublas64_12.dll` / `cudnn*.dll` | Rode `uv sync --extra cuda`. Se persistir, baixe as libs CUDA 12 + cuDNN 9 do repositório *Purfview/whisper-standalone-win* e coloque numa pasta do PATH. |
| `Unknown encoder 'h264_nvenc'` ou erro ao abrir NVENC | FFmpeg sem NVENC (use o build full) ou driver desatualizado. |
| Legenda sem a fonte certa | A fonte de `caption_style.font` precisa existir no Windows, ou aponte `render.fonts_dir` para uma pasta com o `.ttf`. |
| `moment_without_video` em todo momento | Ver o passo 4 do roteiro (`segments.csv`). |
| Clipe começa ou termina deslocado | Ajuste `clip.reaction_offset_s` e `clip.pre_roll_s` (no YouTube, também `youtube.chat_delay_s`). |
| Muitos clipes ruins | Suba `detector.trigger_score` ou `scoring.min_final_score`. |
| Nenhum clipe | Rode com `signal_log_interval_s: 5` e veja se o `score` chega no `trigger_score`. |
| Gravação reinicia durante anúncios | Suba `recording.stall_timeout_s` ou use conta Turbo/sub ([Anúncios](#anúncios-da-twitch)). |
| `kick_blocked` / HTTP 403 no Kick | Ver [Kick bloqueado](#kick-bloqueado-http-403--kick_blocked-no-log). |
| Chat do Kick com `Pusher recusou` | Atualize `kick.pusher_url` (DevTools → Network → WS). |
| `youtube_status_failed` / `PageFormatError` | Tela de consentimento ou layout novo do YouTube: abra a página `/live` do canal no navegador e compare com `adapters/youtube/parsing.py`. |
| streamlink: `No plugin can handle URL` | Atualize o streamlink (`uv lock --upgrade-package streamlink` e `uv sync --extra cuda`). |
| `moment_skipped_disk_full` | Libere espaço em `data/clips` ou ajuste `paths.min_free_gb`. |
| Painel não abre | Veja se o processo `panel` está rodando (`data/logs/panel.log`) e se a porta 8080 está livre (mude `panel.port`). |
| Painel mostra "nunca iniciou" / "parado" | O processo não está publicando batimento: veja `monitor.log`/`worker.log`. Rodando sem `cortes run`, suba o processo que falta. |
| Pedido ao Claude fica "Não deu certo" com "não encontrado no PATH" | Instale o Claude Code no PC e faça login rodando `claude` uma vez; ou ajuste `assistant.claude_path`. |
| Pedido ao Claude falha com "saída inesperada" | Rode `claude -p "oi" --output-format json` no terminal para ver o erro (login expirado, plano sem acesso…). |
| `rerender` diz "kit de edição incompleto" | O kit foi apagado (mais de `worker.edit_kit_days`) ou o corte veio do `render-file`: não dá para refazer a legenda desse corte. |
| Painel abre no PC mas não no celular | `panel.host: 0.0.0.0`, `PANEL_PASSWORD` no `.env` e regra no Firewall (ver [Acessar pelo celular](#acessar-pelo-celular)). |

## Créditos e direitos

Todo clipe leva o endereço do canal na tela (`twitch.tv/…`, `kick.com/…` ou
`youtube.com/@…`), e a descrição sugerida sempre inclui o crédito (`.txt` ao lado do vídeo).
Republicar conteúdo de terceiros pode gerar denúncia de direitos autorais nas plataformas. O
ideal é ter autorização dos streamers.
