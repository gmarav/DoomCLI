@echo off
rem DOOM CLI launcher for Windows (run.sh is the Linux/macOS equivalent).
rem Sound effects come from the engine (OpenAL); music plays through the
rem built-in Windows MIDI sequencer, so no extra system deps are needed.
setlocal
cd /d %~dp0
if not exist .venv\Scripts\python.exe (
    echo Creating venv...
    python -m venv .venv || exit /b 1
    .venv\Scripts\python.exe -m pip install -r requirements.txt || exit /b 1
)
rem Upgrade ViZDoom's bundled OpenAL (1.21) to ours (1.24+): newer builds
rem automatically follow Windows default-device changes, so sound effects
rem move to your headset along with everything else.
copy /y third_party\OpenAL32.dll .venv\Lib\site-packages\vizdoom\OpenAL32.dll >nul 2>&1
.venv\Scripts\python.exe -m doomcli.main %*
