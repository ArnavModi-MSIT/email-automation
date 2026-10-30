# Resume Mailer

Local UI to send individual emails with a resume attachment using your SMTP account. Python 3.10+; no dependencies.

## Start

Double-click `start.bat`, or run `python app.py`. A browser opens automatically. The server binds only to `127.0.0.1` and chooses an available port. Keep its terminal open until the queue finishes.

## Send

1. Enter your sender email, SMTP username and password. For Gmail use `smtp.gmail.com`, port `587`, STARTTLS and an App Password (Google account → Security → 2-Step Verification → App passwords). Availability depends on account settings. Your regular Gmail password will generally not work.
2. Paste recipients separated by lines, commas, spaces or semicolons. Duplicate exact addresses are removed.
3. Enter a subject and plain-text message. Attach your resume, up to 10 MB.
4. Review the email, then click **Send emails**. Consider sending to your own address first.

Each recipient gets a separate message with the same attachment. The default delay is 10 seconds. Stop queue prevents subsequent sends; an in-progress SMTP operation may still complete. Authentication errors stop the queue. Other failures are shown per recipient while the queue continues.

Credentials and queue details are kept in process memory, not written to disk. The password field clears after starting; the worker releases its password reference when finished. Successful SMTP sends are saved in `send_history.sqlite3` next to the app. Recipients are matched case-insensitively and automatically skipped for 30 days, across restarts and sender accounts. Expired records are removed on startup, during history refresh, and before sending. The UI shows recent recipients and when they become eligible again. Emails sent before this feature was installed are not included. Failed attempts are not added to history. No automatic retry is provided. Restarting loses the current queue display, but preserves the database history. Avoid restarting and resending the whole list if some messages already succeeded. A network failure after SMTP acceptance can produce an uncertain result; verify your sent mail before manually retrying. “Sent” means accepted by SMTP, not confirmed inbox delivery.

Your provider's sending quotas and policies still apply. The app does not bypass limits. Use it for relevant outreach and honor requests to stop contacting recipients.

## Checks

Run `python -m unittest -v`. Tests use a mock SMTP connection and never send actual mail.
