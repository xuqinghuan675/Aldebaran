# Aldebaran V3.0 — Nuitka Onefile Build Script
# Usage: powershell -ExecutionPolicy Bypass -File tools/build.ps1

param(
    [switch]$PreflightOnly,
    # onefile=单 exe（普通 PC）；standalone=文件夹（云桌面，不解压、秒开）
    [ValidateSet('onefile', 'standalone')]
    [string]$Mode = 'onefile'
)

$ErrorActionPreference = "Stop"
$APP_ROOT = "$PSScriptRoot\.."
$DIST = if ($env:ALDEBARAN_BUILD_DIR) { $env:ALDEBARAN_BUILD_DIR } else { Join-Path $APP_ROOT 'dist-build' }
$CLEAN_DATA = "$DIST\_clean_data"
$PY = "python"
$RequiredPySide = "6.10.2"
$RequiredQtPrefix = "6.10."
$RequiredAkShare = "1.18.60"
$LOCK_FILE = "$DIST\build.lock"
$PdfplumberAvailable = $false
$env:QT_API = "pyside6"

function Assert-FileExists {
    param(
        [string]$Path,
        [string]$Message
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Message`: $Path"
    }
}

function Assert-DirectoryExists {
    param(
        [string]$Path,
        [string]$Message
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Message`: $Path"
    }
}

function Assert-NoActiveBuildProcess {
    $distPattern = [regex]::Escape($DIST)
    $buildProcessPattern = 'nuitka|Backend\.scons|app\.build|app\.dist'
    $active = @(
        Get-CimInstance Win32_Process |
            Where-Object {
                $_.ProcessId -ne $PID -and
                $_.CommandLine -and
                $_.CommandLine -match $distPattern -and
                $_.CommandLine -match $buildProcessPattern
            }
    )
    if ($active.Count -gt 0) {
        $summary = ($active | ForEach-Object { "$($_.ProcessId):$($_.Name)" }) -join ', '
        throw "Refusing to build because another Aldebaran build process is active: $summary"
    }
}

$buildTitle = if ($Mode -eq 'standalone') { "Nuitka Standalone Build" } else { "Nuitka Onefile Build" }
Write-Host "============================================" -ForegroundColor Cyan
Write-Host " Aldebaran V3.0 — $buildTitle" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

# ---- check nuitka ----
Write-Host "[1/4] Checking Nuitka ..." -ForegroundColor Cyan
& $PY -m nuitka --version 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Nuitka not installed." -ForegroundColor Red
    Write-Host "Run: pip install nuitka ordered-set zstandard"
    exit 1
}

# ---- check Qt runtime ----
Write-Host "[2/4] Checking PySide runtime ..." -ForegroundColor Cyan
$runtimeCheck = @"
import sys
import importlib.metadata as importlib_metadata
from PySide6.QtCore import __version__ as PYSIDE_VERSION_STR, qVersion
required_pyside = '$RequiredPySide'
required_qt_prefix = '$RequiredQtPrefix'
required_akshare = '$RequiredAkShare'
akshare_version = importlib_metadata.version('akshare')
qt_version = qVersion()
print(f'PySide={PYSIDE_VERSION_STR} Qt={qt_version}')
print(f'AkShare={akshare_version}')
if PYSIDE_VERSION_STR != required_pyside or not qt_version.startswith(required_qt_prefix):
    print(
        'Refusing to build with this Qt runtime. '
        f'Expected PySide {required_pyside} and Qt {required_qt_prefix}x.'
    )
    sys.exit(2)
if akshare_version != required_akshare:
    print(
        'Refusing to build with this AkShare runtime. '
        f'Expected AkShare {required_akshare}.'
    )
    sys.exit(3)
"@
& $PY -c $runtimeCheck
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Refusing to build with this Qt runtime." -ForegroundColor Red
    Write-Host "Run: python -m pip install --upgrade --force-reinstall -r requirements.txt"
    exit 1
}

$pdfplumberCheck = "import pdfplumber; print('pdfplumber=' + str(getattr(pdfplumber, '__version__', 'unknown')))"
& $PY -c $pdfplumberCheck
if ($LASTEXITCODE -eq 0) {
    $PdfplumberAvailable = $true
} else {
    Write-Host "[WARN] pdfplumber missing; table extraction disabled." -ForegroundColor Yellow
}

