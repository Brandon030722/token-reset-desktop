[CmdletBinding()]
param(
    [ValidateSet("win-x64", "win-arm64")]
    [string]$Runtime = "win-x64",
    [string]$Python = "python",
    [switch]$SelfContained,
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") { throw "Build this package on Windows; PyInstaller does not cross-compile." }
$projectRoot = Split-Path $PSScriptRoot -Parent
$work = Join-Path $projectRoot "build\windows\$Runtime"
$output = Join-Path $projectRoot "artifacts\TiboMonitor-$Runtime"
$project = Join-Path $projectRoot "desktop\windows\TiboMonitor.Windows.csproj"

function Invoke-Checked {
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command failed with exit code $LASTEXITCODE" }
}

foreach ($tool in @("dotnet", $Python)) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "Install developer dependency before building: $tool" }
}

Push-Location $projectRoot
try {
    $pythonArch = (& $Python -c "import platform; print(platform.machine().lower())").Trim()
    if ($LASTEXITCODE -ne 0) { throw "Could not detect Python architecture." }
    if (($Runtime -eq "win-x64" -and $pythonArch -notin @("amd64", "x86_64")) -or
        ($Runtime -eq "win-arm64" -and $pythonArch -notin @("arm64", "aarch64"))) {
        throw "Use a Python interpreter matching $Runtime (found $pythonArch)."
    }
    if (-not $SkipFrontend) {
        Invoke-Checked "npm" @("ci")
        Invoke-Checked "npm" @("run", "build")
    }
    if (-not (Test-Path "dist\index.html")) { throw "Build the frontend before packaging." }
    New-Item -ItemType Directory -Force -Path $work | Out-Null
    Invoke-Checked $Python @("-m", "venv", (Join-Path $work "venv"))
    $buildPython = Join-Path $work "venv\Scripts\python.exe"
    Invoke-Checked $buildPython @("-m", "pip", "install", "pyinstaller==6.16.0")
    Invoke-Checked $buildPython @("-m", "unittest", "discover", "-s", "tests", "-p", "test_monitor.py")
    Invoke-Checked $buildPython @(
        "-m", "PyInstaller", "--clean", "--noconfirm", "--onedir", "--console",
        "--name", "TiboMonitorHelper", "--paths", $projectRoot,
        "--exclude-module", "tkinter", "--exclude-module", "webbrowser",
        "--distpath", (Join-Path $work "helper-dist"),
        "--workpath", (Join-Path $work "pyinstaller"),
        "--specpath", $work, "desktop\windows\monitor_entry.py"
    )
    # Stage a fresh package: never include developer state or stale frontend data.
    if (Test-Path $output) { Remove-Item -Recurse -Force $output }
    New-Item -ItemType Directory -Force -Path $output | Out-Null
    $selfContainedValue = if ($SelfContained) { "true" } else { "false" }
    Invoke-Checked "dotnet" @(
        "publish", $project, "-c", "Release", "-r", $Runtime,
        "--self-contained", $selfContainedValue, "-o", $output,
        "-p:PublishSingleFile=false", "-p:PublishReadyToRun=false"
    )
    Copy-Item -Recurse (Join-Path $work "helper-dist\TiboMonitorHelper") (Join-Path $output "monitor")
    New-Item -ItemType Directory -Force -Path (Join-Path $output "site") | Out-Null
    Copy-Item "dist\index.html" (Join-Path $output "site\index.html")
    Copy-Item "dist\favicon.svg" (Join-Path $output "site\favicon.svg")
    Copy-Item -Recurse "dist\assets" (Join-Path $output "site\assets")
    Copy-Item "desktop\windows\README.md" (Join-Path $output "README-Windows.md")
    Copy-Item "monitor.config.example.json" (Join-Path $output "monitor.config.example.json")
    # Help exits before polling and does not write user monitoring state.
    Invoke-Checked (Join-Path $output "monitor\TiboMonitorHelper.exe") @("--once", "--help")
    $archive = "$output.zip"
    if (Test-Path $archive) { Remove-Item -Force $archive }
    Compress-Archive -Path $output -DestinationPath $archive -CompressionLevel Optimal
    Write-Output "Built $archive"
    Write-Output "Windows UI and live network behavior still require a target-machine smoke test."
}
finally { Pop-Location }
