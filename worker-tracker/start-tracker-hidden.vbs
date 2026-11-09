' Runs start-tracker.ps1 hidden - no console window. Scheduled at logon and every 5 min by the
' "WorkerTrackerServer" scheduled task, so a crash or reboot self-heals within 5 minutes.
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & dir & "\start-tracker.ps1""", 0, False
