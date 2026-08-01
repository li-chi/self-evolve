Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Please find all developers' branches in the `BenchTasksCollv3` project for the most recent commits that added new tasks. For each person’s new tasks, check the development status for each task. If the implementation satisfies the requirements, it is considered implemented; otherwise, it is considered implementing. Update all these new tasks on our Notion page `Task Tracker`, and create a new branch in GitHub named finalpool, adding all of the implemented tasks till now in Notion Page to finalpool, with the relative path in the project being `tasks/finalpool`.

Tips:

- You could find the requirements in `tasks/examples`
- In addition to the content requirements explicitly mentioned in the examples, only the existence of the file needs to be checked

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `github`
- `notion`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools github                 # list tools with one-line summaries
mcp-tool schema github <tool_name>    # full argument schema for one tool
mcp-tool call github <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call github <tool_name> '{}'
```
