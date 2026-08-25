# Scripts

## `Start.bat`

Double-click it. That is the whole workflow.

It sets up the environment on first use (creates the virtualenv, installs dependencies,
downloads the browser), stops any portal still running from an earlier launch so the port
is free and the current code runs, starts the portal (`http://127.0.0.1:8000`, or the next
free port if 8000 is taken — the exact address is printed in the window), and opens it in
your browser. Keep the window open while you work.

If port 8000 is unavailable even after the old portal is stopped — Windows can bind-reserve
it for Hyper-V / WSL / Docker, with nothing to kill — the portal simply moves to the next
free port and opens the browser there. Nothing to do.

Collect from the **Runs** page. If the site needs a sign-in, a browser window opens for you
— sign in there, solving the CAPTCHA yourself, and collection continues by itself.

## `Check.bat`

Double-click it when the portal will not start. It checks everything at once — whether the
code on disk is current, whether port 8000 can actually be bound, what holds it, whether
Windows has reserved it, and what processes are left over — and ends with a verdict naming
the fix. It only reads: it kills nothing and changes nothing.

The check exists because three unrelated faults all surface as the same `WinError 10048`,
and one of them — a working copy older than the fix — is invisible from the error message
and cost the most time to find.

## `internal/`

Machinery `Start.bat` calls. Nothing here needs to be run by hand.

| File | What it is |
|---|---|
| `_ensure-env.bat` | Creates the virtualenv and installs dependencies, once. |
| `_stop-portal.bat` | Kills a portal or collection browser left over from an earlier launch, so a fresh start binds the port and runs current code. Safe when nothing is running. |
| `doctor.py` | The checks behind `Check.bat`. Read-only; runs on a bare interpreter, so it still works when the virtualenv is the thing that is broken. |
| `serve.py` | Portal entry point. Takes `--host`, `--port`, `--reload`. |

If you do run `serve.py` from a terminal, use the project's interpreter rather than the
system one — it says so itself if you get that wrong:

```
.venv\Scripts\python.exe scripts\internal\serve.py --port 9000
```
