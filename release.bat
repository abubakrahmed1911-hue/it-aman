@echo off
REM ============================================================
REM  IT Aman — Release Script (Windows)
REM  الاستخدام:  release.bat 3.4
REM  بيعمل: توليد manifest + توقيع + git push
REM ============================================================

setlocal enabledelayedexpansion

REM --- رقم الإصدار (اجباري) ---
set VERSION=%1
if "%VERSION%"=="" (
    echo.
    echo  خطأ: لازم تحدد رقم الإصدار
    echo  مثال:  release.bat 3.4
    echo.
    exit /b 1
)

REM --- مسار الـ private key ---
set KEY_PATH=%USERPROFILE%\.it-aman\ed25519_private.pem

REM --- تأكد إن الـ key موجود ---
if not exist "%KEY_PATH%" (
    echo.
    echo  خطأ: الـ private key مش موجود في:
    echo  %KEY_PATH%
    echo.
    echo  شغّل الأمر ده مرة واحدة عشان تنشئ الـ key:
    echo    python generate_keypair.py
    echo.
    exit /b 1
)

REM --- تأكد إن python موجود ---
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  خطأ: Python مش مثبّت أو مش في الـ PATH
    echo  حمّله من: https://python.org
    echo.
    exit /b 1
)

REM --- تأكد إن cryptography مثبّت ---
python -c "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  جاري تثبيت مكتبة cryptography...
    pip install cryptography
    if errorlevel 1 (
        echo  فشل التثبيت. شغّل:  pip install cryptography
        exit /b 1
    )
)

echo.
echo  ================================================
echo   IT Aman — Release v%VERSION%
echo  ================================================
echo.

REM --- خطوة 1: توليد الـ manifest ---
echo  [1/3] توليد update_manifest.json...
python generate_manifest.py %VERSION% . --key "%KEY_PATH%"
if errorlevel 1 (
    echo.
    echo  فشل توليد الـ manifest. تحقق من الـ private key.
    exit /b 1
)
echo  تم توليد الـ manifest بنجاح.
echo.

REM --- خطوة 2: تحديث version.json ---
echo  [2/3] تحديث version.json...
python -c "import json; f=open('version.json','w'); json.dump({'version':'%VERSION%'},f); f.close()"
echo  تم تحديث version.json.
echo.

REM --- خطوة 3: git add + commit + push ---
echo  [3/3] رفع التعديلات على GitHub...
git add src\gui.py src\daemon.py update_manifest.json version.json data.json
git status --short
echo.

set COMMIT_MSG=v%VERSION%: update release
git commit -m "%COMMIT_MSG%"
if errorlevel 1 (
    echo  ملاحظة: ما فيش تعديلات جديدة أو git commit فشل.
)

git push origin main
if errorlevel 1 (
    echo.
    echo  فشل الـ push. تأكد من:
    echo   1. إنك متصل بالإنترنت
    echo   2. إن SSH key مضاف على GitHub
    echo   3. شغّل:  git push origin main  يدوياً
    exit /b 1
)

echo.
echo  ================================================
echo   تم! v%VERSION% متاح على GitHub
echo   الأجهزة هتتحدث تلقائياً خلال 24 ساعة
echo   أو اضغط "تحديث الآن" من الواجهة
echo  ================================================
echo.

endlocal
