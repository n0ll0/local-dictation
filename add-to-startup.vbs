' Adds a Start Menu Startup shortcut so dictation launches hidden at every logon.
' Double-click to enable. To undo, press Win+R, open shell:startup and delete
' the "Local dictation.lnk" file created here.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
If Not fso.FileExists(BuildPath(appDir, "startup.vbs")) Then
    MsgBox "startup.vbs was not found next to this script. Run this from the " _
         & "installed folder.", vbCritical, "Local dictation"
    WScript.Quit 1
End If
linkPath = sh.SpecialFolders("Startup") & "\Local dictation.lnk"
Set lnk = sh.CreateShortcut(linkPath)
lnk.TargetPath = sh.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\wscript.exe"
lnk.Arguments = """" & BuildPath(appDir, "startup.vbs") & """"
lnk.Description = "Local dictation, starts hidden at logon"
lnk.Save
MsgBox "Dictation will now start automatically at every logon." & vbNewLine _
     & vbNewLine _
     & "Created: " & linkPath & vbNewLine & vbNewLine _
     & "To undo: press Win+R, type shell:startup, press Enter, " _
     & "and delete ""Local dictation.lnk"" there." & vbNewLine, _
       vbInformation, "Local dictation"

Function BuildPath(folder, name)
    BuildPath = fso.BuildPath(folder, name)
End Function
