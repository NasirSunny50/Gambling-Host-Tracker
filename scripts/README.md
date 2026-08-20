# Scripts

## `Start.bat`

Double-click it. That is the whole workflow.

It sets up the environment on first use (creates the virtualenv, installs dependencies,
downloads the browser), starts the portal at `http://127.0.0.1:8000`, and opens it in your
browser. Keep the window open while you work.

Collect from the **Runs** page. If the site needs a sign-in, a browser window opens for you
— sign in there, solving the CAPTCHA yourself, and collection continues by itself.

## `internal/`

Machinery `Start.bat` calls. Nothing here needs to be run by hand.

| File | What it is |
|---|---|
| `_ensure-env.bat` | Creates the virtualenv and installs dependencies, once. |
| `serve.py` | Portal entry point. Takes `--host`, `--port`, `--reload`. |

If you do run `serve.py` from a terminal, use the project's interpreter rather than the
system one — it says so itself if you get that wrong:

```
.venv\Scripts\python.exe scripts\internal\serve.py --port 9000
```
