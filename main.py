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
    
    
    # pending gen: https://claude.ai/chat/d233cca6-f17d-4cdc-9df9-6cd3e8e7fa1d
    # user: python, for project persistence (2 am)
    
    # pending gen: https://claude.ai/chat/00be006b-a4d7-446b-b15b-0774bf737627
    # user: brandon, for match history and agent stats page (7 pm)