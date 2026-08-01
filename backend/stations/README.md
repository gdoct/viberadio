# The dial

One Markdown file per station. Each station runs its own agents, timeline and HLS
stream; they share the media library, so a track downloaded for one is instantly
available to all.

## File format

The file name is the station's identity, and the headings are its definition:

```markdown
# Goldie Oldie Rock KGOR      <- station name, on air and in the picker

## Style                      <- what the selector picks from
60s 70s rock

## DJ                         <- the DJ's name
Kyle

## Persona                    <- how the DJ sounds and talks
Male, enthusiastic, dark voice

## Catchphrase                <- dropped into breaks now and then
Where your best memories happen

## TTS voice                  <- a Kokoro voice id, e.g. am_onyx, af_bella
am_onyx
```

All six are required. `Style`, `Persona` and `Catchphrase` end up verbatim in the
DJ's prompts, so write them the way you would brief a real presenter.

## File names

`<order>-<slug>.md` — for example `01-kgor.md`:

- **order** sets the position on the dial. Renumber the files to reorder it.
- **slug** is the station's id: `/stream/kgor/playlist.m3u8`, `?station=kgor`, and
  its own `data/hls/kgor/` segment directory. Changing it makes a new station
  rather than renaming the old one, which keeps its rows and history.

## Adding, editing and removing

Drop a new file in, or edit one, and restart the backend — bootstrap applies the
files to the `channels` table on startup, so the file is the source of truth for
everything above. Seed the new station's library with:

```bash
uv run python -m viberadio.bootstrap --seed --station <slug>
```

Deleting a file stops bootstrap from maintaining that station, but its row stays
in the database and it stays on the dial. To retire one for good, delete the file
and then its row:

```bash
sqlite3 data/viberadio.db "DELETE FROM channels WHERE slug = '<slug>'"
```

Files starting with `.` or `_`, and this README, are ignored.
