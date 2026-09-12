# ============================================================================
#  a4agent packaging pipeline
#  Lite   : powershell -File build.ps1 [-Version 0.1.0] -Lite
#           -> 单个轻量安装包（只含启动器，引擎由首次向导在线下载）
#  Full   : powershell -File build.ps1 [-Version 0.1.0]
#           -> 三个离线安装包（cu12.4 / cu13.3 / vulkan，内置引擎 payload）
#  NOTE : keep this file ASCII-only (WinPS 5.1 reads BOM-less files as ANSI).
# ============================================================================
#Requires -Version 5.1
param(
    [string]$Version = "0.1.0",
    [switch]$Lite
)
$ErrorActionPreference = 'Stop'
$root   = Split-Path -Parent $PSScriptRoot          # C:\ProgramMine\a4agent
$dotnet = 'C:\ProgramMine\dotnet-sdk\dotnet.exe'

$iscc = @('C:\ProgramMine\tools\InnoSetup\ISCC.exe',
          'C:\Program Files (x86)\Inno Setup 6\ISCC.exe') |
         Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw 'ISCC.exe not found - install Inno Setup first' }

# -- 1/4 publish self-contained single-file launcher ------------------------
Write-Host '== 1/4 publish launcher =='
$publishDir = Join-Path $root 'publish'
# publish does not wipe the output dir; stale exes from old names would leak
# into every installer, so clean it first.
Remove-Item (Join-Path $publishDir '*') -Recurse -Force -ErrorAction SilentlyContinue
& $dotnet publish (Join-Path $root 'src\App\App.csproj') -c Release -r win-x64 --self-contained true `
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true `
    -p:EnableCompressionInSingleFile=true -o $publishDir
if ($LASTEXITCODE -ne 0) { throw 'publish failed' }
Get-ChildItem $publishDir | ForEach-Object { '  {0}  {1:N1} MB' -f $_.Name, ($_.Length/1MB) }

if ($Lite) {
    # -- 2/4 compile single lite installer --------------------------------------
    Write-Host '== 2/2 compile lite installer =='
    & $iscc (Join-Path $root 'installer\installer.iss') `
        "/DBackendName=Lite" `
        "/DVersion=$Version" `
        "/DPublishDir=$publishDir" `
        "/DAppId=a4agent-Lite"
    if ($LASTEXITCODE -ne 0) { throw 'ISCC failed: Lite' }
    Write-Host '== done =='
    Get-ChildItem (Join-Path $root 'installer\out') -Filter '*Lite*' |
        ForEach-Object { '  {0}  {1:N1} MB' -f $_.Name, ($_.Length/1MB) }
    return
}

# -- 2/4 prepare engine payloads --------------------------------------------
Write-Host '== 2/4 prepare engine payloads =='
$payloadRoot = Join-Path $root 'payloads'
$zips        = Join-Path $payloadRoot '_zips'

# cuda12.4 payload comes from the local proven build
$dest124 = Join-Path $payloadRoot 'cuda12.4'
New-Item -ItemType Directory -Force -Path $dest124 | Out-Null
Remove-Item "$dest124\*" -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item 'C:\ProgramMine\llama-cpp\*.dll'            $dest124 -Force
Copy-Item 'C:\ProgramMine\llama-cpp\llama-server.exe' $dest124 -Force
$s = (Get-ChildItem $dest124 -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ('  cuda12.4 payload: {0:N0} MB' -f $s)

function New-PayloadFromZips {
    # Merge one or more official zips into a pruned payload dir.
    param([string[]]$ZipNames, [string]$Name)
    $dest = Join-Path $payloadRoot $Name
    $tmp  = "$dest.extract"
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    foreach ($z in $ZipNames) {
        $zipPath = Join-Path $zips $z
        if (-not (Test-Path $zipPath)) { throw "missing $zipPath - download official build first" }
        $sub = Join-Path $tmp ([IO.Path]::GetFileNameWithoutExtension($z))
        Expand-Archive -LiteralPath $zipPath -DestinationPath $sub -Force
    }

    $server = Get-ChildItem $tmp -Recurse -Filter 'llama-server.exe' | Select-Object -First 1
    if (-not $server) { throw "($($ZipNames -join ', ')) no llama-server.exe found" }
    $bin = $server.DirectoryName

    Remove-Item $dest -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Copy-Item (Join-Path $bin '*.dll') $dest -Force
    Copy-Item $server.FullName         $dest -Force
    Get-ChildItem $bin -Directory | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $dest $_.Name) -Recurse -Force
    }
    # runtime add-on packs (cudart/cublas) may sit in a sibling extraction root
    Get-ChildItem $tmp -Recurse -Filter '*.dll' |
        Where-Object { -not (Test-Path (Join-Path $dest $_.Name)) } |
        ForEach-Object { Copy-Item $_.FullName $dest -Force }
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    $sz = (Get-ChildItem $dest -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
    Write-Host ('  {0} payload: {1:N0} MB' -f $Name, $sz)
}

New-PayloadFromZips -ZipNames @('cu133.main.zip', 'cuda13.3.zip') -Name 'cuda13.3'
New-PayloadFromZips -ZipNames @('vulkan.zip')                      -Name 'vulkan'

# drop backend-specific readme into each payload (templates live in .\readmes)
$readmeMap = @{
    'cuda12.4' = 'README-cuda12.4.txt'
    'cuda13.3' = 'README-cuda13.3.txt'
    'vulkan'   = 'README-vulkan.txt'
}
foreach ($k in $readmeMap.Keys) {
    $src = Join-Path $PSScriptRoot (Join-Path 'readmes' $readmeMap[$k])
    if (Test-Path $src) {
        Copy-Item $src (Join-Path (Join-Path $payloadRoot $k) 'README-FIRST.txt') -Force
        Write-Host ('  readme -> {0}' -f $readmeMap[$k])
    }
}

# -- 3/4 compile three installers --------------------------------------------
Write-Host '== 3/4 compile installers =='
$variants = @(
    @{ name = 'cu12.4'; src = $dest124;                                 id = 'a4agent-CUDA124' },
    @{ name = 'cu13.3'; src = (Join-Path $payloadRoot 'cuda13.3');      id = 'a4agent-CUDA133' },
    @{ name = 'vulkan'; src = (Join-Path $payloadRoot 'vulkan');        id = 'a4agent-VULKAN'  }
)
foreach ($v in $variants) {
    Write-Host ('-- {0} --' -f $v.name)
    & $iscc (Join-Path $root 'installer\installer.iss') `
        "/DBackendName=$($v.name)" `
        "/DVersion=$Version" `
        "/DPublishDir=$publishDir" `
        "/DEngineSource=$($v.src)" `
        "/DAppId=$($v.id)"
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed: $($v.name)" }
}

# -- 4/4 list artifacts -------------------------------------------------------
Write-Host '== 4/4 done =='
Get-ChildItem (Join-Path $root 'installer\out') |
    ForEach-Object { '  {0}  {1:N1} MB' -f $_.Name, ($_.Length/1MB) }
