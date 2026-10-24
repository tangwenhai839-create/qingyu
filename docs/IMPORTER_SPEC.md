# Importer interface

This page describes the import pipeline in `gateway/app/importers/` as it is implemented. A new source, including a Claude export or another JSON shape, has to fit this pipeline. This document does not add a parser, a filename, an HTTP route, or a setuptools entry point.

Issue #19 is the Claude conversation importer. It is not implemented here. Open a design comment on that issue before changing the candidate filename set or the persistence path.

## Pipeline

`POST /api/v1/import/chatgpt` accepts one multipart file. The name must end in `.zip` (otherwise 415). The form field `source_platform` defaults to `chatgpt`. The route requires the operator bearer token from `require_token`. The handler returns 202 and `{"import_job_id": "<uuid>"}`. It does not parse the export. Parsing runs in a separate process, `python -m app.importers.worker_runtime`.

The request writes the upload under `<ARCHIVE_ROOT>/staging/uploads/<generation>/payload`, then calls `ArchiveService.prepare`. That copies the bytes to a generation under `staging/archive-prepared`, hashes them with SHA-256, and hardlinks the file to `archive/objects/<first two hex digits>/<sha256>`. `prepare` does not read or write the database. Both staging generations are removed when the request finishes. A failed removal is logged. It does not roll back an archive object that was already published.

`persist_prepared` then runs in the request transaction:

- The relative path must be `archive/objects/<sha256[:2]>/<sha256>`, the file must be a regular file, and its size and SHA-256 must match the prepared values.
- A new digest inserts one `ArchiveObject` with `integrity_status` `verified`.
- An existing row for that digest is kept when the size, relative path, and `verified` status match. `integrity_checked_at` is set to the check time. A size, path, or status mismatch raises and the transaction rolls back. The published file is not deleted.
- Every successful call inserts a new `ArchiveObservation` (original filename, source platform, logical path, and observation metadata). The observation is not an update of an older observation.
- After commit, `finalize` writes `manifests/<sha256>.json` from the committed row (`manifest_version` 2, including the object id, digest, size, relative path, media type, and integrity fields). The manifest is replaced to match that row. The object bytes are not rewritten.

The request also inserts an `ImportJob` in status `pending`, with `parser_name` `pending-detection` and `parser_version` `1`, linked to a `SourceAccount` of the submitted platform and account label `default`.

The worker claims the job, copies the archived zip to `staging/jobs/<job id>/<attempt>/<lease token>/_verified_input.zip`, and hashes that copy. The size and SHA-256 must match the `ArchiveObject`. A mismatch sets `integrity_status` to `integrity_failure`, records `integrity_error` and `integrity_checked_at`, and stops the job. The object file and earlier observations stay. Retry is refused for `archive_integrity_failure`.

## Staging

`ensure_staging_layout` creates four directories under `<ARCHIVE_ROOT>/staging`:

| Directory | Role |
| --- | --- |
| `uploads` | Request-body generations. Pre-job only. |
| `archive-prepared` | Bytes being hashed and linked into `archive/objects`. Pre-job only. |
| `jobs` | `jobs/<job id>/<attempt>/<lease token>/`, holding `_verified_input.zip` and `extracted/`. |
| `quarantine` | Staging paths that cleanup could not delete in place. |

`create_prejob_generation` accepts only `uploads` and `archive-prepared`. It enqueues the directory for cleanup before creating it, and holds an owner lock. Cleanup may delete files only inside these staging namespaces. It has no authority over `archive/objects/`, `manifests/`, or committed database rows. That is an application rule, not a separate filesystem principal. `staging/` is not evidence and is not part of a backup.

## ZIP member selection

`safe_extract` (`gateway/app/importers/zip_staging.py`) checks the central directory before writing files. `ZipLimits` defaults are 512 MiB total uncompressed, 128 MiB for one member, 10,000 members, and a compression ratio of 100. It rejects absolute or drive paths, `..`, empty names, Windows reserved names, trailing dots or spaces, ADS-style colons, duplicate or case-insensitive path collisions, file/directory collisions, encrypted members (`encrypted_zip_unsupported`), nested members whose name ends in `.zip`, and member types other than a file or directory. Extraction cannot write outside the attempt directory.

