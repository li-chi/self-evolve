Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

I am a professor, recently some students have sent me resumes by eamils, and I will only communicate with students who have independent first-author publications. I want to finish the interviews within the two days of tomorrow and the day after tomorrow (Hong Kong Time), at the same time each student must be reserved at least one and a half hours of time for the interview, it must be scheduled during working hours: 8 AM to 5 PM. Finally, sync the relevant schedule into Google Calendar, and tell me what time slots they have been scheduled for respectively?

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `google_calendar`
- `emails`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools google_calendar                 # list tools with one-line summaries
mcp-tool schema google_calendar <tool_name>    # full argument schema for one tool
mcp-tool call google_calendar <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call google_calendar <tool_name> '{}'
```
