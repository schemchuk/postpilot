# Publish through postpilot

Give postpilot a public GitHub release in your project's own repository. Postpilot
reads the release, applies the project's approval policy and publishes to Volodymyr's
accounts. Ask Volodymyr before the project's first release. Never handle social tokens.

## Register your project

Open a PR in postpilot to add an entry to `config/projects.toml`. Give Volodymyr
the public repository's `owner/name`, proposed project key, kinds, channels,
approval mode and daily limit. Get his OK before your first release. Adding an
`auto` project or widening an existing project's kinds, channels or daily limit
requires his explicit OK on the PR.

```toml
[projects.example]
repo = "owner/public-repo"
kinds = ["reel", "post"]
channels = ["linkedin-de", "threads"]
approval = "tap"
max_per_day = 1
enabled = true
```

The key must match `[a-z0-9][a-z0-9-]{0,39}` in full. Use a unique repository
(repository names are compared case-insensitively). Both lists must be non-empty
and contain distinct values. Use the kinds and channel names below. The last three
settings default to `tap`, `1` and `true`, respectively. A disabled project
stops intake and holds pending publication.

## Tag and manifest

Use a regular published release (neither draft nor prerelease). Tag it
`<kind>/<id>`, with kind `reel` or `post` as allowed by your project entry.
The ID must be 1–80 characters and match `[a-z0-9]+(?:-[a-z0-9]+)*` in full.
Use lowercase ASCII letters and digits in groups separated by single hyphens.
IDs are unique per project across both kinds.

Attach `manifest.json` as a UTF-8 JSON object of at most 16,000 bytes. Use
exactly the keys below. Duplicate keys and unknown keys are rejected.

| Field | Required | Type and rules |
| --- | --- | --- |
| `v` | Yes | Integer `2`. |
| `kind` | Yes | String `"reel"` or `"post"`. Must match the tag prefix and be allowed for the project. |
| `id` | Yes | String. Must match the tag suffix and follow the ID rules above. |
| `release_at` | Yes | ISO 8601 datetime string with uppercase `T` and an explicit UTC offset. Use `Z` or an offset like `+01:00`. The parser also accepts signed hour-only and compact offsets. |
| `ai` | Yes | JSON boolean `true` or `false`. Set it to `true` for AI-generated media. |
| `channels` | No | Non-empty array of distinct channel name strings. Each must be allowed by the project and supported for the kind. If omitted, all project channels supported for the kind are selected. |

Required fields have no defaults. At intake, `release_at` may be up to 1 hour
in the past or 60 days ahead, inclusive. The window is measured against the
intake run's clock. Telegram shows the scheduled time in Berlin time.

| Kind | Channel names |
| --- | --- |
| `reel` | `linkedin-de`, `threads`, `instagram`, `facebook`, `youtube`, `tiktok` |
| `post` | `linkedin-de`, `threads` |

Paused channels are unavailable. LinkedIn UA is paused. Instagram and Facebook
publish Reels. YouTube publishes Shorts. TikTok sends a draft to the TikTok
inbox for manual completion.

## Release assets

Attach files with these exact names. Limits are inclusive and byte limits apply
to both the reported asset size and downloaded bytes, unless noted.

| Asset | Reel | Post | Intake limits |
| --- | --- | --- | --- |
| `manifest.json` | Required | Required | 16,000 bytes. See schema above. |
| `post_de.txt` | Required | Required | 12,000 bytes. Reel: 2,200 characters with any AI video label, even without Instagram. Post: 3,000 characters with any AI image label. |
| `post_threads.txt` | Optional | Optional | 2,000 bytes and 500 characters, including any AI image label on a post. |
| `video.mp4` | Required | — | 100,000,000 bytes. Must have a video stream and an MP4 duration that ffprobe can read. Duration must be 3–90 seconds. |
| `cover.png` | Required | — | 10,000,000 bytes. Must start with the PNG signature. Used as the preview and as a thumbnail where supported. |
| `image.png` or `image.jpg` | — | Optional | One image at most. 8,000,000 bytes. Must start with the matching PNG or JPEG signature. |
| `title_de.txt` | Optional | — | YouTube title override. Use UTF-8, at most 16,000 bytes and at most 100 characters after trimming. |

The legacy `reel_de.mp4` and `cover_de.png` names are for the factory only.

Both caption files must be UTF-8, contain non-whitespace text and have no NUL
characters. Whitespace and newlines count toward the limits. Postpilot does no
rewriting, shortening or truncation of captions. LinkedIn and YouTube apply
their channel-specific escaping. If `post_threads.txt` is absent, Threads uses
`post_de.txt` if it fits its 500-character limit with any applicable label.
Otherwise Threads is skipped and the Telegram intake notice gives the reason.
An invalid `post_threads.txt` rejects intake even if Threads is outside the
selected channels.

For YouTube, a title override reported as over 16,000 bytes, invalid UTF-8,
a NUL, an empty trimmed title or a title over 100 characters leads to the
default title. Without an override, YouTube tries the first line and then the
first sentence of the caption, up to 100 characters. At publication, the
YouTube description must fit 5,000 UTF-8 bytes after channel escaping and
any AI label. An oversized description fails that channel. A cover over
2,097,152 bytes skips the YouTube thumbnail upload.

### AI labels

