# install-falcon.ps1: the CrowdStrike Falcon sensor in the image, with your CID, from what
# fetch-falcon.sh put in win-img\files (README.md, "Example: CrowdStrike Falcon"). Copy it next to
# install.ps1 and call it at the end there:  & (Join-Path $PSScriptRoot 'install-falcon.ps1')
#
#   NO_START=1    the image's sensor never registers: each machine registers at its first boot,
#                 as its own host
#   VDI=1         a fork or a snapshot's copy notices it was cloned and registers as a new host
#   ProvNoWait=1  the build has no network: the installer doesn't wait for Falcon's cloud
#
# Right after the build's first logon the installer can fail (exit 28, or 1618 while Windows
# Installer is busy) and succeed a minute later: up to 10 tries. The CID is in the image, readable
# by every machine's administrators, as on any host you deploy the sensor to.
$ErrorActionPreference = 'Stop'
$files = Join-Path $PSScriptRoot 'files'
$sensor = Join-Path $files 'FalconSensor_Windows.exe'
$cidFile = Join-Path $files 'falcon-cid.txt'
foreach ($f in $sensor, $cidFile) {
    if (-not (Test-Path $f)) { throw "$f is missing: run fetch-falcon.sh from fetch.sh before the build" }
}
$cid = (Get-Content -Raw $cidFile).Trim()
if ($cid -notmatch '^[0-9A-Fa-f]{32}-[0-9A-Fa-f]{2}$') { throw "falcon-cid.txt holds no CID" }
$logs = 'C:\ProgramData\boxd\falcon-install'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$code = -1
for ($try = 1; $try -le 10; $try++) {
    $p = Start-Process -FilePath $sensor -Wait -PassThru -ArgumentList @(
        '/install', '/quiet', '/norestart', "CID=$cid", 'NO_START=1', 'VDI=1', 'ProvNoWait=1',
        '/log', (Join-Path $logs "try-$try.log"))
    $code = $p.ExitCode
    if ($code -eq 0 -and (Get-Service CSAgent -ErrorAction SilentlyContinue)) { break }
    "falcon: try $try exited $code; again in 60 s"
    Start-Sleep -Seconds 60
}
if ($code -ne 0 -or -not (Get-Service CSAgent -ErrorAction SilentlyContinue)) {
    throw "the Falcon sensor's installer exited $code (logs in $logs)"
}
"falcon: the sensor is installed (it registers at each machine's first boot)"
