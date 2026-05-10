$me = $PID
Get-CimInstance Win32_Process |
    Where-Object { $_.ProcessId -ne $me -and $_.CommandLine -like '*bot_main.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
& 'C:/Users/User/AppData/Local/Microsoft/WindowsApps/python3.13.exe' 'd:/Luna 5.0/bot_main.py'
