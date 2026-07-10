"""
main.py is the main cli tool that users call. When compiled
from Python to a .exe, it will be renamed to decapod.exe.
"""

# Imports
import sys
import tui

def main():
    app = tui.TUI()

    app.run()

app = tui.TUI()

if __name__ == "__main__":
    main()
