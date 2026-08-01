Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Analyze the wandb project https://wandb.ai/mluo/deepscaler-1.5b?nw=nwusermluo, identify the experiment with the best validation set performance, and find which step performed best in that experiment. Save the best_experiment_name, best_step, and best_val_score to a CSV file named `best_experiment.csv` in the workspace.

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `wandb`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools wandb                 # list tools with one-line summaries
mcp-tool schema wandb <tool_name>    # full argument schema for one tool
mcp-tool call wandb <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call wandb <tool_name> '{}'
```
