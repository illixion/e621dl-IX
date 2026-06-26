# e621dl

A small automated downloader for [e621](https://e621.net). It runs the searches
defined in `config.ini` and saves every matching post into a destination
folder such as the macOS Drop Box (`~/Public/Drop Box`). Uses the v2 post
API (`v2=true`).

## Usage

```sh
pip install -r requirements.txt
python3 e621dl.py
```

On first run a default `config.ini` is created next to the script.

## Configuration (`config.ini`)

```ini
[Settings]
download_directory = ~/Public/Drop Box
pool_directory = ~/Public/Drop Box/Pools   ; optional; default: <download_directory>/pools

[Defaults]
days = 1            ; how far back to search
ratings = s, q, e   ; allowed ratings
min_score = -100

[Blacklist]
tags = tag1, tag2   ; posts with these tags are skipped (wildcards allowed)

[Pools]
ids = 12345, 67890  ; download whole pools (ordered albums) by ID

[my search]         ; every other section is a search group
tags = fav:SomeUser
```

Each search group may override `days`, `ratings`, and `min_score`.

## Pools

Pools are ordered albums. List their numeric IDs in the `[Pools]` section and
each one downloads, in order, into its own subfolder under `pool_directory`
(default `<download_directory>/pools`), with files named `NNNN_<postid>.<ext>`
so they sort in album order. The blacklist still applies; date/score/rating
filters do not (a pool is downloaded in full). Posts in a pool that have since
been deleted from the site are reported as "unavailable" and skipped.

Unlike searches, pools dedup against the **live files on disk**: each run checks
which posts are already present in the album folder, so a file you delete is
re-downloaded on the next run, keeping the album complete. Pools are not tracked
in `database.txt`.

## Re-download behavior

Every search download is recorded in `database.txt`. Files you delete from the
download folder are **not** downloaded again — remove the corresponding post ID
from `database.txt` if you ever want a post re-fetched. (Pools are the
exception: see above — they always restore deleted files.)
