<#
.SYNOPSIS
  Sets up the ACE-Step 1.5 backend of ghost-label on Windows with AMD ROCm (gfx1151:
  Ryzen AI Max+ 395 / Radeon 8060S). Automates docs/backends/acestep/windows-rocm.md.

.DESCRIPTION
  Run it from this repo (cloned or unzipped), in a normal (non-admin) PowerShell:
      powershell -ExecutionPolicy Bypass -File scripts\setup-acestep-windows-rocm.ps1
  It installs Python 3.12, Git and ffmpeg with winget (unless -SkipWinget), creates a venv,
  installs AMD's ROCm PyTorch for gfx1151, checks that a GPU kernel actually runs, clones
  ACE-Step 1.5, installs its ROCm requirements without letting them replace torch, downloads the
  models (unless -SkipModels), writes engine.json, and records a short test song.

.PARAMETER Root
  Folder for the venv and the ACE-Step clone. Default: $env:USERPROFILE\music-engine
.PARAMETER Rocm
  10.0.0 (default: torch 2.13.0+rocm10.0.0) or 10.1.0 (torch 2.13.0+rocm10.1.0).
.PARAMETER Arch
  GPU architecture for the device extras. Default gfx1151.
#>
param(
  [string]$Root = (Join-Path $env:USERPROFILE "music-engine"),
  [ValidateSet("10.0.0", "10.1.0")][string]$Rocm = "10.0.0",
  [string]$Arch = "gfx1151",
  [switch]$SkipWinget,
  [switch]$SkipModels,
  [switch]$SkipTestSong
)

$ErrorActionPreference = "Stop"
$HostDir = Split-Path -Parent $PSScriptRoot
$Index = "https://stable.repo.amd.com/rocm/whl-next/"
# From AMD's "Install PyTorch for ROCm" page (Ryzen / gfx1151 / Windows / pip).
$Wheels = @{
  "10.0.0" = @("torch[device-$Arch]==2.13.0+rocm10.0.0", "torchvision[device-$Arch]==0.28.0+rocm10.0.0", "torchaudio==2.11.0.2+rocm10.0.0")
  "10.1.0" = @("torch[device-$Arch]==2.13.0+rocm10.1.0", "torchvision[device-$Arch]==0.28.0+rocm10.1.0", "torchaudio==2.11.0.2+rocm10.1.0")
}

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Run {
  # Simple function on purpose: everything after the program, including "-m", lands in $args.
  $exe = $args[0]
  $rest = @($args | Select-Object -Skip 1)
  & $exe @rest
  if ($LASTEXITCODE -ne 0) { throw "Failed ($LASTEXITCODE): $exe $($rest -join ' ')" }
}
function Refresh-Path {
  $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

if (-not (Test-Path (Join-Path $HostDir "engine.py"))) { throw "Run this script from the ghost-label repo (engine.py not found in $HostDir)." }
New-Item -ItemType Directory -Force $Root | Out-Null

if (-not $SkipWinget) {
  Step "Installing Python 3.12, Git and ffmpeg with winget (already-installed ones are skipped)"
  foreach ($id in @("Python.Python.3.12", "Git.Git", "Gyan.FFmpeg")) {
    & winget list -e --id $id *> $null
    if ($LASTEXITCODE -eq 0) { Write-Host "$id is installed"; continue }
    Run winget install -e --id $id --accept-source-agreements --accept-package-agreements
  }
  Refresh-Path
}

Step "Creating the Python 3.12 venv in $Root\venv"
$py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Run py -3.12 -m venv (Join-Path $Root "venv") }
Run $py -m pip install --upgrade pip

Step "Installing ROCm $Rocm PyTorch for $Arch from $Index"
$pipArgs = @("-m", "pip", "install", "--index-url", $Index) + $Wheels[$Rocm]
Run $py @pipArgs

Step "Checking that a GPU kernel runs"
Run $py -c "import torch; print('torch', torch.__version__, 'HIP', torch.version.hip); assert torch.cuda.is_available(), 'no ROCm GPU visible to torch'; p = torch.cuda.get_device_properties(0); print(p.name, getattr(p, 'gcnArchName', ''), round(p.total_memory / 2**30, 1), 'GiB'); print('kernel test:', (torch.zeros(8, device='cuda') + 1).sum().item())"

Step "Cloning ACE-Step 1.5"
$Ace = Join-Path $Root "ACE-Step-1.5"
if (Test-Path (Join-Path $Ace ".git")) { Run git -C $Ace pull --ff-only } else { Run git clone https://github.com/ace-step/ACE-Step-1.5 $Ace }

Step "Installing ACE-Step's ROCm requirements, keeping this torch"
$Constraints = Join-Path $Root "torch-constraints.txt"
& $py -m pip freeze | Select-String '^(torch|torchvision|torchaudio)==' | ForEach-Object { $_.Line } | Set-Content -Encoding ascii $Constraints
Get-Content $Constraints
Run $py -m pip install -r (Join-Path $Ace "requirements-rocm.txt") -c $Constraints
Run $py -m pip install -r (Join-Path $HostDir "requirements.txt") -c $Constraints
$after = & $py -c "import torch; print(torch.__version__)"
if ($after -notmatch "rocm") { throw "torch was replaced ($after). Re-run this script; never use 'uv run' or 'pip install -e .' in the ACE-Step clone." }
Write-Host "torch is still $after"

Step "Writing $HostDir\engine.json"
$config = [ordered]@{
  backend = "acestep"
  env     = [ordered]@{ ACESTEP_PROJECT_ROOT = $Ace; ACESTEP_ROCM_DTYPE = "float32" }
  options = [ordered]@{}
}
$config | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 (Join-Path $HostDir "engine.json")
Get-Content (Join-Path $HostDir "engine.json")

if (-not $SkipModels) {
  Step "Downloading the ACE-Step main model (~10 GB) into $Ace\checkpoints"
  Push-Location $Ace
  try { Run $py -m acestep.model_downloader --model main } finally { Pop-Location }
}

Step "Checking the protocol with the mock backend"
Run $py (Join-Path $HostDir "driver.py") --engine-arg=--backend --engine-arg=mock --max-seconds 4 --no-play

if (-not $SkipTestSong) {
  Step "Recording a 30-second test song with ACE-Step (the first run compiles GPU kernels; be patient)"
  Run $py (Join-Path $HostDir "driver.py") --steps 8 --max-seconds 30
}

Write-Host "`nDone. Record more with:" -ForegroundColor Green
Write-Host "  & `"$py`" `"$HostDir\driver.py`" --steps 16 --max-seconds 180"
