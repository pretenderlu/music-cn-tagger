' Music CN Tagger — silent launcher (Windows).
'
' Launches the server via pythonw.exe so no console window appears
' and the script self-exits immediately; the browser will open on its
' own once Flask is up. To stop the server, close it from Task Manager
' (look for "pythonw.exe").
'
' Use start.bat instead if you want to see startup logs / errors.

Option Explicit

Dim sh, fso, scriptDir, pyCmd
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = scriptDir

' pythonw.exe ships with the python.org installer. -X utf8 keeps
' Chinese filenames/console output sane on Windows.
pyCmd = "pythonw -X utf8 """ & scriptDir & "\app.py"""

' 0 = hidden window, False = do not wait for it to finish.
sh.Run pyCmd, 0, False
