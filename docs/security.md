# Security

ScrapeArc is meant for **one person or a household on a private network**, optionally reached through Tailscale. It
is not built to face the open internet.

[← Back to the README](../README.md)

## What you should do

1. **Set a long `MINIBOORU_PASSWORD`.** Without a password there is no login at all: the app warns about this when it
   listens beyond the local machine. It refuses to start with the example password.
2. **Don't port-forward it to the internet.** Use Tailscale (see [remote-access.md](remote-access.md)) to reach it from
   outside. Don't use Tailscale Funnel.
3. **Use HTTPS when you're away from home.** Plain `http://` sends the password unencrypted. That's acceptable inside
   your home network, and Tailscale encrypts the connection either way.
4. **Keep `.env.minibooru` and the data folder private.** They're gitignored; never commit them.
5. **Update** with `git pull && make up` now and then, and keep Docker and the host updated.

## How it is protected

| Area | What the app does |
|---|---|
| Login | One password (user `admin`), compared in constant time. After 5 wrong attempts in 5 minutes, logins are refused for a while. |
| Sessions | An HMAC-signed cookie (`HttpOnly`, `SameSite=Lax`) that lasts 30 days. The key is random per install and mixed with the password, so changing the password logs every device out. |
| Every page and file | Requires login, except the login page, the app icon and the static files. |
| Forged requests from other sites (CSRF) | POST requests from another origin are refused, and the cookie isn't sent on cross-site requests. |
| Clickjacking | Pages can't be shown inside another site's frame (`X-Frame-Options: DENY`). |
| Stored files | Only pictures and videos are accepted. SVG is refused because it can carry script. Files are served with `nosniff` and a sandbox policy, so a stored file can never run code in the app. Backgrounds and icons are re-encoded. |
| Scraped pages | Page content is only parsed, never run. Text from sites is escaped when shown. |
| Server-side requests | The scraper refuses private and internal addresses (your router, other machines at home), obeys robots.txt, and limits its request rate. |
| The bookmark | The page talks only to the window it opened, and the window only to the page that opened it (both check origin). The site's files are downloaded by your browser, and your site password never reaches the app. |
| Links out | `Referrer-Policy: same-origin`, so sites you click through to don't learn your app's address. |
| No phoning home | The app contacts nothing except the sites you tell it to scrape: no telemetry, no update checks, no third-party scripts or fonts. |
| Container | Runs as your own user id (not root), with the data in one folder. |

## Known limits

- There is one account. Everyone with the password can do everything, including deleting.
- There is no two-factor login. Rely on a long password plus Tailscale.
- Colors you pick in Customize are stored in each browser. Name, icon and backgrounds are stored on the server.
- The scraper's HTML detection is heuristic: always check what a new site imports with a small run first.

## Reporting a problem

If you find a security issue, please open a GitHub issue without exploit details and ask for a private contact, or use
GitHub's "Report a vulnerability" button if it's enabled for the repository.
