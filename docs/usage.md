# Using the app

[← Back to the README](../README.md)

![Gallery](screenshots/gallery.jpg)

## First visit

Log in with user `admin` and the password from `.env.minibooru`. The first time, the app asks for a **name** (default
"ScrapeArc"), an optional **icon**, and optional **tag types** (see below). The name and icon show in the header
and the browser tab, and you can change them later in **Customize**. A new install starts with a plain dark blue
screen; add a background picture in Customize if you like.

## The gallery (front page)

- **Search** with tags separated by spaces. All of them must match.
  - `-tag` excludes a tag.
  - `type:video` or `type:image` shows only videos or only pictures.
  - `title:word` matches a word in the title.
  - Example: `tag1 tag2 -tag3 type:video`
- **Filter by**: everything, favorites, pictures, videos, or posts flagged as possible duplicates.
- **Show**: everything, only your uploads, or only scraped posts.
- **Sort**: newest or oldest first, by the date the post was made on the original site (the import time if the site
  gave none).
- Posts with several files show a count badge.

## Uploading

Open **Upload** at the top of the gallery, pick files, and type tags separated by spaces. Tick **as one post** to put
all the files in a single post; otherwise each file becomes its own post. Pictures and videos are accepted (not iPhone
HEIC: export those as JPEG).

## A post

![A post](screenshots/post.jpg)

- The box under the title shows when the post was made on its site, when you saved it, and a link to the original.
- **Tags**: click one to see every post with it. Edit the title and tags in **Edit**, then **Save tags**.
- **★ Favorite** adds it to the favorites filter.
- **Delete a single file** of a multi-file post: hover over it and press its delete button. A deleted file is never
  downloaded again by later scrapes.
- **Set as Background**: choose a picture, crop it to a desktop or phone shape, and save.
- **Possible duplicates**: if another post looks like the same picture, the post page says so. Choose **Keep both**,
  **merge** (moves the tags over and deletes this one), or delete the other.
- Use the arrow keys or the arrows on the page to move to the previous or next post in the same search.

## The Tags page

![Tags](screenshots/tags.jpg)

Tags are grouped by **type**, then by their first letter (A–Z, 0–9, and # for everything else); use the letter bar
to jump. Click a tag to see its type and post count, open it, change its type, or delete it.

To delete or change many tags at once, press **Select several**. Tick tags one by one, use the **all** button of a
letter, or use **Select all**, then use the bar at the bottom to change their type or delete them. You can also
block tags (they are never added by imports again), make aliases (two names for one tag), and implications
(tagging one tag automatically adds another).

### Tag types

You decide what kinds of tags you have, for example *person*, *artist* and *place*. Each type gets its own section
on the Tags page and its own colour on posts. **other** always exists: every tag without a type of its own goes
there, and on the Tags page it's folded away once you have other types.

Pick types on the first-run page, or at any time under **Tag types** at the bottom of the Tags page: add one, rename
it, change its colour, or delete it (its tags move to other; nothing is lost). Give a single tag a type from its
popup, or many at once with **Select several**. A tag that already has a type keeps it when a later import sees it as
other.

**Typing scraped tags by position.** Many sites list a post's tags in a fixed order, for example the author first,
then the people in the photo, then a general tag. Under **Scraping: type tags by their position** you can choose the
type for the *first* tag, the tags *in between*, and the *last* tag. The scraper and the Save bookmark then type tags
for you. Leave all three on *other* if your sites have no such order.

## Customize

![Customize](screenshots/customize.jpg)

Open **Customize** from the header:

- **Name and icon** of the app.
- **Backgrounds**: one for desktops and one for phones, cropped to the right shape. They're stored on the server, so
  every device you use gets them. **✕** removes one.
- **Customize screen →** opens a page for everything about how the app looks, with a live preview:
  - **Ready-made setups** (dark, light and high-contrast). Load one, then change anything. Every setup passes the
    WCAG readability minimums.
  - **Colours for every kind of box**: page, header, cards, info boxes, input boxes, main and other buttons, links
    and dates. Each text colour shows how readable it is on its box (aim for 4.5:1). Click a box in the preview to
    jump to its colours.
  - **Layout**: how much the background picture fades, corner roundness, thumbnail size and outlines around posts.

  Colours and layout are saved in that browser, so each device can look different.

## Tips

- On a phone, add the app to your home screen (Share → Add to Home Screen) to get its icon.
- Everything (posts, tags, history, backgrounds, name, icon) lives in the data folder; back it up (see
  [install.md](install.md#6-backups)).
