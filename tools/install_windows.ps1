param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[A-Za-z0-9_-]+/[A-Za-z0-9_-][A-Za-z0-9_.-]*$')]
    [string]$Repository
)
$ErrorActionPreference = 'Stop'
$architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
if ($architecture -ne 'AMD64') { throw 'This package requires x64 Windows.' }
$base = "https://github.com/$Repository/releases/latest/download"
$name = 'TGCopyTrader-Windows-x64.zip'
$root = Join-Path $env:LOCALAPPDATA 'Programs\TGCopyTrader'
$stage = Join-Path $root ([guid]::NewGuid().ToString())
New-Item -ItemType Directory -Path $stage -Force | Out-Null
try {
    $zip = Join-Path $stage $name
    $manifest = Join-Path $stage 'SHA256SUMS.txt'
    Invoke-WebRequest "$base/SHA256SUMS.txt" -UseBasicParsing -OutFile $manifest
    $entries = @(Select-String -LiteralPath $manifest -Pattern ('^([0-9a-fA-F]{64})  ' + [regex]::Escape($name) + '$'))
    if ($entries.Count -ne 1) { throw 'Release checksum is missing or ambiguous.' }
    $expected = $entries[0].Matches[0].Groups[1].Value
    Invoke-WebRequest "$base/$name" -UseBasicParsing -OutFile $zip
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $zip).Hash -ne $expected) { throw 'Release checksum mismatch.' }
    $destination = Join-Path $root $expected
    $exe = Join-Path $destination 'TGCopyTrader.exe'
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        Expand-Archive -LiteralPath $zip -DestinationPath $stage
        $payload = Join-Path $stage 'TGCopyTrader'
        $entry = Join-Path $payload 'TGCopyTrader.exe'
        if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { throw 'Incomplete release package.' }
        Set-Content -LiteralPath $entry -Stream Zone.Identifier -Value "[ZoneTransfer]`r`nZoneId=3"
        [IO.Directory]::Move($payload, $destination)
    }
    $menu = [Environment]::GetFolderPath('Programs')
    $shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $menu 'TGCopyTrader.lnk'))
    $shortcut.TargetPath = $exe
    $shortcut.WorkingDirectory = $destination
    $shortcut.Save()
    Write-Output "Installed: $exe"
} finally {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
