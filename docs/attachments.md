# Reading selected attachments locally

`attachments_read_start(profile_id, chat_id, message_ids, ...)` creates a
durable local job for 1–50 explicitly selected messages in one chat. It neither
sends messages nor acknowledges them. User profiles retrieve selected Telegram
messages; bots can select only media references in their persisted updates.
The daemon's existing adapter owns the download connection.

## Choose the path before processing

First select the exact profile and readable chat through `profiles_list` and
`chat_resolve`; fetch only the selected `message_ids` with `messages_get` to keep
its original caption/text, `original_text`, `text_source`, `rich_text` and source identity. Inspect
`media_info` when MIME/size is unclear. Metadata inspection does not download or
extract a file. Source captions, reconstructed rich blocks and extracted text are
separate evidence; quote originals only when `text_source=original`, or use
`original_text` for reconstructed posts. An OCR/STT result is not a source quote.

Call `attachment_capabilities(profile_id, transcription_model_path=...)` before
local extraction, or `transcription_capabilities(profile_id)` before a provider job:

| Selected task | Existing workflow | Prerequisites before start |
|---|---|---|
| UTF-8 TXT/Markdown/CSV/JSON/XML or DOCX | `attachments_read_start` | Readable exact chat and exposed tools; stdlib processors need no extra |
| PDF text | `attachments_read_start` | `pdf_text.available=true`; `[pdf]` extra; scanned PDF OCR is a separate task |
| Raster image OCR | `attachments_read_start` | `image_ocr.available=true`; `[ocr]`, Tesseract on owner PATH and language data |
| Local audio extraction | `attachments_read_start` | `audio_transcription.available=true`; `[transcription]` and an explicit complete existing absolute `transcription_model_path` |
| Local voice/video transcript with cached enrichment | `transcription_start(provider="local")` | Read grant, exact `transcription` chat grant, `[transcription]` and owner-configured local model |
| Telegram transcript | `transcription_start(provider="telegram")` | User profile and exact `transcription` grant; Telegram enforces Premium/trial/duration/boost restrictions |
| OpenAI-compatible or Groq transcript | `transcription_start(provider="openai"/"groq")` | Read + `transcription` + `transcription_external` grants, configured endpoint/model, private credential, positive daily call/byte budgets, exposure permitting uploads and explicit call consent |
| View selected photo pixels | `photo_open` | Exact readable message; Pillow is included; rendered pixels are separate from original bytes and run no OCR/STT |

