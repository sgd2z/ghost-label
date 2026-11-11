# Source rules

These files are the app's judgment. The code never decides anything about genres, moods,
languages, styles or wording on its own. It collects data, sends the relevant rule file plus that
data to your AI provider, and stores what comes back.

Edit them in Settings › Rules (your copy is saved in the app's data folder under `Rules/`, and
"Reset" brings back the bundled version). They are plain text, written for the model.

| File | Used when |
|---|---|
| general.md | Every writing call (prepended) |
| taste.md | Onboarding questions and the taste profile |
| roster.md | Planning which new artists to invent |
| artist.md | Writing an artist's dossier |
| discography.md | Planning an artist's releases |
| album.md | Writing one album's tracklist and liner notes |
| song.md | Writing one song's lyrics and the style text the music model hears |
| artwork.md | Briefs for covers and press photos |
| links.md | Analysing pasted links and writing a style brief |
| mashup.md | Planning a mashup song from pasted links (and each new take) |
| listening.md | Updating taste from favorites, plays and skips |
