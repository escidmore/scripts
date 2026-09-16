# StoryGraph Import Audit

- StoryGraph export: `/Users/host/Downloads/d6f2a3c2c916d8ab42f8e70ec124f3995c403f883f4e9d902b0578234e390479.csv`
- Expected catch-up books: 52
- Correct audiobook edition and status: 35
- Wrong, missing, or formatless edition: 16
- Correct audiobook but wrong status/dates: 1
- Missing known start date: 46
- No correction required: 3

## Edition Or Status Repairs

- **Learning Curves**: select or create audiobook edition; mark read; set finish date; add start date (currently `paperback`, `to-read`)
- **Maybe Tomorrow I'll Know**: select or create audiobook edition; add start date (currently `digital`, `read`)
- **Save Scumming, Book 1**: select or create audiobook edition (currently `digital`, `read`)
- **Couriers Outbound**: select or create audiobook edition; add start date (currently `digital`, `read`)
- **Inkpot Gods**: select or create audiobook edition; add start date (currently `hardcover`, `read`)
- **Hench**: mark read; set finish date; add start date (currently `audio`, `to-read`)
- **Darksight Dare**: select or create audiobook edition (currently `no format`, `read`)
- **The Girl Who Bit Me**: select or create audiobook edition; add start date (currently `paperback`, `read`)
- **Heroics 101: A Superhero Slice-of-Life LitRPG**: select or create audiobook edition; add start date (currently `no format`, `read`)
- **Andy in the Apocalypse 2**: select or create audiobook edition; add start date (currently `paperback`, `read`)
- **Lost Souls and a Demoness 2**: select or create audiobook edition; add start date (currently `digital`, `read`)
- **Love, Gods and Sinners**: select or create audiobook edition; add start date (currently `digital`, `read`)
- **The Shape of Monsters**: select or create audiobook edition; add start date (currently `digital`, `read`)
- **Green Crime**: select or create audiobook edition; add start date (currently `digital`, `read`)
- **The Orb of Cairado**: select or create audiobook edition (currently `digital`, `read`)
- **Lancer: An Epic Sci-Fi Adventure**: select or create audiobook edition; add start date (currently `digital`, `read`)
- **Splinter Angel: Book 3**: select or create audiobook edition; add start date (currently `digital`, `read`)

## Already Complete

- The Duke
- Bone of My Bone
- Bi

## Files

- `storygraph-edition-status.tsv`: catalog/format/status repairs first.
- `storygraph-start-dates.tsv`: repetitive start-date corrections.
- `storygraph-repair.tsv`: combined queue.

The export contains no StoryGraph page IDs, so each row includes the best previously confirmed StoryGraph URL when available and a deterministic title/author search URL otherwise.
