' Launches the credit report tool service with no console window (window style 0).
' Registered as a logon task by install-autostart.ps1 - see the README.
' Appends to data\service.log so a failed start is diagnosable after the fact.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = appDir
If Not fso.FolderExists(appDir & "\data") Then fso.CreateFolder(appDir & "\data")
sh.Run "cmd /c """"" & appDir & "\.venv\Scripts\python.exe"" scraper_service.py >> """ & appDir & "\data\service.log"" 2>&1""", 0, False
