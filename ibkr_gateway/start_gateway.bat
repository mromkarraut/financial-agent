@echo off
rem IBKR CP Gateway launcher
rem Called from start.py via cmd.exe — do not run this directly from bash.

rem Explicit Java path (no reliance on Windows PATH)
set JAVA_BIN=C:\Program Files\Java\jre1.8.0_461\bin

rem Config file — paper and live use the same gateway config.
rem Paper vs live is determined by login credentials at https://localhost:5000
set config_file=root\conf.yaml
for /F %%i in ("%config_file%") do set config_path=%%~dpi

set RUNTIME_PATH=%config_path%;dist\ibgroup.web.core.iblink.router.clientportal.gw.jar;build\lib\runtime\*

echo IBKR CP Gateway starting...
echo Config : %config_file%
echo Java   : %JAVA_BIN%\java.exe

"%JAVA_BIN%\java.exe" ^
  -server ^
  -Dvertx.disableDnsResolver=true ^
  -Djava.net.preferIPv4Stack=true ^
  -Dvertx.logger-delegate-factory-class-name=io.vertx.core.logging.SLF4JLogDelegateFactory ^
  -Dnologback.statusListenerClass=ch.qos.logback.core.status.OnConsoleStatusListener ^
  -Dnolog4j.debug=true ^
  -Dnolog4j2.debug=true ^
  -classpath %RUNTIME_PATH% ^
  ibgroup.web.core.clientportal.gw.GatewayStart
