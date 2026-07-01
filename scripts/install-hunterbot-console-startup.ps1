param(
    [string]$ShortcutName = "Hunterbot Console Tunnel.lnk"
)

$ErrorActionPreference = "Stop"

$repoDir = Split-Path -Parent $PSScriptRoot
$tunnelScript = Join-Path $repoDir "scripts\start-hunterbot-console-tunnel.ps1"
if (-not (Test-Path -LiteralPath $tunnelScript)) {
    throw "Tunnel script not found: $tunnelScript"
}

$startupFolder = [Environment]::GetFolderPath("Startup")
if (-not $startupFolder) {
    throw "Startup folder not found."
}

$oldVbsPath = Join-Path $startupFolder "start-hunterbot-console-tunnel.vbs"
if (Test-Path -LiteralPath $oldVbsPath) {
    Remove-Item -LiteralPath $oldVbsPath -Force
}

$shortcutPath = Join-Path $startupFolder $ShortcutName
if (Test-Path -LiteralPath $shortcutPath) {
    Remove-Item -LiteralPath $shortcutPath -Force
}

$powershellExe = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
if (-not (Test-Path -LiteralPath $powershellExe)) {
    $powershellExe = (Get-Command powershell.exe -ErrorAction Stop).Source
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $powershellExe
$shortcut.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $tunnelScript + '"'
$shortcut.WorkingDirectory = $repoDir
$shortcut.Description = "Start Hunterbot console tunnel at logon"
$shortcut.WindowStyle = 7
$shortcut.Save()

Write-Host "Startup shortcut installed: $shortcutPath"
Write-Host "The console tunnel will start automatically after the next Windows logon."
