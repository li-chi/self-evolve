FROM toolathlon-harbor-base:v3

# Task subtree WITHOUT evaluation/ and groundtruth_workspace/ (artifact guard):
# preprocess + fixtures live where upstream expects them, under the harness root.
COPY task/ /opt/toolathlon/tasks/finalpool/quantitative-financial-analysis/

# Mock wiring: the agent-facing MCP bridge config and the initial mock state.
COPY mcp/servers.template.json /opt/harbor-mcp/servers.template.json
COPY mock_seed/ /opt/harbor-mcp/seeds/

COPY init.sh /opt/harbor-init/init.sh
RUN chmod +x /opt/harbor-init/init.sh

WORKDIR /app
ENTRYPOINT ["/opt/harbor-init/init.sh"]