After extraction, `ImportProcessor` keeps regular files whose suffix is `.json` and whose name is exactly `fixture.json` or `conversations.json`. Other files stay on disk and are counted as `unparsed_archive_members`. Zero selected files is `unsupported_structure`. More than one is `ambiguous_structure`. The selected member is decoded with `load_json_document`, which rejects duplicate object keys (`duplicate_json_key`) instead of keeping one of them.

A Claude or other JSON importer whose member is not named `fixture.json` or `conversations.json` is invisible to this selector. Extending the name set is a code change in `ImportProcessor.process`. That change belongs to the importer issue, not to this spec.

## Parser contract

A parser is a class with:

- `name: str` and `version: str`, stable identifiers stored on `ImportBatchCommit.parser_name` and `contract_version`.
- `can_parse(payload) -> bool`. Detection only. It must not persist anything.
- `validate(payload, member) -> list[dict]`. Return unknown-field warnings. Raise `ParserValidationError` with a list of error dicts (`type`, `raw_pointer`, `detail`) when the document is not acceptable. Validation covers the whole document before any conversation is persisted.
- `parse(payload) -> Iterator[ParsedConversation]`. One item per conversation, in source order.

The shared dataclasses live in `gateway/app/importers/generic.py`.

`ParsedConversation`: `external_id`, `title`, `messages`, `current_node_id`, and optional `nodes`, `raw_json_pointer`, `source_conversation_json`, `raw_create_timestamp`, `raw_update_timestamp`, `source_fragment_json`. `nodes` defaults to empty.

`ParsedNode`: `node_id`, `declared_node_id`, `parent_node_id`, `declared_children`, `message_presence` (`object`, `null`, or `absent`), `message`, `raw_json_pointer`, `source_node_json`, `source_fragment_json`.

`ParsedMessage`: `node_id`, `parent_node_id`, `role`, `content_text`, `content_json`, `model`, `raw_timestamp`, `branch_key`, `attachments`, and optional `external_message_id`, `raw_update_timestamp`, `content_type`, `semantic_evidence_json`, `observation_evidence_json`, `raw_json_pointer`, `source_fragment_json`.

`ParsedAttachment`: `stable_attachment_key`, `external_attachment_id`, `original_reference`, `member_path`, `filename`, `media_type`, `size_bytes`, `metadata`, `descriptor_json`, `raw_json_pointer`, and optional `asset_id`, `source_references`, `source_fragment_json`.

JSON pointers use `pointer_escape`: `~` becomes `~0` and `/` becomes `~1`.

`ParserRegistry` holds a list of parser instances. The default list is `GenericTreeParser()` and `ChatGPTExportParser()`, constructed in that order inside `ParserRegistry.__init__`. There is no entry point, package metadata, or directory scan. A new parser is registered by adding an instance to that list, or by passing `parsers=` in a test. `ImportProcessor` takes the same registry through `registry=`.

`detect` keeps every parser for which `can_parse` is true:

- one match returns `("matched", parser)`;
- more than one returns `("ambiguous_structure", None)`;
- none returns `("unsupported_real_chatgpt_export", None)` when the payload looks like a conversation export (a dict containing `conversations`, or a non-empty list whose first object has any of `id`, `conversation_id`, `mapping`, `current_node`), and `("unsupported_structure", None)` otherwise.

`resolve(name, version)` returns the single installed parser with that exact pair. It does not fall back to another version. Zero or several matches return `None`.

If the job already has `ImportBatchCommit` rows, `resolve_durable_parser_contract` requires exactly one `(parser_name, contract_version)` across those rows, equal to the pair stored on the job, and installed in the registry. Otherwise processing stops with `parser_contract_conflict` or `parser_contract_unavailable`. The pinned parser is used even when `can_parse` would now choose something else. A partial retry uses the same pair. Changing the meaning of an existing `(name, version)` makes replay compare new parse output with observations written by the old meaning.

The job's own `parser_name` stays `pending-detection` until the first newly inserted batch. That first batch copies `parser.name` and `parser.version` onto the job and onto the batch row.

## Installed parsers

`GenericTreeParser` (`name` `generic-tree-fixture`, `version` `0.1`) accepts an object whose `schema_marker` is `qingyu-fixture-v1` and whose `conversations` is an array. It is the project fixture, not a ChatGPT export. `parse` fills `messages` and leaves `nodes` empty. Unknown fields become warnings; known fields of the wrong type raise `ParserValidationError`.

