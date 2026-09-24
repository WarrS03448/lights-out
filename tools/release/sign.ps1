# Sign first-party release binaries with Azure Artifact Signing, then verify them.
param(
    [string]$Path,
    [string]$EvidenceDirectory,
    [switch]$CheckConfiguration,
    [switch]$VerifyOnly
)
$ErrorActionPreference = 'Stop'
# A release can be launched from PowerShell 7 while this script runs in Windows
# PowerShell. Load this host's security module, not one inherited via PSModulePath.
Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
$signTool = $env:HUB_SIGN_SIGNTOOL
if (-not $signTool) {
    $sdkRoot = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits/10/bin'
    $sdk = Get-ChildItem -LiteralPath $sdkRoot -Directory |
        Where-Object { $_.Name -match '^\d+\.\d+\.\d+\.\d+$' } |
        Sort-Object { [version]$_.Name } -Descending |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'x64/signtool.exe') } |
        Select-Object -First 1
    if ($sdk) { $signTool = Join-Path $sdk.FullName 'x64/signtool.exe' }
}
$signDlib = $env:HUB_SIGN_DLIB
if (-not $signDlib) {
    $signDlib = Join-Path $env:LOCALAPPDATA 'Microsoft/MicrosoftArtifactSigningClientTools/Azure.CodeSigning.Dlib.dll'
}
$signMetadata = $env:HUB_SIGN_METADATA
if (-not $signMetadata) { $signMetadata = Join-Path $env:LOCALAPPDATA 'LightsOut/signing/metadata.json' }
$publisher = $env:HUB_SIGN_PUBLISHER
if (-not $publisher) { $publisher = 'Samuel Warren' }
foreach ($item in @($signTool, $signDlib, $signMetadata)) {
    if (-not $item -or -not (Test-Path -LiteralPath $item -PathType Leaf)) {
        throw 'Signing configuration is incomplete. See docs/code-signing.md; no unsigned release will be published.'
    }
}
# The existing metadata selects AzureCliCredential. Only the CLI's normal local
# credential cache is used; never export access tokens into metadata or logs.
$metadata = Get-Content -LiteralPath $signMetadata -Raw -Encoding UTF8 | ConvertFrom-Json
if ($metadata.AccessToken) { throw 'Remove the access token from signing metadata; use Azure CLI authentication.' }
$azureBin = Join-Path $env:ProgramFiles 'Microsoft SDKs/Azure/CLI2/wbin'
if (Test-Path -LiteralPath $azureBin) { $env:PATH = $azureBin + ';' + $env:PATH }
if ($CheckConfiguration) {
    Write-Output 'Signing tools and metadata found.'
    exit 0
}
if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'A release binary is required.' }
$binary = (Resolve-Path -LiteralPath $Path).Path
if (-not $VerifyOnly) {
    & $signTool sign /v /fd SHA256 /tr 'http://timestamp.acs.microsoft.com' /td SHA256 /dlib $signDlib /dmdf $signMetadata $binary
    if ($LASTEXITCODE -ne 0) { throw 'Artifact Signing failed. Publication must stop.' }
}
& $signTool verify /pa /all /v $binary
if ($LASTEXITCODE -ne 0) { throw 'Signature verification failed. Publication must stop.' }
$signature = Get-AuthenticodeSignature -LiteralPath $binary
$identityEku = '1.3.6.1.4.1.311.97.951605561.555398629.748726612.577571204'
$signerEkus = @($signature.SignerCertificate.Extensions | Where-Object { $_.Oid.Value -eq '2.5.29.37' } |
    ForEach-Object { $_.EnhancedKeyUsages | ForEach-Object { $_.Value } })
if ($signature.Status -ne 'Valid' -or $signature.SignatureType -ne 'Authenticode' -or
    -not $signature.TimeStamperCertificate -or $identityEku -notin $signerEkus -or
    $signature.SignerCertificate.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) -ne $publisher) {
    throw 'Release requires a valid timestamped signature from the expected publisher.'
}
Write-Output "Verified timestamped signature: $publisher"
if ($EvidenceDirectory -and -not $VerifyOnly) {
    # Retain every verified compiler signing input. The publisher requires only
    # the final setup, byte-identical to its output, and rejects internal signing.
    New-Item -ItemType Directory -Path $EvidenceDirectory -Force | Out-Null
    Copy-Item -LiteralPath $binary -Destination (Join-Path $EvidenceDirectory ([IO.Path]::GetFileName($binary)))
}
