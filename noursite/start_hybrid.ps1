$py = "$PSScriptRoot/.venv/Scripts/python.exe"

if (-not (Test-Path $py)) {
	Write-Error "Python virtual environment not found at $py"
	exit 1
}

# Stop old instances to avoid port/session conflicts.
Get-CimInstance Win32_Process |
	Where-Object {
		$_.Name -eq 'python.exe' -and (
			$_.CommandLine -match 'uvicorn\s+api.main:app' -or
			$_.CommandLine -match 'flask_front\\app.py'
		)
	} |
	ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

# FastAPI backend (API/JWT)
$apiProc = Start-Process -FilePath $py -ArgumentList @("-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000") -WorkingDirectory $PSScriptRoot -PassThru

# Wait until FastAPI answers before launching Flask.
$apiReady = $false
for ($i = 0; $i -lt 30; $i++) {
	if ($apiProc.HasExited) {
		break
	}
	try {
		$resp = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 1
		if ($resp.StatusCode -eq 200) {
			$apiReady = $true
			break
		}
	} catch {
		# keep trying briefly
	}
	Start-Sleep -Milliseconds 500
}

if (-not $apiReady) {
	if (-not $apiProc.HasExited) {
		Stop-Process -Id $apiProc.Id -Force
	}
	Write-Error "FastAPI failed to start on port 8000"
	exit 1
}

# Flask gateway frontend (stable mode, no reloader)
Start-Process -FilePath $py -ArgumentList @("flask_front\app.py") -WorkingDirectory $PSScriptRoot

Write-Output "FastAPI backend started: http://127.0.0.1:8000"
Write-Output "Flask gateway started: http://127.0.0.1:5000"
Write-Output "Use only: http://127.0.0.1:5000"