`ChatGPTExportParser` (`name` `chatgpt-export`, `version` `1`) accepts a non-empty array whose first object has a `mapping` object and a string `id` or `conversation_id`. `validate` then checks every conversation: ids, mapping nodes, parent and child links, and finite numbers. `parse` fills `nodes`, including mapping nodes whose `message` is null or absent, and copies source fragments onto the parsed objects.

`can_parse` of a new parser must be false for documents either installed parser already accepts. Overlap makes `detect` return `ambiguous_structure` for those documents.

## What persistence writes

`ImportService.import_fixture` walks `parsed_conversations`. Each conversation is one batch, committed on its own. The batch key is `<zip member>:conversation:<index>`.

For every parser it may insert:

- a `Conversation` keyed by source account and external id, or a synthetic id `synthetic:<archive sha256>:<member>:conversation:<index>` when `external_id` is missing;
- a `Message` and a `MessageRevision` when that revision fingerprint is new;
- a `MessageObservation` for that job, external node id, and revision, if one is not already present.

An existing revision is not updated. A later import with a different fingerprint inserts another revision. On a new batch, if the same external conversation id already exists and `title` differs, the import assigns `Conversation.title` to the new title. A replay of an existing batch does not change the title. That title assignment is the identity field this path writes again. It does not delete the conversation, its messages, its revisions, or its observations.

`real_export` is `parser.name == "chatgpt-export"` and `parsed.nodes` is non-empty. Only that case also inserts `ConversationObservation` and `MappingNodeObservation`, and fills message-observation source fingerprints. On a replay of an existing batch, `_validate_real_export_snapshot` checks the stored observation against the parsed snapshot and does not insert a second conversation observation for that batch. A mismatch raises `BatchReplayIdentityError` (`batch_replay_identity_mismatch` on the job).

A parser with any other name, including one that fills `nodes`, follows the fixture path: no `ConversationObservation` and no `MappingNodeObservation`. Naming a Claude parser `chatgpt-export` so that this branch runs is not a valid extension. The name is the ChatGPT contract. A Claude observation writer is new persistence code and is part of issue #19.

Attachment bytes, when `ParsedAttachment.member_path` is set, must be a canonical relative POSIX path of a regular extracted file. When `member_path` is empty and `asset_id` is set, the resolver looks for exactly one regular member whose stem equals that id and whose suffix is exactly `.dat`. Display names and directories are not fallbacks. More than one match is `ambiguous`. None is `missing`. Declared size or media type must match the file or the attachment is `failed`. Available bytes go through `ArchiveService.prepare` and become their own archive object plus observations. A failed attachment can leave the job partial. It does not delete the source zip.

Parser validation, unsupported structure, and encrypted-zip failures mark the job failed or partial and leave the archived zip in place. `staging/jobs/` for a successful terminal attempt may be deleted. Failed attempt directories remain until staging cleanup, which still cannot delete `archive/objects/` or `manifests/`.

## Adding a parser

1. Add a class under `gateway/app/importers/` with `name`, `version`, `can_parse`, `validate`, and `parse`. Keep `name` and `version` different from `generic-tree-fixture`/`0.1` and `chatgpt-export`/`1` unless the new class is a compatible replacement of that same contract.
2. Make `can_parse` false for the fixture marker and for the ChatGPT list shape, unless this parser is that contract.
3. Construct it in the default list in `ParserRegistry.__init__`. Do not invent an entry point. Tests can pass `parsers=` or `ImportProcessor(registry=...)`.
4. Put the JSON document in the zip under the name `fixture.json` or `conversations.json`, or change the candidate set in `ImportProcessor.process` as part of the importer issue. This spec does not add a filename.
5. Expect fixture-style rows (`Conversation`, `Message`, `MessageRevision`, `MessageObservation`) from the existing service. Conversation and mapping-node observations stay behind the `chatgpt-export` gate. Do not reuse that name to reach the gate.
6. Leave `(name, version)` immutable once a batch has committed it. Removing the class makes those jobs `parser_contract_unavailable`.
7. Do not delete or rewrite archive object bytes, manifests, observation rows, or message revisions. A new import appends. Tests belong in `gateway/tests/`, run against PostgreSQL, and must not commit real conversation exports.

Public job responses still include the durable report. Do not put database URLs, tokens, server filesystem paths, raw exception strings, or lease tokens into parser errors or report fields.
