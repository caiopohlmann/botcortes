<#
.SYNOPSIS
  Faz o bot iniciar sozinho quando o Windows liga (no logon do usuario) e voltar se cair.

.DESCRIPTION
  Cria a tarefa agendada "cortesgta6", que roda `uv run cortes run` na pasta do projeto:
    - dispara no logon do usuario atual (use o login automatico do Windows para o PC
      voltar sozinho depois de queda de energia ou atualizacao do Windows);
    - sem limite de tempo de execucao;
    - se o processo terminar com erro, o Agendador tenta de novo a cada 1 minuto.

  Roda na sessao do usuario (com janela de console), nao como servico: assim a GPU,
  o NVENC e o cache do Whisper funcionam igual ao terminal.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\registrar-tarefa.ps1
  Start-ScheduledTask -TaskName cortesgta6        # inicia agora
  Stop-ScheduledTask  -TaskName cortesgta6        # para
  Unregister-ScheduledTask -TaskName cortesgta6   # remove
#>
param(
    [string]$Pasta = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$Nome = "cortesgta6"
)

$ErrorActionPreference = "Stop"

$uv = (Get-Command uv -ErrorAction Stop).Source
if (-not (Test-Path (Join-Path $Pasta "pyproject.toml"))) {
    throw "pyproject.toml nao encontrado em $Pasta"
}

$acao = New-ScheduledTaskAction -Execute $uv -Argument "run cortes run" -WorkingDirectory $Pasta
$gatilho = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$config = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $Nome `
    -Action $acao `
    -Trigger $gatilho `
    -Settings $config `
    -Description "Bot de cortes de lives (cortesgta6): monitor + worker" `
    -Force | Out-Null

Write-Host "Tarefa '$Nome' registrada. Pasta: $Pasta"
Write-Host "Iniciar agora:  Start-ScheduledTask -TaskName $Nome"
