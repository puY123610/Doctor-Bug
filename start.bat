@echo off
chcp 65001 >nul
REM Doctor Bug 一键启动（Windows，实验性）
REM 注意：产品本身还没在 Windows 上适配、验证过。这个脚本只帮你把环境搭起来、把程序打开，能不能跑通不保证。
REM 做法参考 AUTOMATIC1111 的 webui-user.bat（同类项目通行做法）。
REM 运行之前需要先准备好：Python 3.12（装的时候勾选 Add to PATH）和联网。没有 Python 时脚本会停下来说明，不会自己下载（太慢）。
cd /d %~dp0

REM 光有装好的记号还不算数：环境里的 python 得真在，缺了就重装。
if not exist .venv\.drbug_setup_ok goto setup
if not exist .venv\Scripts\python.exe goto setup
goto ask_target

:setup
set PYVER=
py -3.12 -c "import sys" >nul 2>&1 && set PYVER=-3.12
if not defined PYVER py -3.11 -c "import sys" >nul 2>&1 && set PYVER=-3.11
if not defined PYVER goto no_python

echo 第一次运行：正在建环境、装依赖，大约几分钟，别关窗口…
py %PYVER% -m venv .venv || goto failed
.venv\Scripts\pip.exe install -r requirements.txt || goto failed
type nul > .venv\.drbug_setup_ok
echo 环境装好了，以后再打开就直接进。

:ask_target
set /p TARGET=把要修的项目文件夹路径粘贴进来，再按回车：
if not exist "%TARGET%" (
  echo 这个文件夹不存在：%TARGET%
  pause
  exit /b 1
)

set PYTHONPATH=%~dp0
cd /d "%TARGET%"
"%~dp0.venv\Scripts\python.exe" -m core.cli
pause
exit /b 0

:no_python
echo 这台电脑上还没有 Python 3.12，先装好再回来运行这个脚本：
echo   去 python.org 下载 Python 3.12 安装包，安装时务必勾选 Add python.exe to PATH。
echo 装好以后，重新双击这个脚本就行。
pause
exit /b 1

:failed
echo 出问题了：环境没装成功，多半是网络不通（装依赖要联网）。
echo 国内网络可以先在命令提示符里运行 set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple 再重新双击这个脚本。
echo 把上面这段文字拍下来发给我们就行；也可以删掉本文件夹里的 .venv 再重新运行这个脚本。
pause
exit /b 1
