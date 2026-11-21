# Delete the kind demo cluster.
# Native tools write progress to stderr; PowerShell 5.1 would treat that as an error under "Stop".
# Exit codes are checked explicitly in Invoke-Checked instead.
$ErrorActionPreference = "Continue"
$cmd = Get-Command kind -ErrorAction SilentlyContinue
$Kind = if ($cmd) { $cmd.Source } else { (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter kind.exe | Select-Object -First 1).FullName }
& $Kind delete cluster --name incident-copilot
