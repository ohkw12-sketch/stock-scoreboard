$ErrorActionPreference = "Stop"

Write-Host "OpenDART API key setup"
Write-Host "Paste the 40-character key below. The key will not be displayed."

$secureKey = Read-Host "OpenDART API key" -AsSecureString
$keyPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
$plainKey = $null

try {
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPtr)

    if ([string]::IsNullOrWhiteSpace($plainKey) -or $plainKey.Length -ne 40) {
        Write-Host "Not saved: the OpenDART key must be exactly 40 characters." -ForegroundColor Red
        exit 1
    }

    [Environment]::SetEnvironmentVariable("DART_API_KEY", $plainKey, "User")
    Write-Host "Saved as the Windows user environment variable DART_API_KEY." -ForegroundColor Green
    Write-Host "The key was not written to the repository or printed on screen."
}
finally {
    if ($keyPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPtr)
    }
    $plainKey = $null
    $secureKey.Dispose()
}

Read-Host "Press Enter to close"
