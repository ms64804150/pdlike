# Build a portable Windows client: dist\PerfPilot\PerfPilot.exe
# Usage: powershell -ExecutionPolicy Bypass -File packaging\build.ps1

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Get-BuildPython {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:APPDATA\uv\python\cpython-3.12.14-windows-x86_64-none\python.exe",
        "$env:USERPROFILE\.local\share\uv\python\cpython-3.12.14-windows-x86_64-none\python.exe"
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $listed = & py -0p 2>$null
        foreach ($line in $listed) {
            if ($line -match "32") { continue }
            if ($line -match "([A-Za-z]:\\.+python\.exe)") {
                $hit = $Matches[1].Trim()
                if (Test-Path $hit) { return $hit }
            }
        }
    }
    throw "Need 64-bit Python. PATH currently resolves to 32-bit python, which cannot build pymobiledevice3/PyInstaller wheels."
}

function Assert-64BitPython([string]$PythonExe) {
    $bits = & $PythonExe -c "import struct; print(struct.calcsize('P') * 8)"
    if ($LASTEXITCODE -ne 0) { throw "Failed to probe $PythonExe" }
    if ($bits.Trim() -ne "64") {
        throw "Refusing $PythonExe ($bits-bit). Use 64-bit Python for this build."
    }
}

function Invoke-Python {
    param([string]$PythonExe, [string[]]$PyArgs)
    Write-Host ("+ {0} {1}" -f $PythonExe, ($PyArgs -join " "))
    & $PythonExe @PyArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit $LASTEXITCODE"
    }
}

function Stop-LockedDist {
    $dist = Join-Path $Root "dist\PerfPilot"
    Write-Host "[Packaging] stop processes locking $dist"
    Get-Process PerfPilot -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host ("[Packaging] stop PerfPilot pid={0} path={1}" -f $_.Id, $_.Path)
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
    Get-CimInstance Win32_Process -Filter "Name='adb.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
        $exe = [string]$_.ExecutablePath
        if ($exe -and $exe.StartsWith($dist, [System.StringComparison]::OrdinalIgnoreCase)) {
            Write-Host ("[Packaging] stop bundled adb pid={0} path={1}" -f $_.ProcessId, $exe)
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 1
}

$BasePython = Get-BuildPython
Assert-64BitPython $BasePython
Write-Host "Base Python: $BasePython"

$VenvDir = Join-Path $Root ".venv-packaging"
$Python = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "Creating packaging venv: $VenvDir"
    Invoke-Python $BasePython @("-m", "venv", $VenvDir)
}
Assert-64BitPython $Python
Write-Host "Using Python: $Python"

$Vendor = Join-Path $Root "vendor\platform-tools"
$Zip = Join-Path $env:TEMP "platform-tools-windows.zip"
$Url = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"

if (-not (Test-Path (Join-Path $Vendor "adb.exe"))) {
    Write-Host "Downloading Android platform-tools..."
    Invoke-WebRequest -Uri $Url -OutFile $Zip
    $Extract = Join-Path $env:TEMP "platform-tools-extract"
    if (Test-Path $Extract) { Remove-Item $Extract -Recurse -Force }
    Expand-Archive -Path $Zip -DestinationPath $Extract -Force
    New-Item -ItemType Directory -Force -Path (Split-Path $Vendor) | Out-Null
    if (Test-Path $Vendor) { Remove-Item $Vendor -Recurse -Force }
    Move-Item (Join-Path $Extract "platform-tools") $Vendor
}

$WintunDll = Join-Path $Root "vendor\wintun\amd64\wintun.dll"
if (-not (Test-Path $WintunDll)) {
    Write-Host "Downloading WinTun..."
    $WintunZip = Join-Path $env:TEMP "wintun-0.14.1.zip"
    Invoke-WebRequest -Uri "https://www.wintun.net/builds/wintun-0.14.1.zip" -OutFile $WintunZip -UseBasicParsing
    $WintunExtract = Join-Path $env:TEMP "wintun-extract"
    if (Test-Path $WintunExtract) { Remove-Item $WintunExtract -Recurse -Force }
    Expand-Archive -Path $WintunZip -DestinationPath $WintunExtract -Force
    $Src = Get-ChildItem $WintunExtract -Recurse -Filter "wintun.dll" | Where-Object { $_.FullName -match "amd64" } | Select-Object -First 1
    if (-not $Src) { throw "WinTun zip missing amd64\\wintun.dll" }
    New-Item -ItemType Directory -Force -Path (Split-Path $WintunDll) | Out-Null
    Copy-Item $Src.FullName $WintunDll -Force
    $License = Get-ChildItem $WintunExtract -Recurse -Filter "LICENSE.txt" | Select-Object -First 1
    if ($License) { Copy-Item $License.FullName (Join-Path $Root "vendor\wintun\LICENSE.txt") -Force }
}
if (-not (Test-Path (Join-Path $Vendor "adb.exe"))) {
    throw "platform-tools missing adb.exe at $Vendor"
}
$Req = Join-Path $Root "packaging\requirements-build.txt"
Invoke-Python $Python @("-m", "pip", "install", "-U", "pip")
Invoke-Python $Python @("-m", "pip", "install", "-r", $Req)
$TunDest = Join-Path $VenvDir "Lib\site-packages\pytun_pmd3\wintun\bin\amd64"
if (Test-Path $WintunDll) {
    New-Item -ItemType Directory -Force -Path $TunDest | Out-Null
    Copy-Item $WintunDll (Join-Path $TunDest "wintun.dll") -Force
    Write-Host "[Packaging] wintun.dll -> $TunDest"
}
Stop-LockedDist
Invoke-Python $Python @("-m", "PyInstaller", "--noconfirm", "--clean", (Join-Path $Root "packaging\perfpilot.spec"))

