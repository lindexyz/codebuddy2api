@echo off
rem 构建 CodeBuddy2API-UI 单文件桌面程序（需先安装依赖：pip install -r requirements.txt pyinstaller）
cd /d "%~dp0"
python -m PyInstaller --onefile --noconsole --name CodeBuddy2API-UI ^
  --paths . ^
  --collect-submodules uvicorn ^
  --collect-submodules core ^
  gui\codebuddy_gui.py
echo.
echo 构建完成: dist\CodeBuddy2API-UI.exe
echo 自检验证: dist\CodeBuddy2API-UI.exe --selftest
pause
