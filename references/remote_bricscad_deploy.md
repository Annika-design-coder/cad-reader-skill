# Remote BricsCAD Service Deployment

Use this reference when setting up the Linux cloud CAD reader.

## Server Files

Place this skill folder on the Linux server, for example:

```bash
/opt/cadlist/cad-reader-skill
```

Required files:

```text
scripts/remote_bricscad_server.py
scripts/remote_bricscad_export.lsp
scripts/remote_bricscad_doctor.py
assets/systemd/cadlist-remote-bricscad.service
```

## Verify Prerequisites

```bash
python3 /opt/cadlist/cad-reader-skill/scripts/remote_bricscad_doctor.py \
  --port 8765 \
  --token <generate-a-long-random-token> \
  --public-url https://<your-cad-reader-host>
```

If BricsCAD is installed somewhere else, pass:

```bash
--bricscad /real/path/to/bricscad.sh
```

## Install Systemd Service

Review and adjust these paths in `assets/systemd/cadlist-remote-bricscad.service` before installing:

```text
WorkingDirectory=/opt/cadlist/cad-reader-skill
Environment=BRICSCAD_CMD=/opt/bricsys/bricscad/v26/bricscad.sh
Environment=BRICSCAD_SERVER_HOST=127.0.0.1
Environment=BRICSCAD_SERVER_PORT=18765
Environment=BRICSCAD_SERVER_TOKEN=<generate-a-long-random-token>
Environment=BRICSCAD_LISP_PATH=/opt/cadlist/cad-reader-skill/scripts/remote_bricscad_export.lsp
ExecStart=/usr/bin/python3 /opt/cadlist/cad-reader-skill/scripts/remote_bricscad_server.py
```

Install:

```bash
sudo cp /opt/cadlist/cad-reader-skill/assets/systemd/cadlist-remote-bricscad.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cadlist-remote-bricscad
```

Check status and logs:

```bash
sudo systemctl status cadlist-remote-bricscad --no-pager
sudo journalctl -u cadlist-remote-bricscad -n 100 --no-pager
```

## Required Health Results

On the server, the internal CAD reader should be local only:

```bash
curl -s http://127.0.0.1:18765/health
curl -s http://127.0.0.1:18765/ready
```

Nginx should expose public entrypoints and proxy to `127.0.0.1:18765`:

```text
public :80   /cad/ -> 127.0.0.1:18765
public :8765 /cad/ -> 127.0.0.1:18765 fallback during transition
```

From a client:

```bash
curl -s http://<server-ip>:8765/health
curl -s http://<server-ip>:8765/cad/health
curl -s http://<server-ip>/cad/health
```

Expected: JSON response. If TCP connects but HTTP returns empty reply or connection reset, the port is not served correctly by `remote_bricscad_server.py` or a firewall/proxy is dropping HTTP.

## Common Fixes

- Port open but `/health` empty reply: stop the wrong process on 8765 and restart `cadlist-remote-bricscad`.
- `/ready` says `bricscad_exists=false`: fix `BRICSCAD_CMD`.
- `/ready` says `lisp_exists=false`: fix `BRICSCAD_LISP_PATH`.
- BricsCAD fails headless: install `xvfb` and keep `BRICSCAD_USE_XVFB=auto`.
- Token mismatch: align `BRICSCAD_SERVER_TOKEN` on server and `CADLIST_REMOTE_BRICSCAD_TOKEN` on client.
- Service dies after request: inspect `journalctl -u cadlist-remote-bricscad`.

## Client Acceptance Test

After `/health` and `/ready` return JSON:

```powershell
python scripts\test_cad_reader_tool.py --file "<sample.dwg>" --bbox XMIN YMIN XMAX YMAX
```
