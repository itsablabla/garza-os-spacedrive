---
id: CORE-016
title: "Slack Live API Adapter for Chat Archival"
status: In Progress
assignee: jamiepine
priority: High
tags: [core, archive, adapters, slack, chat]
whitepaper: docs/core/design/archive.md
last_updated: 2026-04-20
---

## Description

Add a read-only Slack Web API adapter that archives live workspace content into Spacedrive's Chat library model without replacing the existing export-based Slack adapter.

The adapter should ingest accessible conversations, users, root messages, threaded replies, reactions, timestamps, and searchable metadata. It should use config-driven token auth and persist incremental sync state so recurring syncs only fetch new history when possible.

## Implementation Notes

- Keep the existing `adapters/slack/` export adapter unchanged.
- Add a new `adapters/slack-live/` script adapter with token-based configuration.
- Preserve compatibility with archive browsing by keeping `message` as the primary search model.
- Persist adapter cursor state through the archive runtime so the script can resume channel and thread history incrementally.
- Limit dependencies to Python stdlib.

## Acceptance Criteria

- [ ] New live Slack adapter exists in its own adapter directory.
- [ ] Adapter manifest exposes token auth and optional conversation/history filters.
- [ ] Sync script indexes workspaces, channels, users, messages, replies, reactions, and metadata.
- [ ] Sync script persists incremental channel/thread cursor state.
- [ ] Existing export adapter remains available and unchanged.
- [ ] Archive runtime passes persisted cursor state back into script adapters.
- [ ] Automated validation covers the runtime cursor payload and Slack live manifest parsing.
