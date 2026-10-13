# Swaps the app folder for the staged one after the app has quit, then starts it.
# Parameters: window pid, app folder, staged folder, folder for the previous build, result file, new version, exe name.
# ASCII only (Windows PowerShell reads a file without a byte-order mark as ANSI): the result is a code the app turns into words.
param(
    [int]$ProcessId, [string]$AppDir, [string]$NewDir, [string]$PreviousDir,
    [string]$Result, [string]$Version, [string]$Exe
)
$ErrorActionPreference = 'Stop'

function Done($ok, $code, $who = @()) {
    $json = @{ ok = $ok; code = $code; version = $Version; who = @($who | Where-Object { $_ }) } | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($Result, $json, (New-Object Text.UTF8Encoding($false)))
}
function Start-App {
    $env:PYINSTALLER_RESET_ENVIRONMENT = '1'  # a new program, not a child of the packaged app that started this script
    Start-Process -FilePath (Join-Path $AppDir $Exe) -WorkingDirectory $AppDir
}

$wait = if ($env:APPLY_WAIT_SECONDS) { [int]$env:APPLY_WAIT_SECONDS } else { 60 }  # the variable shortens it for tests
try { Wait-Process -Id $ProcessId -Timeout $wait -ErrorAction Stop } catch { }
if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) { Done $false 'still_running'; exit 1 }

$prevApp = Join-Path $PreviousDir (Split-Path $AppDir -Leaf)
if (Test-Path -LiteralPath $PreviousDir) { Remove-Item -LiteralPath $PreviousDir -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $PreviousDir | Out-Null

$deadline = (Get-Date).AddSeconds($wait)
while ($true) {  # a running djs.exe of an assistant holds files: wait for it, then name it
    try { Move-Item -LiteralPath $AppDir -Destination $prevApp; break }
    catch {
        if ((Get-Date) -gt $deadline) {
            $who = @()
            try {  # naming the program is a courtesy: never the reason the old app is not started again
                $who = Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($AppDir, 'OrdinalIgnoreCase') } |
                    ForEach-Object { $_.ProcessName } | Sort-Object -Unique
            } catch { }
            Done $false 'locked' $who
            Start-App
            exit 1
        }
        Start-Sleep -Seconds 1
    }
}

try { Copy-Item -LiteralPath $NewDir -Destination $AppDir -Recurse }
catch {
    Remove-Item -LiteralPath $AppDir -Recurse -Force -ErrorAction SilentlyContinue
    Move-Item -LiteralPath $prevApp -Destination $AppDir
    Done $false 'swap_failed'
    Start-App
    exit 1
}
Done $true 'ok'
Start-App
