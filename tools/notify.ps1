# Pop-up message box (center screen, must be dismissed). Usage: notify.ps1 "Title" "Message"
param([string]$Title = "Credit Report Tool", [string]$Message = "")
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
# A TopMost owner form forces the box to the foreground, above the browser/other windows.
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.WindowState = 'Minimized'
$owner.Show()
$owner.Activate()
[System.Windows.Forms.MessageBox]::Show(
    $owner, $Message, $Title,
    [System.Windows.Forms.MessageBoxButtons]::OK,
    [System.Windows.Forms.MessageBoxIcon]::Information) | Out-Null
$owner.Close()
