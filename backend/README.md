# AMail admin backend

Lets an administrator sign AMail PCs in to company mailboxes and out again,
and see which mailboxes each PC is signed in to, without ever giving users
the mailbox passwords.

## How it works

- **Mailboxes** (email address, IMAP/SMTP servers and password) are added in
  the admin panel. Passwords are stored encrypted with a master key, and the
  panel never shows a password again once it is saved.
- **PCs** join with a setup code from the panel (AMail → Settings →
  Organization). AMail shows the server's address and asks the user to
  confirm before joining. Each PC makes its own key pair and shows a **check
  code**. To approve the PC, type that code into the panel: a request made by
  someone else with your setup code will have a different one.
- **Signing in:** pick a PC and a mailbox. At its next check-in (about every
  30 seconds) the PC receives the mailbox, with the password encrypted so that
  only that one PC can open it, and only for that mailbox's own servers.
  Pointing the account at another server, even by editing AMail's files,
  leaves the password locked. AMail uses it to log in, but never displays or
  exports it. The user can't change that account's servers or password, and
  can't remove the account. If the user already had that account themselves,
  AMail asks them before handing it over.
- **Signing out:** the mailbox is removed from the PC at its next check-in.
  A PC that is offline is signed out as soon as it comes back online. If you
  think the password may have leaked, change it at your mail host and enter
  the new one in the panel. PCs that are still signed in pick it up
  automatically, and the PC you signed out never gets it.
- **Blocking a PC** signs it out of every managed mailbox, and the PC
  disconnects from the organization.
- **7-day limit:** every check-in answer carries the server's time, signed by
  the server's own key. A PC whose last signed time is more than 7 days old
  stops using its managed mailboxes until it reaches the server again. The
  signature can't be faked by editing a setting.
- Users can still add their own personal accounts. The panel lists them per PC.

The password does reach the PC, encrypted to that PC's key, because AMail logs
in to the mail server itself. A determined, technical user with full control
of their PC could dig it out of memory. Change a mailbox's password when you
sign someone out of it for good.

## Moving existing accounts under the organization

People already using their company mailbox in AMail (with the password saved
on their PC) don't have to set anything up again. Once their PC has joined
and is approved, its page in the panel lists **Accounts the user added**.
Click **Take over** (or *Take over all password accounts*):

1. Within a minute, AMail on that PC asks the person to allow it. The
   question says what happens; they can say no.
2. On a yes, AMail sends the account's saved password and server settings
   to this server over the pinned connection. The server stores the password
   encrypted, the account becomes a company mailbox signed in on that PC, and
   the password is removed from the PC. Mail keeps syncing; Account Settings
   on the PC now says "Managed by your organization".
3. On a no, the panel shows *Declined*. Ask them, then try again, or click
   **Leave personal** to stop the panel reminding you about it.

The server only accepts a hand-over it asked for, so a PC can't push
mailboxes into the panel by itself. If the same mailbox is on several PCs,
the first hand-over creates it; taking it over on the next PC just signs
that PC in, keeping the password already stored.

Accounts that sign in with Google or Microsoft have no password to hand
over: add them under **Mailboxes** with the Google / Microsoft sign-in, then
sign the PC in. AMail replaces the person's own sign-in with the managed one
(after asking them).

## What users do with company mailboxes

The **Users** page lists, per PC and mailbox, what people did: emails
opened, sent and replied to, deleted or marked as spam, attachments opened
or saved, printed, marked read or unread, and when AMail was started or
closed. Each entry is a few words (the subject and the other party); email
contents are never recorded. Filter by PC, mailbox or day. A PC's own page
shows its last few entries.

Only mailboxes managed from this panel are recorded. Accounts people added
themselves (a personal Gmail, say) are never recorded, and the server
refuses entries for any address it doesn't manage. AMail's Organization
tab tells the user that this recording happens.

Entries reach the server with the PC's next check-in (within a minute while
it's online) and queue on the PC meanwhile. They are kept for 90 days
(`AMAIL_EVENT_DAYS` in `run_server.cmd` changes that) and then deleted.

## Admin panel security

- **Two-step sign-in is required.** The first sign-in shows a QR code to scan
  with an authenticator app (Microsoft Authenticator, Google Authenticator,
  1Password…). After that, each sign-in needs the password and the current
  6-digit code. Each code works once. Lost the phone? Another admin resets
  it on the panel's **Admins** page; with no other admin, run
  `python -m amail_admin create-admin NAME --reset-2fa` on the server.
- **More admins:** add, remove, or reset them on the **Admins** page. Each
  change asks for your own password again, so a panel left open can't be
  used to add an admin behind your back. There's always at least one admin,
  and you can't remove yourself.
- **Sessions are ended on the server.** Signing out, or changing the
  password, ends them everywhere. A session also ends after 30 minutes idle,
  or after 8 hours in total.
- **Who can reach it:** install with `-AdminNetwork` set to the admins' own
  PCs or subnet, for example `-AdminNetwork 192.168.1.50,192.168.1.51`. The
  firewall and the panel both enforce it. Keep guest Wi-Fi off that network.
