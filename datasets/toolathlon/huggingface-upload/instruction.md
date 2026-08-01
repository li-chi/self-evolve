Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Please scan the workspace folder, pick the model checkpoint with the highest eval_accuracy, then push the best model's folder to Hugging Face Hub as a model repo named `MyAwesomeModel-TestRepo`. 

Finalize the repo's `README.md` with the detailed evaluation results for all 15 benchmarks (keep three decimal places), you must refer to the current `README.md` under workspace and ensure its completeness in the uploaded repo. Do not change any other content in the `README.md` besides the benchmark scores.

You can use the `hf_token.txt` under the workspace if necessary.


## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `huggingface`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools huggingface                 # list tools with one-line summaries
mcp-tool schema huggingface <tool_name>    # full argument schema for one tool
mcp-tool call huggingface <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call huggingface <tool_name> '{}'
```
