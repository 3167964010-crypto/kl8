@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Please install Python 3 and try again.
  pause
  exit /b 1
)
REM ===== 大模型 API 配置（可选）=====
REM 想启用 AI 大模型预测时：删掉下面三行行首的 REM 并填入你的密钥，保存后重新双击本脚本
REM set KL8_LLM_URL=https://api.deepseek.com/v1/chat/completions
REM set KL8_LLM_KEY=在这里填入你的API密钥
REM set KL8_LLM_MODEL=deepseek-chat

echo Starting KL8 sync server...
python sync_server.py
pause
