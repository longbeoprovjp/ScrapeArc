# Changelog

## Unreleased

- **Needs tags** filter: posts with no tag besides upload.
- **Sure duplicates** also clears a single picture that is already one of the pictures of a post with several files
  (that post is kept whole).
- Tags page: explanations and lists in boxes, like the Scrape and Stats pages.
- **Duplicate review** lists every picture of both posts; click one to see it big. It opens on the two closest
  pictures, so a single picture's match inside a post with several files is in front of you.
- The image now uses Python 3.15 (was 3.12) and FastAPI 0.163.1; build tooling updated.
- **Send from the iPhone's Photos app**: a page linked from the upload window sets up an Apple Shortcut in the share
  sheet. It uses a private upload key (only allows adding files; make a new one any time) instead of the login.
- iPhone **HEIC photos become JPEG** on upload, so every browser can show them.
- **Select posts → Edit tags**: add and/or remove tags on all selected posts at once, with suggestions while typing.
- **Locked-drive safety**: with `MINIBOORU_DATA_REQUIRED=1` the app won't start a new, empty archive in a data
  folder whose encrypted drive isn't unlocked yet; it shows a "data is locked" page instead.
- Docs: unlocking the encrypted drive with the TPM chip.
- **Sure duplicates**: clear near-certain look-alike pairs at once (single pictures, same shape, almost identical)
  after looking at a sample; the bigger picture is kept. Videos, multi-file posts and less certain pairs stay for review.
- Resolving a duplicate keeps the copy's scrape record and remembers its file, so a rescan doesn't save it again, and the
  **upload** tag no longer moves onto a post that wasn't uploaded (also for Select posts → Merge into one).

## 1.0.0

The first release.

- **Gallery** with tag search (`tag -tag type:video title:word`; words that aren't tags search titles), filter
  buttons, sorting by the date posted on the original site, posts per row, and multi-file posts.
- **Scraper** that works on many sites without site-specific code: keywords, start page, stop at a count or a date,
  auto rescans on a timer, single posts. Obeys robots.txt and waits between requests. A run cut off by a restart
  continues from the page it reached.
- **"Save to …" bookmark and toolbar button** for sites that need a login: your own browser downloads the post.
- **Duplicates**: copies of the same picture inside a post are dropped, look-alike posts are flagged, and a review page
  goes through them one pair at a time.
- **Tags**: your own tag types and colours, aliases, implications, blocked tags, tag suggestions while typing.
- **Phones**: one-row header, add to Home Screen (opens where you left off), separate phone background and layout.
- **Customize**: name, icon, backgrounds, ready-made colour setups that pass WCAG contrast checks, every box's colour.
- **Stats**: totals, storage and free space, posts per month, top tags.
- Ready-made Docker image for amd64 and arm64.
