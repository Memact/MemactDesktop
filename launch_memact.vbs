Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
mainPath = fso.BuildPath(root, "main.py")
pythonwCommand = "pythonw"

shell.CurrentDirectory = root

On Error Resume Next
Set lookup = shell.Exec("cmd /c where pythonw.exe")
If Err.Number = 0 Then
    Do While Not lookup.StdOut.AtEndOfStream
        line = Trim(lookup.StdOut.ReadLine())
        If line <> "" Then
            pythonwCommand = line
            Exit Do
        End If
    Loop
End If
Err.Clear
On Error GoTo 0

shell.Run """" & pythonwCommand & """ """ & mainPath & """", 0, False
