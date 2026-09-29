' Runs ngrok-bypass-vpn.ps1 -Elevated with no visible console window.
' Used by the NgrokBypassVpn scheduled task (see ngrok-bypass-vpn.ps1 -Install) so the bypass
' re-applies silently both on a timer and the instant Pritunl reports a connection.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
toolsDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & toolsDir & "\ngrok-bypass-vpn.ps1"" -Elevated", 0, False
