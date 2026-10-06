# Rebuild the desktop shortcut. Keep this file ASCII-only because Windows
# PowerShell 5.1 reads non-BOM scripts with the system ANSI code page.

$AppRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Target = Join-Path $AppRoot 'launch.bat'
$Arguments = ''
$IconPath = Join-Path $AppRoot 'app_icon.ico'
$ShortcutName = 'Aldebaran.lnk'

$Desktop = [Environment]::GetFolderPath('Desktop')
$LnkPath = Join-Path $Desktop $ShortcutName

# Remove any stale shortcut first so icon/path cache does not stick.
if (Test-Path $LnkPath) { Remove-Item $LnkPath -Force }

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($LnkPath)
$Shortcut.TargetPath = $Target
$Shortcut.Arguments = $Arguments
$Shortcut.WorkingDirectory = $AppRoot
$Shortcut.IconLocation = "$IconPath, 0"
$Shortcut.Description = 'Aldebaran'
$Shortcut.WindowStyle = 1
$Shortcut.Save()

Write-Host "Shortcut created: $LnkPath" -ForegroundColor Green
Write-Host "Target: $Target" -ForegroundColor Gray
