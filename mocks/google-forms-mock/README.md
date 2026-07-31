# google-forms-mock

Mock MCP server mirroring [`matteoantoci/google-forms-mcp`](https://github.com/matteoantoci/google-forms-mcp),
which is what Toolathlon uses as its `google_forms` server. Every tool
name and parameter matches the official server; responses match the
Google Forms API v1 response shapes that `google-forms-mcp` returns.

## Tool surface (5)

| tool                            | maps to                                                        |
|---------------------------------|----------------------------------------------------------------|
| `create_form`                   | `forms.forms.create`                                           |
| `add_text_question`             | `forms.forms.batchUpdate` (`createItem` + `textQuestion`)       |
| `add_multiple_choice_question`  | `forms.forms.batchUpdate` (`createItem` + `choiceQuestion`)     |
| `get_form`                      | `forms.forms.get`                                              |
| `get_form_responses`            | `forms.forms.responses.list`                                   |

Upstream return shapes:

- `create_form` -> `{formId, title, description, responderUri}` (the
  simplified shape upstream constructs, **not** the raw `Form` resource).
- `add_text_question` / `add_multiple_choice_question` ->
  `{success, message, questionTitle, [options,] required}` (upstream's
  acknowledgement; does not include the assigned `itemId` /
  `questionId`).
- `get_form` -> raw Google Forms API v1 `Form` resource.
- `get_form_responses` -> raw `ListFormResponsesResponse`, i.e.
  `{"responses": [<FormResponse>...]}`.

Plus three mock-only debug tools used by per-task setup/verification:

- `mock_debug_state` -- return the full persisted state.
- `mock_debug_seed_form(formId, title, description=None,
  items=[{title, type, required?, options?, paragraph?, questionId?,
  itemId?, description?}])` -- insert a complete form fixture in
  declared order (no `insert(0, ...)` reversal).
- `mock_debug_seed_response(formId, answers, responseId=None,
  createTime=None)` -- seed a response. `answers` accepts the
  shorthand `{questionId: "value"}` / `{questionId: ["v1","v2"]}` or a
  verbatim Answer object.

## Behavior notes

- `add_text_question` and `add_multiple_choice_question` insert the
  new item at index 0 of `form.items`, mirroring the upstream's
  `location: { index: 0 }`. To seed forms in canonical question order,
  use `mock_debug_seed_form`.
- `revisionId` is bumped on every mutating call; the value is opaque.
- `formId` is a 44-char URL-safe blob beginning with `1`, matching the
  real-world API prefix.
- `responderUri` is `https://docs.google.com/forms/d/<formId>/viewform`.
- `linkedSheetId` is always `null`; this mock does not implement
  Google Forms <-> Sheets linking.
- Quiz settings (`settings.quizSettings.isQuiz`) default to `false`;
  the upstream does not expose a setter so the mock keeps it static.
- Errors mirror Google API error envelopes: `{"error": {"code", "message",
  "status"}}` for `NOT_FOUND` cases.

## Skipped (not in upstream surface)

- `update_form` / `batchUpdate` passthrough (no general request).
- `delete_item`, `move_item`, `update_item` (upstream only supports
  `createItem`).
- Question types beyond text/short and `RADIO` multiple-choice
  (CHECKBOX, DROP_DOWN, SCALE, DATE, TIME, FILE_UPLOAD, etc.) -- the
  upstream does not expose these.
- Quiz toggling / `set_quiz_settings`.
- `responderUri` rewriting / `publish_form`.
- Google Drive folder placement (forms have no `folder_id` here).

If a future Toolathlon task needs any of the above, add the tool to
this mock first, then upstream.

## State

`$GFORMS_MOCK_STATE_DIR/state.json` (default
`/workspace/output/end_state/gforms/state.json` inside the container,
`~/.openclaw/gforms_mock/state.json` on host).

```jsonc
{
  "forms": {
    "<formId>": {
      "formId", "revisionId",
      "info": {"title", "documentTitle", "description"?},
      "settings": {"quizSettings": {"isQuiz": false}},
      "items": [
        {"itemId", "title",
         "questionItem": {"question": {
            "questionId", "required",
            "textQuestion": {"paragraph": false}
            | "choiceQuestion": {"type": "RADIO",
                                 "options": [{"value": "..."}]}}}}
      ],
      "responderUri": "https://docs.google.com/forms/d/<formId>/viewform",
      "linkedSheetId": null
    }
  },
  "responses": {
    "<formId>": [
      {"responseId", "formId", "createTime", "lastSubmittedTime",
       "answers": {"<questionId>": {
          "questionId",
          "textAnswers": {"answers": [{"value": "..."}]}}}}
    ]
  },
  "next_id": {"item", "question", "response", "revision"},
  "calls": [{"op", "ts", ...}]
}
```

Seed via `GFORMS_MOCK_SEED_PATH` (loaded once if no `state.json` exists).
