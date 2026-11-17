# HTTPS and access from anywhere (Tailscale)

By default the app is reachable as plain `http://` on your home network. [Tailscale](https://tailscale.com) adds two
things: you can reach the app from your phone or laptop anywhere, and you get a real HTTPS address with a valid
certificate. Only devices on your own tailnet can open it; it is not public.

[← Back to the README](../README.md)

## Setup

1. Install Tailscale on the server and on each device you want to use, and log in to the same account on all of them:
   `curl -fsSL https://tailscale.com/install.sh | sh`, then `sudo tailscale up`.
2. In the Tailscale admin console, open **DNS**. Turn on **MagicDNS** and **HTTPS Certificates**.
3. On the server, allow your user to manage Tailscale without sudo (once):
   ```
   sudo tailscale set --operator=$USER
   ```
4. Serve the app over HTTPS:
   ```
   tailscale serve --bg 8090
   ```
   This prints the address, for example `https://myserver.tail1234.ts.net`. It keeps working after a reboot.
   Check it with `tailscale serve status`, and turn it off with `tailscale serve --https=443 off`.

The first visit can take a few seconds while the certificate is issued.

## Optional: only allow Tailscale

To stop other devices on your home network from reaching the app directly, set `BIND_ADDR=127.0.0.1` in
`.env.minibooru` and run `make up`. Tailscale serve still reaches it, because it runs on the same machine.

## Notes

- **Get the "Save to …" bookmark again from the https address.** A bookmark is tied to the address it came from (see
  [bookmark.md](bookmark.md)).
- If a device can't resolve the `.ts.net` name, check that MagicDNS is on for it: `tailscale status` should list the
  server. Some Linux setups need `systemd-resolved` for MagicDNS.
- Don't use Tailscale **Funnel** for this app: Funnel publishes it to the whole internet.