New-Item -ItemType Directory -Path $DIST -Force | Out-Null
Assert-NoActiveBuildProcess

$buildLock = $null
try {
    try {
        $buildLock = [System.IO.File]::Open(
            $LOCK_FILE,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch {
        throw "Refusing to build because another Aldebaran build process holds build.lock: $LOCK_FILE"
    }

    Assert-FileExists "$APP_ROOT\app_icon.ico" "Missing required build resource"
    Assert-FileExists "$APP_ROOT\app_icon.jpg" "Missing required build resource"

    # ---- prepare clean data dir ----
    Write-Host "[3/4] Preparing data files ..." -ForegroundColor Cyan
    if (Test-Path -LiteralPath $CLEAN_DATA) {
        Remove-Item -LiteralPath $CLEAN_DATA -Recurse -Force -ErrorAction Stop
    }
    New-Item -ItemType Directory -Path $CLEAN_DATA -Force | Out-Null

    # seed data files (empty defaults, no user data)
    '{"sort_mode":"time"}' | Out-File -FilePath "$CLEAN_DATA\intel_settings.json" -Encoding utf8 -NoNewline
    '[]' | Out-File -FilePath "$CLEAN_DATA\intel_feed.json" -Encoding utf8 -NoNewline

    $intelConfigSrc = "$APP_ROOT\data\intel_config.json"
    $intelConfigDest = "$CLEAN_DATA\intel_config.json"
    $safeIntelConfig = @{}
    if (Test-Path -LiteralPath $intelConfigSrc -PathType Leaf) {
        try {
            $repoIntelConfig = Get-Content -LiteralPath $intelConfigSrc -Raw | ConvertFrom-Json
            if ($null -ne $repoIntelConfig.enable_overseas) {
                $safeIntelConfig['enable_overseas'] = [bool]$repoIntelConfig.enable_overseas
            }
        } catch {
            Write-Host "[WARN] repo intel_config.json parse failed; packaging empty safe config." -ForegroundColor Yellow
        }
    }
    ($safeIntelConfig | ConvertTo-Json -Compress) |
        Out-File -FilePath $intelConfigDest -Encoding utf8 -NoNewline

    if ($PreflightOnly) {
        Write-Host "[OK] Build preflight passed." -ForegroundColor Green
        exit 0
    }

    # ---- nuitka compile ----
    Write-Host "[4/4] Nuitka compiling (15-40 min) ..." -ForegroundColor Cyan
    $targetLabel = if ($Mode -eq 'standalone') { "Aldebaran.exe + dependencies folder" } else { "single Aldebaran.exe" }
    Write-Host "Target: $targetLabel" -ForegroundColor Gray

    # onefile=单 exe，固定解压目录复用避免每次重复解压；standalone=文件夹，不解压秒开
    $modeArgs = if ($Mode -eq 'standalone') {
        @("--standalone")
    } else {
        @(
            "--onefile",
            # 固定解压目录：首次解压后复用，避免每次启动重复解压几百 MB 到随机 %TEMP%
            # （随机 temp 在被杀/崩溃时残留堆积，导致后续启动解压慢/失败、双击无窗口）
            "--onefile-tempdir-spec={CACHE_DIR}\Aldebaran\v3_0_2"
        )
    }
    $optionalPackageArgs = @()
    if ($PdfplumberAvailable) {
        $optionalPackageArgs += @("--include-package=pdfplumber")
    }

    $nuitkaArgs = @($modeArgs) + @(
        "--enable-plugin=pyside6",
        "--include-module=PySide6.QtNetwork",
        "--enable-plugin=matplotlib",
        "--enable-plugin=anti-bloat",
        "--company-name=Aldebaran",
        "--product-name=Aldebaran V3.0",
        "--file-version=3.0.2.0",
        "--copyright=Aldebaran V3.0",
        "--windows-console-mode=disable",
        "--windows-icon-from-ico=$APP_ROOT\app_icon.ico",
        "--include-data-file=$APP_ROOT\app_icon.jpg=app_icon.jpg",
        "--include-data-dir=$CLEAN_DATA=data",
        "--include-package-data=akshare",
        "--include-package-data=certifi",
        "--include-package-data=charset_normalizer",
        "--include-package=talib",
        "--include-package=mootdx",
        "--include-package-data=mootdx",
        "--include-package-data=docx",
        "--nofollow-import-to=tools",
        "--nofollow-import-to=py_mini_racer",
        "--nofollow-import-to=torch",
        "--nofollow-import-to=torchaudio",
        "--nofollow-import-to=torchvision",
        "--nofollow-import-to=scipy",
        "--nofollow-import-to=sympy",
        "--nofollow-import-to=IPython",
        "--nofollow-import-to=polars",
        "--nofollow-import-to=numba",
        "--nofollow-import-to=llvmlite",
        "--include-windows-runtime-dlls=yes",
        "--no-deployment-flag=excluded-module-usage",
        # 用户显式要求本轮拉满：使用全部 32 个逻辑线程进行 C/SCons 编译。
        "--jobs=32",
        # standalone 需 Dependency Walker 分析 DLL 依赖，缓存缺失时会交互询问下载，
        # 非交互/脚本环境会卡死或退出。自动同意下载，永不卡提示。
        "--assume-yes-for-downloads",
        "--output-dir=$DIST",
        "--output-filename=Aldebaran.exe"
    ) + $optionalPackageArgs + @(
        "$APP_ROOT\app.py"
    )

    & $PY -m nuitka @nuitkaArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Nuitka build failed with exit code $LASTEXITCODE"
    }

    # ---- assemble release folder ----
    $relName = if ($Mode -eq 'standalone') { "Aldebaran_V3.0_云桌面版" } else { "Aldebaran_V3.0" }
    $REL = "$DIST\$relName"
    if (Test-Path -LiteralPath $REL) {
        Remove-Item -LiteralPath $REL -Recurse -Force -ErrorAction Stop
    }
    New-Item -ItemType Directory -Path $REL -Force | Out-Null

    if ($Mode -eq 'standalone') {
        # standalone 产物是 app.dist 文件夹（exe + 依赖），整体移入发布目录
        $distDir = "$DIST\app.dist"
        Assert-DirectoryExists $distDir "Missing build output"
        Assert-FileExists "$distDir\Aldebaran.exe" "Missing build output"
        Get-ChildItem -LiteralPath $distDir -Force |
            Move-Item -Destination $REL -Force -ErrorAction Stop
        Assert-FileExists "$REL\Aldebaran.exe" "Missing release output"
        Remove-Item -LiteralPath $distDir -Recurse -Force -ErrorAction Stop
    } else {
        $onefileExe = "$DIST\Aldebaran.exe"
        Assert-FileExists $onefileExe "Missing build output"
        Move-Item -LiteralPath $onefileExe -Destination $REL -Force -ErrorAction Stop
        Assert-FileExists "$REL\Aldebaran.exe" "Missing release output"
    }

    $zipPath = "$DIST\$relName.zip"
    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force -ErrorAction Stop
    }
    Compress-Archive -LiteralPath $REL -DestinationPath $zipPath -Force
    Assert-FileExists $zipPath "ZIP creation failed"
    if ((Get-Item -LiteralPath $zipPath).Length -le 0) {
        throw "ZIP creation failed: empty archive $zipPath"
    }

    Write-Host ""
    Write-Host "============================================" -ForegroundColor Green
    Write-Host " BUILD COMPLETE" -ForegroundColor Green
    Write-Host "============================================" -ForegroundColor Green
    Write-Host " DIR: $REL" -ForegroundColor White
    Write-Host " ZIP: $zipPath" -ForegroundColor White
    Write-Host ""
    Write-Host " Mode: $Mode" -ForegroundColor Gray
    if ($Mode -eq 'standalone') {
        Write-Host "   Aldebaran.exe + 依赖文件（文件夹版，不解压、秒开，适合云桌面）" -ForegroundColor Gray
    } else {
        Write-Host "   Aldebaran.exe   (onefile, self-contained)" -ForegroundColor Gray
    }
    Write-Host " First run creates AldebaranData/ next to exe." -ForegroundColor Gray
    Write-Host "============================================" -ForegroundColor Green
} finally {
    if ($buildLock -ne $null) {
        $buildLock.Dispose()
    }
    Remove-Item -LiteralPath $LOCK_FILE -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $CLEAN_DATA -Recurse -Force -ErrorAction SilentlyContinue
}
