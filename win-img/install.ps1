# install.ps1: the software your Windows image carries, installed inside the image build, after
# boxd's own setup and before the image is generalized (sysprep). It runs once, as the built-in
# Administrator, from the answer ISO's custom\ folder; fetch.sh put the files in custom\files.
#
# Yours to edit (README.md, "Your software layer"). The rules:
# - Install for all users: under C:\Program Files, on the machine Path. Never into the profile:
#   sysprep removes the Administrator's profile, and every machine starts without it.
# - No network: everything comes from custom\files.
# - Don't run sysprep, shut down or reboot: boxd's setup does that after this script.
# - Stop on an error (throw): a half-installed image is worse than a failed build.
# - Git for Windows in C:\Program Files\Git and the GitHub CLI in C:\Program Files\GitHub CLI, if
#   you want boxd's GitHub wiring: the boxd agent makes itself git's credential helper for
#   github.com and puts its gh wrapper ahead of gh, both only when they are there.
#
# boxd's default: Git for Windows, Claude Code, Codex, Bun and the GitHub CLI.
$ErrorActionPreference = 'Stop'
$Files = Join-Path $PSScriptRoot 'files'

function Add-MachinePath([string]$Dir) {
    $k = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SYSTEM\CurrentControlSet\Control\Session Manager\Environment', $true)
    try {
        $path = $k.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if (-not $path) { throw 'the machine Path could not be read' }
        if (($path -split ';') -notcontains $Dir) {
            $k.SetValue('Path', ($path.TrimEnd(';') + ';' + $Dir), [Microsoft.Win32.RegistryValueKind]::ExpandString)
        }
    } finally {
        $k.Close()
    }
}

function Need([string]$Name) {
    $p = Join-Path $Files $Name
    if (-not (Test-Path $p)) { throw "custom\files\$Name is missing (run fetch.sh before the build)" }
    return $p
}

# tar.exe is Windows' own (bsdtar): it reads .zip and .tar.gz.
$tar = Join-Path $env:WINDIR 'System32\tar.exe'
$unpack = Join-Path $env:TEMP 'win-img'
Remove-Item -Path $unpack -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $unpack | Out-Null

# Git for Windows: Inno Setup, silent, for all users, its cmd folder on the machine Path.
$git = Start-Process -FilePath (Need 'git-installer.exe') -Wait -PassThru -ArgumentList @(
    '/VERYSILENT', '/NORESTART', '/NOCANCEL', '/SP-', '/SUPPRESSMSGBOXES', '/ALLUSERS', '/o:PathOption=Cmd')
if ($git.ExitCode -ne 0) { throw "Git for Windows' installer exited $($git.ExitCode)" }

# Claude Code: its native binary. Its background updater would install a per-user copy, which the
# next copy of the machine loses: off for every user (`claude update` still works).
$claudeDir = Join-Path $env:ProgramFiles 'Anthropic\Claude Code'
New-Item -ItemType Directory -Force -Path $claudeDir | Out-Null
Copy-Item -Path (Need 'claude.exe') -Destination (Join-Path $claudeDir 'claude.exe') -Force
Add-MachinePath $claudeDir
[Environment]::SetEnvironmentVariable('DISABLE_AUTOUPDATER', '1', 'Machine')

# Codex: the complete package (codex-package.json, bin\codex.exe and its helpers).
$codexDir = Join-Path $env:ProgramFiles 'OpenAI\Codex'
New-Item -ItemType Directory -Force -Path $codexDir | Out-Null
& $tar -xzf (Need 'codex-package.tar.gz') -C $codexDir
if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $codexDir 'bin\codex.exe'))) { throw 'the Codex package did not unpack' }
Add-MachinePath (Join-Path $codexDir 'bin')

# Bun: bun.exe, and bunx.exe beside it (a hard link: Bun runs as `bun x` when it is named bunx).
& $tar -xf (Need 'bun.zip') -C $unpack
$bunExe = Get-ChildItem -Path $unpack -Recurse -Filter 'bun.exe' | Select-Object -First 1
if (-not $bunExe) { throw 'bun.zip holds no bun.exe' }
$bunDir = Join-Path $env:ProgramFiles 'Bun'
New-Item -ItemType Directory -Force -Path $bunDir | Out-Null
Copy-Item -Path $bunExe.FullName -Destination (Join-Path $bunDir 'bun.exe') -Force
Remove-Item -Path (Join-Path $bunDir 'bunx.exe') -Force -ErrorAction SilentlyContinue
New-Item -ItemType HardLink -Path (Join-Path $bunDir 'bunx.exe') -Target (Join-Path $bunDir 'bun.exe') | Out-Null
Add-MachinePath $bunDir

# The GitHub CLI: gh.exe where its MSI puts it. boxd's agent puts its gh wrapper (the real gh with
# the machine's GitHub credential) ahead of this folder on the Path.
& $tar -xf (Need 'gh.zip') -C $unpack
$ghExe = Get-ChildItem -Path $unpack -Recurse -Filter 'gh.exe' | Select-Object -First 1
if (-not $ghExe) { throw 'gh.zip holds no gh.exe' }
$ghDir = Join-Path $env:ProgramFiles 'GitHub CLI'
New-Item -ItemType Directory -Force -Path $ghDir | Out-Null
Copy-Item -Path $ghExe.FullName -Destination (Join-Path $ghDir 'gh.exe') -Force
Add-MachinePath $ghDir

Remove-Item -Path $unpack -Recurse -Force -ErrorAction SilentlyContinue
'win-img: installed Git, Claude Code, Codex, Bun and the GitHub CLI'
