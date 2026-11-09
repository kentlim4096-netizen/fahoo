' Runs start-ngrok.ps1 hidden - no console window. Scheduled at logon and every 5 min by the
' "WorkerTrackerNgrok" scheduled task, so a crash, reboot, or ngrok session teardown self-heals.
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & dir & "\start-ngrok.ps1""", 0, False
