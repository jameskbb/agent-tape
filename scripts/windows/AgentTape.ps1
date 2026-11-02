<#
.SYNOPSIS
  Start, stop, or check AgentTape from Windows. AgentTape itself runs inside WSL.

.DESCRIPTION
  AgentTape is POSIX-only, so this launcher runs `agenttape serve` inside a WSL
  distribution and opens the web UI in your Windows browser. WSL forwards
  127.0.0.1, so the server stays bound to loopback and needs no API token.

  The server runs in a minimized console window titled "AgentTape server".
  Closing that window, or running with -Stop, stops the server. Runs that were
  in flight are reconciled as interrupted on the next start.

.EXAMPLE
  .\AgentTape.ps1                  # start if needed, then open the browser
  .\AgentTape.ps1 -Status          # report whether the server is up
  .\AgentTape.ps1 -Stop            # stop the server
  .\AgentTape.ps1 -Setup           # first run: install dependencies and build the UI
  .\AgentTape.ps1 -InstallShortcut # add "AgentTape" to the Start menu
#>
[CmdletBinding(DefaultParameterSetName = 'Start')]
param(
  [Parameter(ParameterSetName = 'Stop')][switch]$Stop,
  [Parameter(ParameterSetName = 'Status')][switch]$Status,
  [Parameter(ParameterSetName = 'Setup')][switch]$Setup,
  [Parameter(ParameterSetName = 'Shortcut')][switch]$InstallShortcut,
  [Parameter(ParameterSetName = 'Start')][switch]$NoBrowser,

  # WSL distribution to use. Empty means the WSL default distribution.
  [string]$Distro = $env:AGENTTAPE_WSL_DISTRO,
  # Path of the AgentTape checkout inside WSL.
  [string]$RepoPath = $(if ($env:AGENTTAPE_WSL_REPO) { $env:AGENTTAPE_WSL_REPO } else { '~/src/agent-tape' }),
  [int]$Port = $(if ($env:AGENTTAPE_PORT) { [int]$env:AGENTTAPE_PORT } else { 8787 }),
  [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'
$BaseUrl = "http://127.0.0.1:$Port"

# The Start menu shortcut runs this hidden, so surface failures in a dialog too.
trap {
  $message = $_.Exception.Message
  Write-Error $message -ErrorAction Continue
  if ([Environment]::UserInteractive) {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($message, 'AgentTape', 'OK', 'Error') | Out-Null
  }
  exit 1
}

function Get-WslPrefix {
  if ($Distro) { return @('-d', $Distro) }
  return @()
}

function Invoke-Wsl([string]$Command) {
  # Runs a command in a login shell so ~/.local/bin (uv) is on PATH.
  $wslArgs = (Get-WslPrefix) + @('--', 'bash', '-lc', $Command)
  & wsl.exe @wslArgs
  return $LASTEXITCODE
}

function Test-Ready {
  try {
    $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "$BaseUrl/api/v1/ready"
    return $response.StatusCode -eq 200
  } catch {
    return $false
  }
}

function Assert-Wsl {
  if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'wsl.exe was not found. AgentTape needs WSL on Windows; see docs/DEPLOYMENT.md.'
  }
}

function Assert-Checkout {
  $code = Invoke-Wsl "test -f $RepoPath/backend/pyproject.toml"
  if ($code -ne 0) {
    throw "No AgentTape checkout at '$RepoPath' inside WSL. Clone it there, or pass -RepoPath / set AGENTTAPE_WSL_REPO."
  }
  $code = Invoke-Wsl "test -f $RepoPath/backend/src/agenttape/web_dist/index.html"
  if ($code -ne 0) {
    throw "The web UI has not been built yet. Run: .\AgentTape.ps1 -Setup"
  }
}

switch ($PSCmdlet.ParameterSetName) {
  'Status' {
    if (Test-Ready) { Write-Host "AgentTape is running at $BaseUrl" }
    else { Write-Host "AgentTape is not running on port $Port"; exit 1 }
  }

  'Stop' {
    Assert-Wsl
    if (-not (Test-Ready)) { Write-Host 'AgentTape is not running.'; exit 0 }
    # SIGINT gives uvicorn, the worker, and the scheduler a graceful shutdown.
    Invoke-Wsl "pkill -INT -f 'agenttape serve --port $Port' || true" | Out-Null
    $deadline = (Get-Date).AddSeconds(20)
    while ((Test-Ready) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    if (Test-Ready) { Write-Warning 'AgentTape is still responding; close the "AgentTape server" window.'; exit 1 }
    Write-Host 'AgentTape stopped.'
  }

  'Setup' {
    Assert-Wsl
    Write-Host "Installing dependencies and building the UI in $RepoPath (this takes a few minutes)..."
    $code = Invoke-Wsl "cd $RepoPath && make setup && make build && cd backend && uv run agenttape init"
    if ($code -ne 0) { throw "Setup failed (exit $code). See docs/TROUBLESHOOTING.md." }
    Write-Host 'Setup complete. Start AgentTape with .\AgentTape.ps1'
  }

  'Shortcut' {
    $programs = [Environment]::GetFolderPath('Programs')
    $link = Join-Path $programs 'AgentTape.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = (Get-Command powershell.exe).Source
    $shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`""
    $shortcut.WorkingDirectory = $PSScriptRoot
    $shortcut.Description = 'Start AgentTape in WSL and open it in the browser'
    $shortcut.Save()
    Write-Host "Start menu shortcut created: $link"
    Write-Host 'Remove it by deleting that file.'
  }

  'Start' {
    Assert-Wsl
    if (-not (Test-Ready)) {
      Assert-Checkout
      $serve = "printf '\033]0;AgentTape server\007'; cd $RepoPath/backend && exec uv run agenttape serve --port $Port"
      $wslArgs = (Get-WslPrefix) + @('--', 'bash', '-lc', "`"$serve`"")
      Start-Process -FilePath 'wsl.exe' -ArgumentList $wslArgs -WindowStyle Minimized | Out-Null

      Write-Host "Starting AgentTape on $BaseUrl ..."
      $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
      while (-not (Test-Ready)) {
        if ((Get-Date) -gt $deadline) {
          throw "AgentTape did not become ready within $TimeoutSeconds s. Open the 'AgentTape server' window to see why."
        }
        Start-Sleep -Milliseconds 750
      }
    }
    Write-Host "AgentTape is running at $BaseUrl"
    if (-not $NoBrowser) { Start-Process $BaseUrl }
  }
}
