<#
.SYNOPSIS
  Install (or remove) NearShare for the current user from this folder.

.DESCRIPTION
  Copies the bundle to %LOCALAPPDATA%\Programs\NearShare and adds a Start
  menu entry and an Explorer "Send to > NearShare" entry. Run from an
  elevated PowerShell as well to also let NearShare through Windows
  Firewall on private networks; otherwise Windows asks the first time
  NearShare starts listening.

    powershell -ExecutionPolicy Bypass -File install.ps1
    powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
#>
param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'

$dest = Join-Path $env:LOCALAPPDATA 'Programs\NearShare'
$windows = Join-Path $env:APPDATA 'Microsoft\Windows'
$startLnk = Join-Path $windows 'Start Menu\Programs\NearShare.lnk'
$sendLnk = Join-Path $windows 'SendTo\NearShare.lnk'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if ($Uninstall) {
    Get-Process python, pythonw -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "$dest\*" } | Stop-Process -Force
    Remove-Item $startLnk, $sendLnk -ErrorAction SilentlyContinue
    if ($admin) { Remove-NetFirewallRule -Group 'NearShare' -ErrorAction SilentlyContinue }
    Remove-Item $dest -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host 'NearShare removed.'
    return
}

$src = $PSScriptRoot
if ((Resolve-Path $src).Path -ne $dest) {
    Get-Process python, pythonw -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like "$dest\*" } | Stop-Process -Force
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    New-Item -ItemType Directory -Force (Split-Path $dest) | Out-Null
    Copy-Item $src $dest -Recurse
}

$shell = New-Object -ComObject WScript.Shell
foreach ($link in @(@($startLnk, '', 'NearShare: Quick Share and AirDrop'),
                    @($sendLnk, '--send', 'Send with NearShare'))) {
    $s = $shell.CreateShortcut($link[0])
    $s.TargetPath = Join-Path $dest 'NearShare.exe'
    $s.Arguments = $link[1]
    $s.WorkingDirectory = $dest
    $s.IconLocation = (Join-Path $dest 'nearshare.ico') + ',0'
    $s.Description = $link[2]
    $s.Save()
}

if ($admin) {
    Remove-NetFirewallRule -Group 'NearShare' -ErrorAction SilentlyContinue
    foreach ($exe in 'python.exe', 'pythonw.exe') {
        New-NetFirewallRule -DisplayName "NearShare ($exe)" -Group 'NearShare' `
            -Direction Inbound -Action Allow -Profile Private `
            -Program (Join-Path $dest "bin\$exe") | Out-Null
    }
    Write-Host 'Firewall: NearShare allowed on private networks.'
}
Write-Host "NearShare installed to $dest. Start it from the Start menu."
