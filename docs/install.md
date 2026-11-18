# Installing with Docker

Tested on Debian 12 and 13; any Linux with Docker works the same way, because the app runs inside its container.

[← Back to the README](../README.md)

Docker in one paragraph: an **image** is a frozen copy of the app and everything it needs. A **container** is a running
copy of an image. A **bind mount** is a folder on the real machine that the container can see. The container can be
thrown away at any time; the data folder can't. Everything you care about lives in that one folder
(`MINIBOORU_DATA_DIR`).

| File in the project | What it is |
|---|---|
| `Dockerfile` | Recipe for the image: Python 3.12 on Debian, plus ffmpeg and the app |
| `docker-compose.minibooru.yml` | How to run it: port, data folder, restart policy, user id |
| `.env.minibooru.example` | Settings template (copy it to `.env.minibooru`) |
| `healthcheck.py` | Lets Docker tell whether the server is alive |
| `Makefile` | Shortcuts: `make up`, `make down`, `make logs`, `make status`, `make test`, `make dev` |

## 1. Install Docker (once)

Paste the commands one line at a time. Long lines pasted over SSH can get split, which causes confusing
"command not found" errors.
```
sudo apt update
sudo apt install -y git make curl
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker      # start Docker at boot
sudo usermod -aG docker $USER           # use docker without sudo
```
Log out and back in (the group change only applies to new logins). Then check that `docker run --rm hello-world` and
`docker compose version` both work.

## 2. Get the project

**The easy way: the ready-made image.** Nothing to build; it runs on amd64 PCs and arm64 boards (Raspberry Pi 4/5).

```
mkdir -p ~/scrapearc/data && cd ~/scrapearc
curl -O https://raw.githubusercontent.com/longbeoprovjp/ScrapeArc/main/docker-compose.yml
nano docker-compose.yml        # set MINIBOORU_PASSWORD; change ./data or the user id if you like
docker compose up -d
```
Open `http://<this-machine>:8090` and log in as `admin`. That's all: skip to [step 5](#5-day-to-day). Update later
with `docker compose pull && docker compose up -d`.
The settings table below explains the same options; in this file they are written directly under `environment:`,
`ports:` and `volumes:`.

**From the source code** (to change the code, or if you prefer building it yourself):

```
git clone https://github.com/longbeoprovjp/ScrapeArc.git ~/scrapearc
cd ~/scrapearc
```

## 3. Configure

```
mkdir -p ~/scrapearc-data            # or a folder on a big disk, e.g. /mnt/hdd/scrapearc
cp .env.minibooru.example .env.minibooru
nano .env.minibooru
```

| Setting | What to put |
|---|---|
| `MINIBOORU_PASSWORD` | A long password. The app refuses to start with the example value. |
| `PUID` / `PGID` | The output of `id -u` and `id -g` |
| `MINIBOORU_DATA_DIR` | The folder you made above, as an absolute path. **It must start with `/`**: without it, Docker reads it as a volume name and fails with `refers to undefined volume`. |
| `BIND_ADDR` | `0.0.0.0` for your whole home network, `127.0.0.1` if only Tailscale (see [remote access](remote-access.md)) should reach it |
| `MINIBOORU_PORT` | `8090` unless that port is taken |
| `MINIBOORU_SLEEP` | Seconds between scraper requests (default 2). Please don't go lower. |

Keep comments on their own lines. This file is gitignored: never commit it.

Optional: to keep the archive on an encrypted disk, follow [encrypted-drive.md](encrypted-drive.md) first.

## 4. Build and start

```
make up          # docker compose -f docker-compose.minibooru.yml --env-file .env.minibooru up -d --build
make status      # STATUS should become "healthy"
make logs
```
Open `http://<server-ip>:8090`, log in as `admin` with your password, then choose the app's name and icon. You can
change both later in **Customize**.

Because of `restart: unless-stopped` and `systemctl enable docker`, the app comes back by itself after a reboot.
If the machine runs `ufw`: `sudo ufw allow from 192.168.0.0/16 to any port 8090 proto tcp` (use your LAN range).

Handy alias:
`alias mb='docker compose -f ~/scrapearc/docker-compose.minibooru.yml --env-file ~/scrapearc/.env.minibooru'`,
then `mb ps`, `mb logs -f`, `mb restart`.

## 5. Day-to-day

| Task | Command |
|---|---|
| Update to a newer version | `cd ~/scrapearc && git pull && make up` (this stops a running scrape: wait for it, or restart it after) |
| Restart / stop / start | `mb restart` / `mb stop` / `mb start` |
| Logs | `mb logs -f --tail=100` |
| Remove the container (data stays) | `make down` |
| Shell inside | `mb exec minibooru bash` |
| How much is stored | `du -sh <data dir>/files` |
| Remove extra copies inside posts (preview, then apply) | `mb exec minibooru python -m minibooru dedupe-media`, then add `--apply` |
| Set the name/icon from the command line | `mb exec minibooru python -m minibooru brand --name "My Archive" --icon /data/icon.jpg` |

The data folder contains `db.sqlite` (posts, tags and history), `files/<2 chars>/<sha256>.<ext>` (the originals,
named by their content), `thumbs/` (previews), `backgrounds/` and `branding/`.

## 6. Backups

The whole archive is `MINIBOORU_DATA_DIR`. First make a consistent copy of the database while the app runs, then copy
the folder:
```
mb exec minibooru python -c "import sqlite3; s=sqlite3.connect('/data/db.sqlite'); d=sqlite3.connect('/data/db.backup.sqlite'); s.backup(d)"
rsync -av <data dir>/ /path/to/backup/scrapearc/
```
To run it daily, add both lines to `crontab -e`. To restore, put the folder back and start the container.

Moving an existing archive to a new machine: stop the app, then `rsync -av <old data dir>/ user@newhost:<new data dir>/`.

## 7. Proxmox LXC

On the Proxmox host, for the container (ID 101 here):
```
pct set 101 -features nesting=1,keyctl=1                 # Docker inside LXC needs both
pct set 101 -mp0 /mnt/hdd/scrapearc,mp=/mnt/scrapearc    # bind-mount a host folder for the data
```
Use an **unprivileged** container. File ownership is shifted inside it: uid 1000 in the container is 101000 on the host.
So run `chown -R 101000:101000 /mnt/hdd/scrapearc` on the host. Inside the LXC, set `MINIBOORU_DATA_DIR=/mnt/scrapearc`,
`PUID=1000` and `PGID=1000`. Run `pct reboot 101` after changing features, then follow steps 1 to 4 inside the container.

## 8. Troubleshooting

- **`permission denied` writing `/data`**: `PUID`/`PGID` don't own the data folder. Fix it with `sudo chown -R PUID:PGID <data dir>`.
- **"port already in use"**: change `MINIBOORU_PORT`.
- **`unhealthy`**: read `mb logs`.
- **"MINIBOORU_PASSWORD is still the example value"**: set your own password in `.env.minibooru`.
- **SSH says "REMOTE HOST IDENTIFICATION HAS CHANGED"** after you reinstalled the server: run `ssh-keygen -R <server-ip>` on
  your computer. If you did *not* reinstall anything, find out why before connecting.
- **`make status` hangs after a reboot** with the encrypted drive: see [encrypted-drive.md](encrypted-drive.md#troubleshooting).
- **iPhone HEIC photos** aren't accepted by the uploader. Export them as JPEG.
