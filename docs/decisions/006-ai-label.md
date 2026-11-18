# 006. AI-generated label

Status: ACCEPTED (Volodymyr, 2026-11-18)

Decision: every HeyGen video is labelled as AI-generated on each channel: a visible burned-in text in
the video, the caption line "AI-generated avatar / KI-generierter Avatar", and the platform's own
AI label where the API supports it. The Channel protocol rejects a video item whose `ai_label` is false.
