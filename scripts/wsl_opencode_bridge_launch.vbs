Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run "pythonw.exe """ & here & "\wsl_opencode_bridge.py""", 0, False
