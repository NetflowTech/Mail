# Architecture

A high-level map of how AMail is put together, to help you find your way
around the code.

## Overview

AMail is a **Qt (PySide6) desktop application** with a local **SQLite**
database and a **threaded background sync** layer. The UI never talks to the
network directly: workers run on background threads and hand results to the UI
through Qt signals, so the interface stays responsive while many mailboxes sync.

```
          ┌──────────────┐      signals      ┌─────────────────┐
  IMAP ──▶│  sync engine │ ────────────────▶ │   UI (Qt)       │
  SMTP ◀──│  + workers   │ ◀──────────────── │  main window /  │
          └──────┬───────┘   commands        │  dialogs        │
                 │                            └─────────────────┘
                 ▼
          ┌──────────────┐
          │  SQLite DB   │   (messages, accounts, folders, settings)
          └──────────────┘
```

## Startup

`run.py` calls `emailclient.app.main()`, which:

1. Installs logging and a hang watchdog.
2. Creates the `QApplication`, applies the theme, and shows a splash screen.
3. Opens the database, starts the sync engine and the outbox worker.
4. Builds and shows the main window.

## Core modules (`emailclient/`)

| Module | Responsibility |
|---|---|
| `app.py` | Application bootstrap and `main()` entry point. |
| `config.py`, `paths.py` | App constants and per-user data/file locations. |
| `db.py` | All SQLite access: accounts, folders, messages, outbox, settings. |
| `models.py` | Core data types (`Account`, `Message`, …). |
| `credentials.py` | Stores account secrets in the OS keychain. |
| `mailparse.py`, `eml.py` | Parse incoming messages and `.eml` files. |
| `smtp_sender.py` | Build MIME and send outgoing mail over SMTP. |
| `backup.py` | Local backup and restore. |
| `spell.py` | Spell-checking for the composer. |
| `conntest.py` | Validate IMAP/SMTP server settings when adding an account. |
| `notify.py` | Desktop notifications for new mail. |
| `updater.py` | Check for and apply application updates. |
| `oauth.py`, `google_oauth.py` | OAuth sign-in for Gmail / Outlook. |

## Background work (`emailclient/sync/`)

- **Sync engine** — orchestrates per-account IMAP workers that fetch folders and
  messages and write them to the database.
- **Outbox worker** — picks up queued outgoing messages, sends them over SMTP,
  retries on failure, and saves a copy to the Sent folder.

Both communicate with the UI exclusively through Qt signals.

## User interface (`emailclient/ui/`)

The `ui/` package holds the main window and every dialog and widget: the sidebar
and message list, the reader (HTML view via QtWebEngine), the composer, the
account settings, and the settings dialog (notifications, appearance, backup,
feedback). Rendering is kept consistent across platforms with a shared Qt
stylesheet and a light/dark theme system.

## Data & storage

- **Database:** a single SQLite file under the per-user data directory.
- **Settings:** small key/value store via Qt's `QSettings`.
- **Secrets:** account passwords and tokens live in the OS keychain, never in
  the database or settings.

See the [README](../README.md) for the exact data-directory locations per OS.

## Threading model

- The Qt event loop runs on the main thread (all UI).
- IMAP sync and SMTP sending run on worker threads.
- Results and progress flow back to the UI through signals; the UI issues
  commands (sync now, send, delete, …) back to the workers. This separation is
  what lets the app stay smooth with many accounts syncing at once.
