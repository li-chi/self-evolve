Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

I am the teaching assistant for the NLP course. The final presentation assignments from the students have now been received in my email. All the statistical data for this course is stored in the Excel file named `nlp_statistics.xlsx` within the workspace. Please help me identify which students have not submitted their assignments (including the presentation submissions from my inbox), and send them an email. The subject of the email should be "nlp-course-emergency," and the content must include the student's name and ID number to avoid the message being marked as spam.

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `emails`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools emails                 # list tools with one-line summaries
mcp-tool schema emails <tool_name>    # full argument schema for one tool
mcp-tool call emails <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call emails list_emails '{"folder": "INBOX", "limit": 20}'
```
