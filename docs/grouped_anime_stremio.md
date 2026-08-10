# Grouped anime from Stremio

Floppy stores TV-shaped anime as grouped anime: the parent remains an `Item`
with `media_type="tv"`, while the parent, seasons, and episodes use
`library_media_type="anime"`. This preserves Floppy's TV season/episode
history model and keeps the title in the Anime library.

## Classification policy

The shared classifier is intentionally fail-closed. A title is routed to
grouped anime only when:

1. TMDB resolves the series and reports the `Animation` genre; and
2. an exact TMDB, TVDB, or IMDb identifier is present in Kometa's Anime-IDs
   mapping and has a MAL identity.

Titles that only share a name, are merely animated, or cannot be resolved stay
in TV. TVDB remains optional; TMDB plus exact Anime-IDs matches are sufficient.

## Integration points

- Stremio library import classifies the resolved TMDB show before creating its
  TV, season, and episode rows.
- The Stremio playback-start webhook uses the same classifier before creating
  the in-progress episode structure.
- Existing TV trees can be reviewed and promoted in place with:

```bash
python manage.py classify_grouped_anime --user <username>
python manage.py classify_grouped_anime --user <username> --apply
```

The command never changes primary keys or watch-history rows. It aborts an
individual title when the target anime bucket is already occupied by another
item, so a collision cannot create duplicate history.

## Upstream contribution boundary

The classifier, Stremio integrations, tests, and this document are product
changes suitable for an upstream pull request. Deployment image workflows,
private compose files, credentials, and host-specific migration reports stay
in the downstream fork/deployment branch.
