$ErrorActionPreference = 'Stop'
python -m pip install -r requirements.txt
$paths = @('C:\Program Files\Tesseract-OCR', 'C:\Program Files (x86)\Tesseract-OCR')
$ocr = $paths | Where-Object { Test-Path (Join-Path $_ 'tesseract.exe') } | Select-Object -First 1
if (-not $ocr) { throw 'Tesseract not found on build PC. Install Tesseract on the build machine.' }
python -m PyInstaller --noconfirm --clean --windowed --onedir --name ModakBossOverlay --add-data "${ocr};Tesseract-OCR" --collect-all mss modak_overlay.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
Compress-Archive -Path 'dist\ModakBossOverlay' -DestinationPath 'ModakBossOverlay_Windows_Portable.zip' -Force
Write-Host 'Build complete: ModakBossOverlay_Windows_Portable.zip'
