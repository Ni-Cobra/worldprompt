@echo off
setlocal
set "MYCELIUM_JAVA=java"
if defined JAVA_HOME set "MYCELIUM_JAVA=%JAVA_HOME%\bin\java.exe"
pushd "%~dp0" || exit /b 1
if "%~1"=="" goto help
"%MYCELIUM_JAVA%" -jar tools\mycelium\mycelium-wrapper.jar %* --root .
goto done
:help
"%MYCELIUM_JAVA%" -jar tools\mycelium\mycelium-wrapper.jar --help
:done
set "MYCELIUM_EXIT=%ERRORLEVEL%"
popd
exit /b %MYCELIUM_EXIT%
