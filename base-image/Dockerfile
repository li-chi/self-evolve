FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash coreutils tar ripgrep git curl unzip poppler-utils \
    tmux asciinema \
    && rm -rf /var/lib/apt/lists/*

# Dependencies for Toolathlon graders (utils.general.helper and friends)
# plus common libs agents/graders use to manipulate task files.
RUN pip install --no-cache-dir \
    pandas numpy sympy tqdm termcolor pyyaml aiofiles requests \
    beautifulsoup4 openai openai-agents \
    openpyxl python-docx pypdf PyPDF2 pdfplumber python-pptx \
    yfinance datasets huggingface_hub bibtexparser arxiv tenacity addict

# Vendored Toolathlon harness code (graders import utils.*)
COPY toolathlon/ /opt/toolathlon/
ENV PYTHONPATH=/opt/toolathlon

# Fail the build if the grader imports don't resolve
RUN python -c "import utils.general.helper; import utils.mcp.tool_servers; print('utils imports OK')"

WORKDIR /app
