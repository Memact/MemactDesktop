---
name: content_query
triggers:
  - "where did i see"
  - "where did i read about"
  - "where did i see that"
  - "did i look at"
  - "did i read"
  - "what was that page about"
  - "what was that message about"
  - "i remember something about"
  - "i saw something about"
  - "that article"
  - "that message"
  - "that thread"
  - "that note"
  - "that docs page"
  - "that video"
  - "that post"
  - "that thing"
  - "remember"
filters:
  - content_match
priority: relevance
---
When this skill activates, the user is trying to find something
specific they read, saw, or encountered — a concept, topic, article,
video, or piece of information they only partially remember.

Match against extracted keyphrases and full article text in the
vector index. Prioritise semantic similarity over recency — the user
may be asking days or weeks after the event. Do not over-filter by
app or domain unless the query explicitly mentions one.

Return the top 3 most relevant events with source title, URL if
available, keyphrases (if present), and timestamp. Format as a
single sentence describing what was found, followed by supporting
evidence cards.
