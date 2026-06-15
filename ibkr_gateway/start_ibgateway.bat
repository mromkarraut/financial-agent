@echo off
rem Start IB Gateway (TWS socket API mode)
rem Paper trading: listens on port 4002
rem Live trading:  listens on port 4001
rem
rem After IB Gateway opens, log in with your credentials.
rem Enable API in TWS/Gateway: Edit -> Global Configuration -> API -> Settings
rem   - Enable ActiveX and Socket Clients: checked
rem   - Socket port: 4002 (paper) or 4001 (live)
rem   - Allow connections from localhost only: checked

set GW_EXE=C:\Jts\ibgateway\1039\ibgateway.exe

if not exist "%GW_EXE%" (
    echo ERROR: IB Gateway not found at %GW_EXE%
    echo Install from https://www.interactivebrokers.com/en/trading/ibgateway-stable.php
    exit /b 1
)

echo Starting IB Gateway...
echo After login, enable API: Edit ^> Global Configuration ^> API ^> Settings
echo   Socket port: 4002 ^(paper^) or 4001 ^(live^)

start "" "%GW_EXE%"
