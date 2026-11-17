# Resume Mailer

Local UI for sending your message and resume individually through Gmail SMTP. Python 3.10+; no runtime packages required.

## Start

Double-click `start.cmd` (or `start.bat`), or run `python app.py`. The UI opens at `http://127.0.0.1:8765`. Launching it again opens the existing app. Keep the terminal open while sending. After a code update, close the old terminal and launch again.

## Saved details

Sender address/name, SMTP settings, recipients draft, subject, message and resume are automatically saved after editing stops for about a second. Wait for **Saved locally** (shown at the bottom) before closing the tab. The resume is copied into the database, so you do not have to select it again.

With **Remember password on this PC** checked, the Gmail App Password is encrypted using [Windows DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata) for your Windows user. The UI shows a saved-password placeholder; it never fetches the saved password into the page. Leave the field blank to reuse it. Switching the sender, SMTP username or host clears the old credential. Uncheck Remember and save to remove the credential from the live database. Backups may still contain the encrypted old credential until they expire. Other settings and resumes are stored locally without encryption.

## Send

1. Enter your sender email and SMTP username. Gmail settings: `smtp.gmail.com`, port `587`, STARTTLS. Use a Google App Password with 2-Step Verification enabled; availability depends on your Google account settings.
2. Paste recipients separated by lines, commas, spaces or semicolons. Addresses are deduplicated case-insensitively.
3. Write your subject and plain-text message and attach a resume up to 10 MB.
4. Click **Review & send** to see the eligible/skipped counts, then **Send emails**. Test with your own address first.

Each recipient receives a separate message with the same resume. A 10-second delay is the default. **Stop queue** prevents subsequent sends; an email in progress may still finish. Authentication failures stop the queue. Provider quotas still apply.

## History and local storage

On Windows, all persistent data lives in `%LOCALAPPDATA%\ResumeMailer\send_history.sqlite3` (normally `C:\Users\YOUR_USER\AppData\Local\ResumeMailer\send_history.sqlite3`). The UI displays the exact path. Storage belongs to your Windows account and survives restarting the app, changing browser profiles, moving the code, or downloading a fresh project copy. It is not synced to GitHub or other computers. Keep the data folder when reinstalling the app.

The previous project-folder `send_history.sqlite3`, if present, is automatically imported without modifying it. Existing history in the new database is preserved; newer legacy sends are merged. An empty old database cannot reconstruct messages from Gmail Sent. Emails sent through a version without history tracking are not recoverable from this app.

Successful SMTP sends are committed immediately. Recipients are automatically skipped for **30 days** across restarts and sender accounts. Expired protection records are removed during history refresh and before sending. **Expiry never sends an email. Sending always requires your manual action.** The latest queue results also survive restarts.

Before attempting SMTP, the app commits a protective record. A crash or network timeout during sending leaves the recipient marked **Uncertain**, preventing an accidental duplicate. Check Gmail Sent to resolve what happened; the protection expires after 30 days. Definite failures, such as rejected credentials or recipients, do not start a cooldown. An interrupted queue is not resumed automatically. “Sent” means accepted by SMTP, not guaranteed inbox delivery. SMTP cannot provide exactly-once delivery through every crash or network failure.

Daily SQLite backup snapshots are kept beside the database, updated after a queue completes. Backups older than 30 days are removed at startup/queue completion. To restore a snapshot, close the app, keep a copy of the current database, and copy the selected `history-backup-YYYY-MM-DD.sqlite3` over `send_history.sqlite3` in the data folder. Restart; expired protection records will be pruned. Backups contain saved drafts, resume and the Windows-encrypted credential, so keep them private.

## Checks

`python -m unittest -v` runs storage, Windows password encryption, legacy migration, SMTP failure, cooldown, concurrency and localhost API tests. SMTP is mocked; no real emails are sent.

Optional browser regression check: install Playwright for development (`python -m pip install playwright`), ensure Chrome is installed, then run `python check_ui.py`. This uses an isolated Chrome session and temporary database, tests autosave and a full server-process restart, and never connects to Gmail. Screenshots are written to the ignored `.qa` folder.