$Out = Join-Path $Root "dist\PerfPilot\PerfPilot.exe"
if (-not (Test-Path $Out)) {
    throw "PyInstaller finished but $Out is missing"
}

$Internal = Join-Path $Root "dist\PerfPilot\_internal"
# sslpsk_pmd3's OpenSSL 3 extension currently imports the legacy Windows
# filename even though it uses the OpenSSL 3 ABI. Python ships the required
# OpenSSL 3 DLL with its real -x64 name, so add only the alias the extension
# requests. Plain libssl-3.dll/libcrypto-3.dll copies are not referenced and
# would duplicate another ~9 MB in the portable bundle.
$SslSource = Join-Path $Internal "libssl-3-x64.dll"
$SslCompat = Join-Path $Internal "libssl-1_1-x64.dll"
if ((Test-Path $SslSource) -and -not (Test-Path $SslCompat)) {
    Copy-Item -LiteralPath $SslSource -Destination $SslCompat
    Write-Host ("[Packaging] add OpenSSL compatibility DLL: {0}" -f (Split-Path $SslCompat -Leaf))
}

$PreviousPerfPilotData = $env:PERFPILOT_DATA
$BuildTestData = Join-Path $env:TEMP ("perfpilot-build-test-{0}" -f [guid]::NewGuid())
New-Item -ItemType Directory -Force -Path $BuildTestData | Out-Null
$env:PERFPILOT_DATA = $BuildTestData
$DoctorStdout = Join-Path $env:TEMP ("perfpilot-doctor-{0}.out" -f [guid]::NewGuid())
$DoctorStderr = Join-Path $env:TEMP ("perfpilot-doctor-{0}.err" -f [guid]::NewGuid())
try {
    $DoctorProcess = Start-Process -FilePath $Out -ArgumentList @("--doctor") -Wait -PassThru -NoNewWindow `
        -RedirectStandardOutput $DoctorStdout -RedirectStandardError $DoctorStderr
    $DoctorText = Get-Content -LiteralPath $DoctorStdout -Raw -Encoding UTF8 -ErrorAction SilentlyContinue
    if ($null -eq $DoctorText) {
        $DoctorText = ""
    } else {
        $DoctorText = $DoctorText.Trim()
    }
    if ($DoctorProcess.ExitCode -ne 0) {
        $DoctorError = (Get-Content -LiteralPath $DoctorStderr -Raw -Encoding UTF8 -ErrorAction SilentlyContinue).Trim()
        throw "Packaged PerfPilot --doctor failed with exit $($DoctorProcess.ExitCode): $DoctorError"
    }
    # Keep only the JSON object in case a bootloader/runtime message is
    # written around stdout by a different PowerShell/native-process setup.
    $jsonStart = $DoctorText.IndexOf("{")
    $jsonEnd = $DoctorText.LastIndexOf("}")
    if ($jsonStart -ge 0 -and $jsonEnd -ge $jsonStart) {
        $DoctorText = $DoctorText.Substring($jsonStart, $jsonEnd - $jsonStart + 1)
    }
} finally {
    Remove-Item -LiteralPath $DoctorStdout,$DoctorStderr -Force -ErrorAction SilentlyContinue
}
try {
    $Doctor = ConvertFrom-Json -InputObject $DoctorText
} catch {
    # Some Windows PowerShell builds reject otherwise valid UTF-8 JSON when
    # native stdout contains non-ASCII diagnostic text. The doctor output is
    # still trusted only when it explicitly reports portableReady=true.
    if ($DoctorText -match '"portableReady"\s*:\s*true') {
        $Doctor = [pscustomobject]@{ portableReady = $true }
        Write-Host "[Packaging] doctor JSON parser fallback: portableReady=true"
    } else {
        throw "Packaged PerfPilot --doctor returned invalid JSON: $DoctorText"
    }
}
if (-not $Doctor.portableReady) {
    $Details = ($Doctor.hints | ForEach-Object { "- $_" }) -join [Environment]::NewLine
    throw "Packaged runtime is incomplete:$([Environment]::NewLine)$Details"
}
if (-not $Doctor.iosTcpTunnel.ok) {
    throw "Packaged iOS TCP tunnel dependency failed: $($Doctor.iosTcpTunnel.error)"
}
Write-Host ("[Packaging] doctor passed: adb={0}; pymobiledevice3={1}; arch={2}" -f $Doctor.adbVersion, $Doctor.pymobiledevice3Version, $Doctor.pythonArchitecture)
foreach ($Collector in @("android", "ios")) {
    & $Out --collect $Collector --help | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Packaged $Collector collector smoke test failed with exit $LASTEXITCODE"
    }
    Write-Host "[Packaging] $Collector collector smoke test passed"
}
if ($null -eq $PreviousPerfPilotData) {
    Remove-Item Env:PERFPILOT_DATA -ErrorAction SilentlyContinue
} else {
    $env:PERFPILOT_DATA = $PreviousPerfPilotData
}
Remove-Item -LiteralPath $BuildTestData -Recurse -Force -ErrorAction SilentlyContinue

$Archive = Join-Path $Root "dist\PerfPilot-portable-x64.zip"
if (Test-Path $Archive) { Remove-Item $Archive -Force }
Compress-Archive -Path (Join-Path $Root "dist\PerfPilot") -DestinationPath $Archive -CompressionLevel Optimal
if (-not (Test-Path $Archive)) {
    throw "Failed to create portable archive: $Archive"
}

Write-Host ""
Write-Host "Build complete: $Out"
Write-Host "Portable archive: $Archive"
Write-Host "Send the ZIP as-is. Recipients do not need Python or ADB installed."
Write-Host "iPhone users still need Apple Mobile Device support (iTunes or Apple Devices)."
