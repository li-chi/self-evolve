"""Harbor's CLI with the experience hook installed. Same arguments as `harbor`.

    EVOLVE_ARM=log .venv/bin/python -m tools.evolve.run run \\
      -p datasets/toolathlon/ab-testing -a terminus-2 \\
      -m openai/qwen3.6-35b -k 5 -n 16 --env-file .env

Harbor itself is untouched; the patch is applied to the imported classes
before the CLI builds anything.
"""

from tools.evolve import hook

hook.install()

from harbor.cli.main import app  # noqa: E402  (must follow install())

if __name__ == "__main__":
    app()