- **No certificate warnings:** download the certificate from the panel's
  login page (or copy `C:\ProgramData\AMailAdmin\server.crt`). On each admin
  PC, double-click it → Install Certificate → Local Machine → "Trusted Root
  Certification Authorities". From then on, a certificate warning means
  something is wrong. Don't click past it.
- The **Activity** page logs every action. A warning banner appears after
  repeated failed sign-ins or wrong enrollment codes.

## Locking AMail to your server

Before building an AMail release, put your server's certificate fingerprint
(shown under Settings in the panel) into `PINNED_SERVER_FINGERPRINTS` in
`emailclient/org.py`. That build then refuses setup codes for any other
server, so nobody can talk a user into joining a fake organization. If you
leave it empty, AMail still shows the server address and asks the user to
confirm.

## Two ports

| Port | What | Who should reach it |
|------|------|---------------------|
| 8443 | Device API: PCs join and check in | Every AMail PC, including remote ones |
| 8444 | Admin panel | The office network only (the installer's firewall rule, and the panel itself, refuse other addresses) |

Both use HTTPS with the server's own certificate. PCs trust only that
certificate: its fingerprint is in the setup code. Your browser warns once
about the panel's certificate. That warning is expected.

### Remote PCs

PCs outside the office must reach port 8443. Choose one of these:

- **Port forwarding:** forward TCP 8443 on the office router to this server.
  Use your public IP, or a dynamic-DNS name, in the setup address
  (`https://your-name.ddns.net:8443`). Don't forward 8444.
- **VPN:** remote PCs connect to the office VPN, and the setup address is the
  server's office IP.
- **A tunnel or reverse proxy that has its own certificate** (for example
  Cloudflare Tunnel): set its address in Settings and untick "PCs trust only
  this server's own certificate". PCs then check the proxy's certificate the
  normal way. Also set `AMAIL_TRUSTED_PROXIES` (in `run_server.cmd`) to the
  proxy's address, e.g. `127.0.0.1`. The backend then logs, and rate-limits,
  each PC's real address instead of the proxy's. It reads that address from
  one header only: `X-Forwarded-For` by default (nginx, Caddy, most
  tunnels); for Cloudflare set `AMAIL_PROXY_HEADER=CF-Connecting-IP`. Only
  point a tunnel at port 8443, never at 8444.

## Install (Windows server)

1. Install Python 3.10 or newer from python.org. Tick "Add to PATH" and the
   `py` launcher.
2. Copy this `backend` folder somewhere permanent, e.g. `C:\AMailAdmin`.
3. Open PowerShell **as administrator** in that folder and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install_windows.ps1 -PublicUrl https://YOUR-ADDRESS:8443
   ```

   Add `-AdminNetwork <admin PC addresses>` to limit who can reach the
   panel. The script asks for the admin password, installs exact,
   checksum-verified library versions, locks the folder against changes by
   non-administrators, and runs the backend as the low-privilege LOCAL
   SERVICE account, starting with Windows.
4. Install the certificate on your admin PC (see *Admin panel security*),
   open `https://<server-ip>:8444`, sign in, and set up two-step sign-in.
5. **Mailboxes → Add a mailbox.**
6. **Settings:** copy the setup code. On each PC, paste it into AMail under
   **Settings → Organization → Connect**.
7. **PCs:** type in the check code that AMail shows on that PC, approve it,
   then sign it in to mailboxes.

## Backups

Everything lives in `C:\ProgramData\AMailAdmin`: the database, the
certificate, the session key, `master.key` and `signing.key`.

`master.key` and `signing.key` are locked to this machine by Windows. A copy
of the folder alone **cannot be opened on another server**. If `signing.key`
is lost, PCs reject the new server's answers until they're reconnected with a
new setup code. Once, after installing, run:

```powershell
$env:AMAIL_ADMIN_DATA = "C:\ProgramData\AMailAdmin"
.venv\Scripts\python.exe -m amail_admin export-key
```

Keep the printed key in a password manager. To move to a new server: install
there, stop the "AMail Admin" task, copy the data folder over (except
`master.key`), run `import-key` with the saved key, and start the task again.
Keep the same certificate files, or every PC will refuse the new server.

## Everyday commands

Run these in the backend folder, with `AMAIL_ADMIN_DATA` set as above:

| Command | What |
|---------|------|
| `python -m amail_admin create-admin NAME` | Add the first admin, or reset a password when no other admin can (the panel's Admins page does the rest) |
| `python -m amail_admin setup-code` | Print the setup code |
| `python -m amail_admin export-key` / `import-key` | Back up / restore the master key |

Logs go to `C:\ProgramData\AMailAdmin\server.log`. The panel's **Activity**
page records every sign-in, sign-out, approval and login.

## Development

```sh
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest==8.3.5
.venv/bin/python -m pytest tests
AMAIL_ADMIN_DATA=./data .venv/bin/python -m amail_admin init --url https://127.0.0.1:8443
AMAIL_ADMIN_DATA=./data .venv/bin/python -m amail_admin create-admin admin
AMAIL_ADMIN_DATA=./data .venv/bin/python -m amail_admin run
```
