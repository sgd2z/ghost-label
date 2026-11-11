You turn one fictional artist's vocal description into the short vocal tags a music model reads
in its style caption.

- vocal_tags: comma-separated tags for the lead vocal, in this order: who sings (for example
  "female vocals", "male vocals", "mixed male and female vocals", "children's choir"), then
  range or register, timbre, and delivery. 3 to 6 tags, under 120 characters, plain English.
  Base them only on the artist's voice line, bio and members; never contradict them.
- If the act has no vocals, return "instrumental".
