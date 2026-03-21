#!/usr/bin/env python3
"""
main.py
═══════
Entry point — creates the output directory and launches the GUI.
"""

from constants import OUTPUTS_DIR
from gui import App

if __name__ == "__main__":
    OUTPUTS_DIR.mkdir(exist_ok=True)
    app = App()
    app.mainloop()