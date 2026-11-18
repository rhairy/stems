# stems background services (local only)

These services are meant to run **locally on your own Mac** and be used only
**from that Mac**, at http://localhost:8765. The web server listens on
127.0.0.1 and refuses any client that isn't localhost.

Two launchd **login agents** run as you and start whenever you log in:

- **`com.stems.web`** serves `~/Music/Stems` at http://localhost:8765. You can
  play each stem in the browser, download a whole song as one `.zip`, or paste a
  YouTube link to queue a job.
- **`com.stems.worker`** runs queued jobs one at a time with the normal `stems` command.

## Install

```sh
cd ~/repo/stems
./install.sh                    # sets up the Python environment
./daemon/install-daemons.sh     # installs and starts the login agents (no sudo)
```

Open http://localhost:8765. You can run the installer again at any time; it
replaces the old agents. Use `--out DIR` to write somewhere other than
`~/Music/Stems`.

## Using it

- **Browser:** go to http://localhost:8765 and paste a link. The page refreshes
  while the job runs.
- **Command line:** `stemsd submit "https://www.youtube.com/watch?v=..."` queues a
  job. Plain `stems URL` still runs right away in your terminal.

### Job API (for a local script or bot)

```sh
curl -s -X POST http://localhost:8765/api/jobs \
     -H 'Content-Type: application/json' \
     -d '{"url": "https://www.youtube.com/watch?v=...", "two_stems": false, "mp3": false}'
# -> {"id": "20261118-...", "status": "pending", "status_url": "http://localhost:8765/api/jobs/20261118-..."}

curl -s http://localhost:8765/api/jobs/20261118-...
# -> {"status": "done", "link": "http://localhost:8765/strutter/", "zip": "http://localhost:8765/strutter.zip", "files": [...]}
```

`status` moves from `pending` to `running` and ends at `done` or `failed`.

## GPU

The worker checks the GPU (MPS) at startup and logs `gpu-check: ...`. If a job
fails, it retries that job on the CPU. CPU is slower but gives the same result.

## Managing the services

```sh
tail -f ~/Library/Logs/stems/worker.log
launchctl kickstart -k gui/$(id -u)/com.stems.worker     # restart
launchctl print gui/$(id -u)/com.stems.web | head -30    # status
./daemon/install-daemons.sh --uninstall                  # remove
```

After updating the code, run `uv sync` and then `./daemon/install-daemons.sh` again.
