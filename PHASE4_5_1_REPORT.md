# Phase 4.5.1 — Config Parser Reliability

## Baseline and scope

- Fetched `origin/main` on 2026-10-04. Both local HEAD and remote main were `277edf53398525e2f82350fab94a1fa43116756c`.
- Preserved the pre-existing local edit to `config/config.yaml`.
- No changes to Summary architecture, Triage, Delivery, outbox QoS, attachments, history recovery, or OneBot protocol.

## Root cause and new flow

The previous post-validation cue tables rejected valid model interpretations when the administrator used wording outside fixed keyword lists. The model also received the target prefix, could only return an update, and worker errors often reduced to `ValueError`.

1. Existing ADMIN_QQ and self_id checks run before parsing.
2. `parse_target_and_body()` resolves an allowed numeric target or a unique alias prefix and returns the remaining body. Only that prefix is removed; numbers and alias mentions in the body remain intact.
3. Whole-body local rules parse explicit commands and a small set of clear Chinese phrases. Compound or uncertain language proceeds to the model.
4. The request persists its Python-selected `target_group_id` separately. New `input_text` and `intent_text` contain only the body.
5. DeepSeek returns a discriminated union: the existing update intent, `clarify`, or `unsupported`. All variants forbid extra fields. Update field types, enums, null rejection, nonempty changes and length limits remain strict.
6. Python checks capability boundaries without requiring fixed semantic cues. Updates create a proposal or report that no change is needed. Only `/confirm` applies a change.

Local semantics match the model prompt: “只总结” and “恢复正常处理” select the default `summary_only` profile; “加入收件箱” selects `inbox`; “重点关注” selects `priority`; “暂停处理” selects `ignore`. Individual negative statements only switch off the named capability, preserving other fields.

## Clarify, unsupported, errors and retries

- `clarify` completes the request and asks the administrator to choose a concrete setting and resend `/config`.
- `unsupported` completes the request and explains that schedules, expiry and conditional rules are unavailable. It does not apply a partial immediate update.
- Neither outcome creates a proposal or modifies policy. The result notification and completion status commit in one transaction with the existing request identity guard.
- Model `message` text is strictly bounded but never relayed or logged. Application-owned guidance prevents prompt/provider text leakage.
- Config errors use finite codes: `ambiguous_target`, `unauthorized_group`, `invalid_local_config`, `invalid_model_schema`, `unsafe_model_fields`, `unsupported_config`, `provider_error`.
- Provider transient failures, empty output, malformed/schema-invalid output and retryable finish reasons retain bounded retries and persistent retry counts.
- Target errors and malformed explicit local commands never enter the LLM queue. Valid clarify/unsupported results do not retry. Non-transient provider failures and non-retryable finish reasons do not retry.
- Unauthorized groups now receive `/allow add <group_id>` followed by `/confirm <id>` guidance. The outdated YAML/restart instruction was removed.
- Config failure logs contain the request ID, finite code and exception class, not user/provider bodies.

## Structured output decision

Retained `response_format={"type":"json_object"}` with strict Pydantic validation. The current Chat Completions endpoint documents only `text` and `json_object` response formats. Strict JSON Schema function calling remains Beta and is a different interface. No provider, API endpoint, model or dependency migration was introduced.

Sources checked on 2026-10-04:
- https://api-docs.deepseek.com/api/create-chat-completion/
- https://api-docs.deepseek.com/guides/tool_calls/

## Database compatibility

One additive, idempotent migration adds nullable `configuration_requests.intent_text TEXT` inside the existing database startup transaction. No tables are rebuilt or data removed.

NULL identifies legacy full-input requests. Before parsing one, the worker strips only a target prefix compatible with that request's persisted group and stores the resulting body. The persisted target is never reselected. New requests write the body into both text columns, so a body beginning with the same digits as its group cannot be mistaken for a legacy target.

Legacy rows, proposals, command receipts and their identity constraints remain compatible. If a legacy alias no longer matches, the worker safely asks for an explicit new command. Completed requests are not reparsed or resent.

## Regression coverage

New `tests/test_config_parser_reliability.py` covers:

- 19 local forms through proposal and confirmation, and uncertain/compound forms falling through to the model.
- Natural-language priority without fixed cues; numeric and alias prefix removal; preservation of dates, MB, day counts and course numbers.
- Prefix-only and duplicate/overlapping alias handling.
- Clarify/unsupported completion, unchanged policy, safe outbox text and no proposal.
- Concurrent result handling, replay, restart recovery and transaction rollback without duplicate business results.
- Strict union validation against group IDs, SQL, shell, paths, OneBot actions, unknown fields, string booleans, null, empty changes, invalid modes and length violations.
- Bounded model-output retries, transient versus non-transient provider errors, safe failure messages and persistent retry counts.
- No queueing for deterministic input errors; admin/self_id/authorization isolation.
- Idempotent additive migration and legacy numeric/alias request recovery without altering policy before confirmation.

Existing policy tests were updated to assert structural validation instead of keyword gating. Existing expiry, cancellation, stale diff, proposal identity and command receipt tests remain in place.
The shared model-output logging test now expects `ConfigParseResult` for config parsing; all assertions forbidding sensitive content in logs remain unchanged.

## Validation

Tests use the repository virtual environment and an isolated workspace temporary directory (`PYTEST_DEBUG_TEMPROOT`) because the host's default pytest cleanup directory is not writable.

| Command | Actual result |
| --- | --- |
| `python -m pytest -q` | Exit 0; 701 passed, 1 warning in 289.18s |
| `python -m ruff check app tests` | Exit 0; All checks passed! |
| `python -m compileall -q app` | Exit 0 |
| `python -m pip check` | Exit 0; No broken requirements found. |

The first complete suite found two outdated expectations for the config schema name in a shared log-safety test (699 passed). Those expectations were updated without changing the sensitive-data assertions, followed by a full-suite rerun. The FastAPI/Starlette test client also emits a dependency deprecation warning; dependency versions were not changed for this task.

## Known limits

- Natural-language semantic accuracy still depends on the model; users must review proposal diffs. These tests mock the provider and do not measure live DeepSeek semantic quality.
- Clarification uses safe fixed guidance rather than arbitrary model prose and requires a new `/config` command.
- Aliases must be unique prefixes. Stale legacy alias requests may require resubmission with a numeric group ID.
- Scheduled, expiring and conditional policies remain unsupported.

## Changed files

- `app/commands/policies.py`
- `app/llm/deepseek.py`
- `app/policies/errors.py` (new)
- `app/policies/models.py`
- `app/policies/parser.py`
- `app/policies/worker.py`
- `app/storage/db.py`
- `app/storage/policy_repository.py`
- `app/storage/policy_schema.py`
- `tests/test_policies.py`
- `tests/test_triage_schema.py` (config schema name in shared logging test only)
- `tests/test_config_parser_reliability.py` (new)
- `PHASE4_5_1_REPORT.md` (new)

Recommended commit message: `fix: improve natural-language config parsing reliability`
