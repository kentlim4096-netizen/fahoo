' Starts the worker tracker server with no console window, unless it is already running.
' Run at logon and every 5 min by the "WorkerTracker" scheduled task.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
Set ex = sh.Exec("cmd /c netstat -ano | findstr /R /C:"":8780 .*LISTENING""")
If InStr(ex.StdOut.ReadAll, "LISTENING") = 0 Then
  If Not fso.FolderExists(dir & "\data") Then fso.CreateFolder(dir & "\data")
  sh.Run "cmd /c """"" & dir & "\..\.venv\Scripts\python.exe"" server.py >> """ & dir & "\data\server.log"" 2>&1""", 0, False
End If
