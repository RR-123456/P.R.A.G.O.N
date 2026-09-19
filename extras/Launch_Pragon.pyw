"""
Launch_Pragon.pyw
------------------
Double-clickable, console-free launcher for P.R.A.G.O.N.

Run with pythonw.exe (no terminal window). This is the file the desktop
shortcut points to. It just makes sure we're running from the project
folder (so all the relative paths inside pragon_main.py resolve) and
then starts the app.
"""
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)
sys.path.insert(0, PROJECT_DIR)

if __name__ == "__main__":
    import pragon_main
    pragon_main.main()
