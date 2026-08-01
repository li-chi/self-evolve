Your workspace directory is `/app`. When a relative path is mentioned, resolve it against this workspace directory.

Someone just filed an issue in my Annoy-DataSync project asking about the license information for our released datasets. Could you please help me figure out which license we should use? My requirement is to directly reuse the license from the dataset's direct data sources or the models used for synthesis. If there are multiple sources, I'll use the one with the most permissive permissions for derivative/secondary use. Once you've determined the license, please reply and close this issue, strictly following the following format (do not modify, add, remove, or alter anything other than placeholders):

"Thanks for your interest! The licenses for the two datasets are: Annoy-PyEdu-Rs-Raw = {license}, Annoy-PyEdu-Rs = {license}"

Also, please update the corresponding huggingface dataset pages by adding the following line at the end of the existing readme. No other content is required:
"\n\n**License**\n\nThe license for this dataset is {license}."

If you need the huggingface token, you can find it under `.hf_token`.

## Available service tools

This environment provides the following service(s) as MCP tool servers:
- `github`

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
