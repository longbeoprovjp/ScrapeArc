# Changelog

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
