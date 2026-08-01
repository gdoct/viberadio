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

## Persona                    <- who the DJ is, as a character
Sixty-one and on this frequency since seventy-eight...

## Bits                       <- optional: what they keep coming back to
His ex-wife Deborah, who he insists he is over...

## Catchphrase                <- a tic, slipped in about once an hour
Where your best memories happen

## TTS voice                  <- a Kokoro voice id, e.g. am_onyx, af_bella
am_onyx
```

Everything except `Bits` is required. `Style`, `Persona`, `Bits` and `Catchphrase`
end up verbatim in the DJ's prompts, so write them the way you would brief an
actor, not the way you would write a casting note.

**`Persona` is a character, not a voice.** "Male, enthusiastic, dark voice" tells
the model nothing it can act with, and you get a neutral narrator reading song
titles. Give the DJ an age, a history at this station, a temper, an opinion about
the audience, something they are wrong about. Timbre is already handled by
`TTS voice`.

**`Bits` are the running gags** — people, grudges and objects the DJ can return to
weeks apart. The voice agent feeds the DJ their last few breaks back, so anything
listed here can turn into a bit that pays off later instead of a one-off joke. A
station with no `Bits` section still works; it just has less to call back to.

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
