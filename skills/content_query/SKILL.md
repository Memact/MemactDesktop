---
name: content_query
triggers:
  - "where did i see"
  - "did i look at"
  - "did i read"
  - "that article"
  - "that video"
  - "that post"
  - "that thing"
  - "remember"
filters:
  - content_match
priority: relevance
---
When this skill activates, the query likely describes something the user read or encountered - a concept, topic, or piece of information. Match against extracted keyphrases and full article text in the vector index. Prioritize semantic similarity over recency. Return the top 3 events with source title, keyphrases, and timestamp as a single sentence followed by supporting evidence.
