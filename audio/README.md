# Reel audio

Royalty-free tracks. One is picked at random per Reel, from a random point
in the track, trimmed to 7s with a fade in and out.

Levels are handled automatically: each file is measured and gained to a
common target, so a quiet track and a loud one come out the same. Just drop
new files in -- `.mp3`, `.m4a`, `.wav`, `.aac`, `.ogg`, `.flac` -- and
delete any you no longer want. Nothing else to configure.

Long tracks are better than short ones: each Reel takes a different 7
seconds, so a three-minute track gives far more variety than a loop.

## What not to use

Commercial or chart music. Instagram's own catalogue is not reachable
through the publishing API at all, and a copyrighted track baked into the
file gets the Reel auto-muted or removed by Content ID -- the same video
also goes to X, which runs its own detection. The account takes the hit.

`tools/make_beds.py` can still synthesise filler tracks if the folder is
ever empty, but real music sounds better.
