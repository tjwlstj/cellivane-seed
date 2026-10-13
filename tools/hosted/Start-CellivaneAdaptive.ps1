param(
    [ValidateSet('plan', 'smoke', 'serve')][string]$Mode = 'plan',
    [ValidateSet('auto', 'none', 'draft')][string]$Policy = 'auto',
    [ValidateRange(1, 128)][int]$ExpectedOutputTokens = 128,
    [Parameter(Mandatory = $true)][string]$AssetRoot,
    [Parameter(Mandatory = $true)][string]$TargetFile,
    [ValidateSet('0.6b', '1.7b', '4b')][string]$TargetProfile = '4b',
    [string]$DraftFile = '',
    [string]$CalibrationFile = '',
    [string]$CalibrationSourceRoot = '',
    [string]$StorageRoot = '',
    [ValidateRange(1024, 65535)][int]$Port = 18961,
    [ValidateRange(1024, 65535)][int]$WorkerPort = 18962,
    [ValidateRange(0, 86400)][int]$ServeSeconds = 0
)
$ErrorActionPreference = 'Stop'
$taskArgs = @('-3', '-B', (Join-Path $PSScriptRoot 'cellivane_seed_adaptive.py'),
    '--mode', $Mode, '--policy', $Policy, '--expected-output-tokens', $ExpectedOutputTokens,
    '--asset-root', $AssetRoot, '--target-file', $TargetFile, '--target-profile', $TargetProfile,
    '--port', $Port, '--worker-port', $WorkerPort, '--serve-seconds', $ServeSeconds)
if ($DraftFile) { $taskArgs += @('--draft-file', $DraftFile) }
if ($CalibrationFile) { $taskArgs += @('--calibration-file', $CalibrationFile) }
if ($CalibrationSourceRoot) { $taskArgs += @('--calibration-source-root', $CalibrationSourceRoot) }
if ($StorageRoot) { $taskArgs += @('--storage-root', $StorageRoot) }
& py @taskArgs
exit $LASTEXITCODE
