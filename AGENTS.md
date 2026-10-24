# Bili Recorder

- Python >=3.10, `src/bili_recorder/` package; `uv sync` installs the CLI.
- Verify with `uv run python -B -m unittest discover -s tests -v`; media tests need FFmpeg with libsvtav1.
- Public defaults live in `config/example.ini`; local settings, credentials, recordings and runtime state must stay ignored. Do not print credential files.
- Each room owns one process, one output directory and an exclusive lock. Persist session part paths relative to their room directory so Docker and host mounts can share state; keep session state until final media validation succeeds. Unknown live status must never end a session.
- Archive mode must never propagate local deletions to cloud. Local deletion requires a successful independent cloud SHA256 readback and an unchanged local file. Mirror mode and DeleteLocal are incompatible.
- The deployed Docker container contains copied code. Source edits and tests do not imply deployment; do not restart an active recorder without checking its state and authorization.
