FROM toolathlon-harbor-base:v3

# Task subtree WITHOUT evaluation/ and groundtruth_workspace/ (artifact guard):
# preprocess + fixtures live where upstream expects them, under the harness root.
COPY task/ /opt/toolathlon/tasks/finalpool/verl-dataset/
COPY init.sh /opt/harbor-init/init.sh
RUN chmod +x /opt/harbor-init/init.sh

WORKDIR /app
ENTRYPOINT ["/opt/harbor-init/init.sh"]
