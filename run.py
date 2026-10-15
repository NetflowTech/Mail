"""Run OroStarMail from source.

Setup (once):
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt

Run:
    .venv/bin/python run.py
"""
from emailclient.app import main

if __name__ == "__main__":
    main()
