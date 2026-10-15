# Getting started

This guide takes you from a fresh copy of the source to a running app and your
first connected mailbox.

## 1. Install Python

You need **Python 3.10 or newer**.

- **macOS:** install from [python.org](https://www.python.org/downloads/) or via
  Homebrew (`brew install python`).
- **Windows:** install from [python.org](https://www.python.org/downloads/) and
  tick *"Add Python to PATH"* during setup.

Verify:

```bash
python3 --version
```

## 2. Create a virtual environment

Working inside a virtual environment keeps the app's dependencies isolated from
the rest of your system.

```bash
# from the project folder
python3 -m venv .venv
```

Activate it:

- **macOS / Linux:** `source .venv/bin/activate`
- **Windows (PowerShell):** `.venv\Scripts\Activate.ps1`
- **Windows (cmd):** `.venv\Scripts\activate.bat`

## 3. Install dependencies

```bash
pip install -r requirements.txt
```

This installs the Qt bindings (PySide6), the IMAP client, and the other
libraries the app needs.

## 4. Run

```bash
python run.py
```

A splash screen appears while the app loads, then the main window opens.

## 5. Add your first account

1. Open **Settings** (gear icon) → **Accounts**.
2. Click **Add account**.
3. Choose your provider:
   - **Gmail** or **Outlook** — sign in through the secure provider window.
   - **Other (IMAP/SMTP)** — enter your email, password, and the incoming
     (IMAP) and outgoing (SMTP) server details from your provider.
4. Save. The app begins syncing in the background; your folders and messages
   fill in as they download.

You can add more accounts at any time; each one appears in the sidebar with its
own colour and groups.

## Tips

- **Background mode:** closing the main window keeps the app running in the
  system tray so mail keeps syncing. Use the tray menu's **Quit** to exit fully.
- **Themes:** switch between Light, Dark, and System under
  **Settings → Appearance**.
- **Backup:** create a local backup of your mail and settings from
  **Settings → Backup**.

## Troubleshooting

- **The window doesn't appear / closes immediately** — run `python run.py` from
  a terminal to see any error output, and confirm `pip install -r requirements.txt`
  completed without errors.
- **"Certificate expired" or TLS errors** — the app ships a current CA bundle;
  make sure dependencies installed cleanly (especially `certifi`).
- **A mailbox won't connect** — double-check the IMAP/SMTP host, port, and that
  your provider allows app access (some require an app-specific password).
