Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

In my workspace, employees have uploaded their expense claim materials.  You need to first check each expense claim to ensure:  All required supporting documents are complete (i.e., every item listed in the claim has a corresponding invoice). An invoice is complete only if it includes an invoice number, tax amount, and description; missing or N/A fields make the claim incomplete. And the claimed amounts match the amounts shown on the corresponding invoices.  If either of these requirements is not met, send an email to each of the corresponding employee and CC his/her manager. The email subject should be:  `Expense Claim Review Required: {claim_id}`. If both requirements are satisfied, accurately write the data from the expense claim into the **2024Q4REIMBURSEMENT** table in the Snowflake database. For any expense claims that exceed the allowed limit, mark them as abnormal (`flag = 1`). In such cases, also send an email notification to each of the employee and CC his/her department manager. The email subject should be: `Expense Over-Cap Notice: {claim_id}`


## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `snowflake`
- `emails`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools snowflake                 # list tools with one-line summaries
mcp-tool schema snowflake <tool_name>    # full argument schema for one tool
mcp-tool call snowflake <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call snowflake <tool_name> '{}'
```