- For a reel with `ai: true`, postpilot appends two newlines and
  `KI-generiertes Video / AI-generated video` to the LinkedIn, Instagram,
  Facebook and YouTube captions and to the caption supplied for the TikTok
  draft. This uses 43 characters of the budget, leaving 2,157 in
  `post_de.txt`. YouTube also sets its synthetic media flag. Reel text on
  Threads has no label added by postpilot.
- For a post with an image and `ai: true`, postpilot appends two newlines and
  `KI-generiertes Bild / AI-generated image` to LinkedIn and Threads text.
  This uses 42 characters, leaving 2,958 for `post_de.txt` and 458 for the
  Threads text, including when Threads uses the German caption as fallback.
- Text-only posts get no AI label. Setting `ai: false` adds no label.

## Intake and approval

`publish-reels` is scheduled every 15 minutes. Each run scans the first page
of up to 30 releases from each enabled project before it publishes due items.
Allow for GitHub Actions scheduling delays. Polling requires no postpilot
credential in your project. With existing permission to dispatch Actions in
postpilot, you can request an earlier scan of a single project:

```sh
gh workflow run project-intake.yml --repo schemchuk/postpilot -f project=example
```

Use your project key for `project`. The dispatch scans releases, takes no
content and does not publish. Telegram is where Volodymyr sees the result.
A successful dispatch alone does not confirm that a release was accepted.

- `tap`: Volodymyr gets a Telegram preview with the selected text, any
  cover or image, AI label status, scheduled time and Approve/Reject buttons.
  The item waits for approval, even after its scheduled time. Once approved,
  it is eligible in a publishing run at or after `release_at`. Reject skips it.
- `auto`: Get approval in your project before handing over the release.
  Postpilot takes the item in as approved and sends a Telegram notice without
  approval buttons. It becomes eligible at `release_at` if the project still
  allows auto approval.

`max_per_day` is a per-project integer of at least 1. It counts items across
both kinds by their first successful channel publication on a Berlin calendar
day. An item counts once. When the limit is reached, another approved item
waits until the next Berlin day. Telegram reports the hold once per item per day.

## Repeat, replace or fix a release

Keep the same ID for the same item. Postpilot skips release asset metadata it has
already processed. Re-uploading the same content with the same effective
manifest values leaves the item unchanged if the release still passes intake
validation, including the time window. Postpilot compares SHA-256 and size
for recognized content assets and compares the parsed schedule, AI flag,
title and stored channel selection. For reels, omitting `channels` follows
the current project allowlist, so switching between omission and an explicit
list can count as a change.

To fix an item, update the assets on the same release. A valid change can
replace the item until publishing starts. A replacement in `tap` mode needs
a new approval on the new preview. Replacement is refused after a channel has
published or has an active publishing claim, or YouTube has a stored upload
reference. A published item cannot be replaced. A skipped item is refused if it reaches
validation again. Reusing the ID
with a different kind or approval mode is refused.

Keep the release and its assets available during publishing. Postpilot checks
the SHA-256 of assets it downloads again. It rechecks post text and image
assets, and checks reel media when it fetches it. Reel captions are taken from
intake. A missing release or required asset, or a hash mismatch on a new upload
path, can fail publication and produce a Telegram Retry/Skip prompt.

## What intake rejects

For a release it inspects, intake rejects an invalid manifest, tag mismatch,
invalid ID or release time, invalid channel choice, or a post with no allowed
channels. It also rejects missing required assets, both post images together,
invalid asset sizes or download URLs, disallowed redirects or more than five
redirects, invalid captions or media, and refused replacements.

Telegram reports `Project <key> <tag> rejected: <reason>`. Postpilot records
the rejection once per project, tag and asset metadata fingerprint. Fix and
re-upload the affected asset to get another scan. HTTP download failures are
logged and retried on later scans. They do not record a permanent rejection.

Unregistered and disabled projects are outside polling. Drafts, prereleases,
tags with unrelated or disallowed prefixes, and releases without
`manifest.json` are skipped. Malformed GitHub release or asset metadata,
including duplicate asset names, is logged and skipped. These skips do not
send a release rejection to Telegram.

## Create a release

Run these examples from the source repository with GitHub CLI access to create
releases. Prepare the named content files first. Each example writes its own
`manifest.json` with a release time one hour ahead in UTC. The examples use
the channels in the sample allowlist. Set `ai` to match your media.

### Reel

```sh
release_at=$(python3 -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds"))')
cat > manifest.json <<EOF_MANIFEST
{"v":2,"kind":"reel","id":"demo-walkthrough","release_at":"$release_at","ai":true,"channels":["linkedin-de","threads"]}
EOF_MANIFEST
gh release create reel/demo-walkthrough manifest.json video.mp4 cover.png post_de.txt \
  --title "Demo walkthrough" --notes "Postpilot reel"
```

### Post with an image

```sh
release_at=$(python3 -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds"))')
cat > manifest.json <<EOF_MANIFEST
{"v":2,"kind":"post","id":"demo-update","release_at":"$release_at","ai":false,"channels":["linkedin-de","threads"]}
EOF_MANIFEST
gh release create post/demo-update manifest.json post_de.txt image.png \
  --title "Demo update" --notes "Postpilot image post"
```

## Block for your AGENTS.md

```markdown
To publish, build a release per the [postpilot contract](https://github.com/schemchuk/postpilot/blob/main/docs/publishing-contract.md); never handle social tokens; ask Volodymyr before the first release of a project.
```
