' Launch dictate.py at logon with no console window at all.
' The VBScript exists only to run background.cmd hidden - cmd.exe cannot do
' that itself, and pythonw.exe is not an option because it leaves sys.stdout
' as None, which print() cannot write to.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = Replace(WScript.ScriptFullName, "\" & WScript.ScriptName, "")
sh.Run """" & sh.CurrentDirectory & "\background.cmd""", 0, False
