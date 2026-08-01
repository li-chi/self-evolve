Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Remove products of a specified model from your WooCommerce store and recall the corresponding products. The product must be taken off the shelf (set to draft or private status, or with catalog visibility set to hidden), rather than deleted. Given a product model, search for historical orders related to that product and send a recall email to the corresponding customer's email address. See `recall_email_template.md` for the email template. The email must include a Google Forms recall form created by strictly following the `recall_form_template.json` template (order of questions, options and contents), and return the form information in `recall_report.json`. The template is as follows:

```json
{
"form_id": "Google Forms form ID",

"form_url": "Google Forms form link",
}
```

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `woocommerce`
- `google_forms`

Call them with `mcp-tool` (tool names and arguments are exactly the
service's own):

```bash
mcp-tool tools woocommerce                 # list tools with one-line summaries
mcp-tool schema woocommerce <tool_name>    # full argument schema for one tool
mcp-tool call woocommerce <tool_name> '<json-object-of-arguments>'
```

For example:

```bash
mcp-tool call woocommerce <tool_name> '{}'
```
