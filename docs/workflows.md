# Useful tasks for Teleloom

Use these prompts after [connecting a client](clients.md). They leave profile and
chat selection to you, so no specific account or conversation is assumed.

## First conversation

> Check the server build and list profiles with their effective access. Ask me
> which profile to use. List up to ten readable chats, ask me to select one,
> and summarize its latest 20 messages with source links. Keep unread state
> unchanged. Do not send anything or change settings.

## Daily digest

> Ask me to choose a profile, chats and timezone. For the previous calendar day
> in that timezone, collect a bounded snapshot. Summarize decisions, open questions
> and actionable requests. Cite each point, show contradictory evidence where
> relevant, and report coverage and gaps. Acknowledge nothing and send nothing.

## A large research task

> Find discussions useful for a topic I choose. First show a bounded candidate
> list from accessible chats and agree the chat selection, time range and collection
> budget with me. Use collection jobs and frozen evidence, compute useful counters
> before expanding detail, and produce a concise sourced report. Distinguish
> observed counts from full totals. Retrieve exact originals for significant
> claims and list incomplete sources. Do not expand access or send messages.

## Inbox review

> Review the selected profile's unread or locally unprocessed inbox in bounded
> pages. Group items into decisions needed, replies to draft and information.
> Include sources. Draft possible replies, but keep read/acknowledgment state
> unchanged and send nothing until I approve exact previews.

## A controlled reply

> Ask me to select an account, conversation and message. Read the permitted reply
> context and draft a response. If sending is permitted, create an immutable
> preview showing the complete response and reply target. Wait for my confirmation
> of that preview, then execute the matching plan once and show its receipt.

## Documents and media

> From a conversation I select, find relevant documents in an agreed date range.
> Show the file selection before downloading or extracting. Use only configured,
> explicitly allowed engines. Summarize extracted content with exact message/file
> references and mark extraction failures or missing pages. Do not use an external
> provider or forward files without my explicit authorization.

The six [runtime skills](../skills/) and [agent guide](agent-guide.md) define the
execution contract. Profile permissions, backend limitations and current schema
limits still apply to every prompt.
