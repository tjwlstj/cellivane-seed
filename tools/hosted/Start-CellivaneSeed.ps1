param(
    [ValidateSet('serve', 'smoke')][string]$Mode = 'serve',
    [string]$StorageRoot = '',
    [string]$AssetRoot = '',
    [string]$AssetCache = ''
)
$ErrorActionPreference = 'Stop'
$taskArgs = @('-3', '-B', (Join-Path $PSScriptRoot 'cellivane_seed_lab.py'), '--mode', $Mode)
if ($StorageRoot) { $taskArgs += @('--storage-root', $StorageRoot) }
if ($AssetRoot) { $taskArgs += @('--asset-root', $AssetRoot) }
if ($AssetCache) { $taskArgs += @('--asset-cache', $AssetCache) }
& py @taskArgs
exit $LASTEXITCODE
