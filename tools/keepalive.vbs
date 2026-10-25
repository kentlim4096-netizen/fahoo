' Runs autostart.ps1 hidden. Scheduled at logon and every 5 min as a keep-alive; autostart.ps1 only
' launches the service/ngrok if they're not already running, so this is a no-op when all is well.
Set fso = CreateObject("Scripting.FileSystemObject")
toolsDir = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & toolsDir & "\autostart.ps1""", 0, False
