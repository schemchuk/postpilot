# 007. AI client and humanizer

Status: ACCEPTED

Context: Weekly S1 drafts need style and factual checks before they enter the approval queue.
S2 Telegram texts follow the author-preservation rule in F2.2.

Decision: Isolate the Anthropic SDK in one Claude client behind the `TextModel` protocol.
For S1 only, require both deterministic checks and a structured model verdict to pass.
The humanizer allows up to three rewrite and review rounds and fails closed.

Consequences: Tests can use scripted models. Callers must complete the gate before creating
an item. Model failures or rejected drafts stop queue creation for the affected content.