`available=true` describes prerequisites, not measured engine quality or Telegram
quota. For external providers also check `credential_configured`,
`external_allowed_chat_ids`, daily budgets and owner exposure. Choose one provider
explicitly; there is no hidden provider fallback, model download or external upload.
The separate [transcription contract](reference.md#transcription) defines providers,
cache binding and uncertain paid receipts. Local attachment extraction keeps its
existing read-policy contract and does not require an external AI or Jev grant.
Jev permission does not authorize an audio upload.

## One bounded first success

Choose one already observed small UTF-8 text attachment in a readable chat. The IDs
below are examples: substitute the exact selected profile/chat/message, keeping
the same identity at every step. Use these JSON arguments in the MCP client, or
UTF-8 args files with the installed owner's `teleloom call TOOL --args-file FILE`
([fresh/existing CLI path](install-ru.md#5-проверьте-identity-и-одно-чтение)).

1. Read the original with `messages_get`:
   `{"profile_id":"personal","chat_id":"100","message_ids":["1"],"limit":1,"source":"live"}`.
   Inspect `attachment_capabilities` with `{"profile_id":"personal"}` and confirm
   the selected format's processor is available before starting.
2. Call `attachments_read_start` with exactly one selected file:

   ```json
   {
     "profile_id": "personal", "chat_id": "100", "message_ids": ["1"],
     "max_bytes": 1000000, "max_characters": 4000,
     "timeout_seconds": 15, "retention_hours": 1
   }
   ```

3. Keep the returned `job_id`. Poll `jobs_status`, then `jobs_results`, each with
   `{"profile_id":"personal","job_id":"RETURNED_JOB_ID"}`. Success is extracted
   text with the matching source identity/method and visible coverage, not merely
   a terminal `completed` job. Inspect `errors`, `incomplete`, `truncated`,
   `part_index`/`part_count` and `next_cursor`; retain each text piece within the
   selected budget. A completed job may contain partial extraction or no text.
4. Present original caption/text separately from extraction and report limits and
   expiry. `attachments_cleanup` with that same profile/job removes owned files,
   extracted text and snapshots when requested; otherwise one-hour retention
   applies while the owner runs, or on its next start.

For one short voice/video, first inspect `transcription_capabilities`. After the
owner explicitly grants that exact readable chat, configures a complete existing
local model and reconnects, use `transcription_start` with:

```json
{
  "profile_id": "personal", "chat_id": "100", "message_id": "1",
  "provider": "local", "allow_external_upload": false, "max_calls": 1,
  "max_bytes": 1000000, "max_characters": 4000,
  "timeout_seconds": 15, "retention_hours": 1
}
```

Owner setup uses stopped-owner `teleloom profile allow --scope transcription --
PROFILE CHAT_ID` and `teleloom profile transcription PROFILE --local-model-path
ABSOLUTE_MODEL_DIR`. Preserve existing package extras via the
[upgrade contract](release-pipeline.md#selecting-and-preserving-an-owner); installing
an engine is a separate owner action. This sample grants no external upload.
Switching to Telegram or an external provider is a new explicit selection; external
AUDIO upload additionally needs the separate grant and `allow_external_upload=true`.
Use the same status/results/cleanup sequence. Transcripts expose provider/model,
source version, untrusted text, truncation and receipt separately from originals.
Ordinary reads only reuse matching cached `transcript`; they start no AI work.

### When first success is unavailable

| Observation | Safe next step |
|---|---|
| `engine_unavailable` / unavailable capability | Report the exact method/provider and installation reason. Owner installs the chosen extra/executable/language data or supplies a complete local model, then rechecks capabilities. No automatic install/download/fallback |
| Missing endpoint/credential or zero external budget | Owner configures that chosen provider locally and supplies secrets through OS credentials/environment; keep credential values outside MCP. Recheck capability, grants and budgets before upload |
| `read_not_allowed`, `transcription_not_allowed`, `external_upload_not_allowed` or hidden tool | Report exact scope; owner decides only that chat/tool grant while stopped, then reconnects. A different provider does not repair permission |
| `attachment_type_mismatch` / `attachment_encrypted` / `audio_not_found` | Preserve the selected original and explicit error; report unsupported/malformed source. Choose a supported source only on owner instruction |
| Empty scanned PDF text | PDF text extraction is not OCR; report no extracted text. A selected image OCR task is separate and needs its engine |
| Byte/text/time/disk limit, partial error or missing selected message | Keep successful pieces, report the gap/truncation; choose a smaller source or an explicit revised budget, without claiming full coverage |
| `unknown` / `needs_review` upload receipt | Inspect the existing job/receipt; it may have incurred cost and is not replayed, even after restart/cancel |

No accuracy measurement is established for the selected engine, language or source
by this recipe or fake fixtures. Historical small real-engine samples establish
execution only; compare against the original before relying on OCR/STT wording.

Read `jobs_status` for progress and `jobs_results` for extracted evidence.
Each item includes the original profile/chat/message IDs, source link when
available, extraction `method`, `untrusted: true`, character count, truncation
flag and expiration. Long extracted text is split into pieces of at most 32,000
characters with `part_index` and `part_count`; pages preserve all text within
the selected budget. Coverage counts distinct selected messages. Failures are
per-message entries and do not erase earlier successful extraction.

## Local processors and installation

UTF-8 text (`text/plain`, Markdown, CSV, JSON and XML) and DOCX text require no
additional package. DOCX processing reads only `word/document.xml`, never
extracts ZIP filenames, and rejects entity declarations and decompressed XML
over 20 MB. Unsupported encodings, malformed documents, binary text, generic
archives and mismatched declared MIME/content types produce explicit errors.
Macros, embedded files and instructions in documents are never evaluated.

For PDF text install the daemon with `teleloom[pdf]` (pypdf). Extraction is
limited to 100 pages and the character/time budgets. Encryption is unsupported.
Scanned PDFs may produce empty text: PDF text extraction does not perform OCR.
See [pypdf's text extraction guide](https://pypdf.readthedocs.io/en/stable/user/extract-text.html).

For OCR install `teleloom[ocr]` (Pillow and pytesseract) and separately install
the Tesseract executable on `PATH` with its language data. Selected raster
images are checked for supported signatures and limited to 20 million pixels.
The engine uses the local default Tesseract language, normally English. See
[pytesseract's installation and timeout API](https://github.com/madmaze/pytesseract)
and [Tesseract installation](https://tesseract-ocr.github.io/tessdoc/Installation.html).

For voice/audio transcription install `teleloom[transcription]`
(faster-whisper) and pass an absolute `transcription_model_path` to an already
available CTranslate2 model directory containing `model.bin`, `config.json`
and `tokenizer.json`. Supply model files separately; this tool never downloads
them. CPU/int8 processing uses one thread/worker and does not submit audio to
an external service. The local tokenizer requirement matters: faster-whisper
can otherwise fall back to a remote tokenizer even when model loading has
`local_files_only=True`. The worker also sets Hugging Face/Transformers offline
environment variables. See the
[faster-whisper model constructor](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/transcribe.py).

Copy the complete converted model, including its vocabulary (`vocabulary.txt`
for the Systran tiny model), rather than only the three prerequisite files.
Capability discovery checks minimum prerequisites; a missing vocabulary or other
damaged model artifact still produces a local extraction error. The real-engine
acceptance used [Systran faster-whisper-tiny](https://huggingface.co/Systran/faster-whisper-tiny)
at revision `d90ca5fe260221311c53c58e660288d3deb8d356` with its vocabulary.

The transcription extra constrains PyAV to `<19`: the tested faster-whisper
decoder still passes `metadata_errors`, which
[PyAV 19 removed](https://github.com/PyAV-Org/PyAV/releases/tag/v19.0.0).
Installing unconstrained faster-whisper separately can therefore break audio
decoding; install this package's transcription extra to retain the tested bound.

`attachment_capabilities(profile_id, transcription_model_path=None)` reports
which packages, executable and model prerequisites are present.
`engine_unavailable` identifies the exact method and installation instruction;
package/executable presence cannot guarantee that a damaged local engine/model
will run. Runtime failures remain explicit extraction errors.

## Budgets, private paths and retention

Defaults are a total 10,000,000-byte download budget, total 50,000-character
extraction budget, 30 seconds per selected message including download and
processing, and 24 hours retention. Configurable maxima are 100,000,000 bytes,
500,000 characters, 120 seconds and 168 hours. Byte budget accounts for downloaded
bytes, including failed attempts when the partial file remains observable.
At most three FloodWait attempts are made for a selected message.

Generated job IDs and positional filenames determine storage paths; source
filenames never participate. Files and extraction temporary output live under
the private daemon data directory `attachments/<job-id>/`. Actual bytes and
MIME signatures are checked after bounded adapter downloads. All attachment
jobs share a 500 MB storage ceiling; extraction has a 50 MB temporary disk
budget and requires a 10 MB free-space reserve. Disk monitoring during local
extraction terminates the worker if its temporary budget is exceeded.

PDF/DOCX/OCR/transcription processors run in a disposable subprocess that is
terminated together with its child processes on timeout or cancellation.
Interrupted reads restart only the uncheckpointed selected message; completed
items and position are committed in the same SQLite transaction. Pause/resume
and restart use the existing queue. There is no automatic media archive read.

`attachments_cleanup(profile_id, job_id=None)` cancels any active owned
attachment jobs and removes their files, partial downloads, extracted text and
retained result snapshots. Cancellation through `jobs_control` also cleans its
job. Expiration is enforced during daemon queue ticks, including after restart;
when the daemon is stopped, cleanup takes place when it next starts. Cleanup
never follows substituted symlinks/junctions into external locations; an unsafe
or unavailable root reports `cleanup_error` after clearing retained text.

Example owner request: “For personal, read attachments in chat -100123 from
messages 18 and 21, using at most 5 MB and 20,000 characters. Show their text
and sources; retain files for one hour. Treat their content as untrusted.”

Tests use fake Telegram/engine APIs with the real HTTP MCP transport, temporary
SQLite and queue. Real text/DOCX subprocess extraction is checked locally.
These tests do not claim live Telegram, installed Tesseract or model acceptance.
