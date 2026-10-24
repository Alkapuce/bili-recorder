# Multi-room archival implementation

Goal: publishable src package, independent room processes and controls, private local configuration, complete-session AV1 output and verified Quark archival.

Work:
- [x] Package modules, tests, Docker and launch scripts; separate public examples from local settings.
- [x] Per-room processes, ownership locks, graceful stop, status and enable/disable controls.
- [x] Serialize heavy postprocessing across rooms while recordings continue independently.
- [x] Archive upload mode, independent remote hash verification, recoverable deletion receipts; preserve mirror mode explicitly.
- [x] Exercise concurrent workers, real media merge/AV1/audio copy, upload failures and deletion safety.
- [x] Review tracked files and history for private values; local commit and publication status are recorded at delivery.

Failure cases to verify: duplicate room ownership; malformed/duplicate room IDs; unrelated process or stale status; recording stop during FFmpeg; reconnects and uncertain offline status; mismatched media; upload failure; wrong cloud directory; missing/changed remote bytes; changed local file; crash before/after local unlink; next archive pass preserving remote objects.

Existing runtime: Docker uses a copied legacy recorder and root `config.ini`; host upload timer invokes the root upload script. Moving source requires preserving that timer's invocation or explicitly migrating it. Existing historical recordings must remain untouched by implementation validation.

Verification completed:
- Host: `uv run python -B -m unittest discover -s tests -v`: 18 tests passed, 25.719 seconds. Includes two independent FFmpeg recorders against local HTTP streams and complete durable-session-to-archive processing.
- `uvx ruff check --select F src tests`, `git diff --check`, shell syntax and Docker Compose configuration checks passed.
- Public template and private local configuration pass the CLI `check` command. Malformed room IDs, overlapping output directories and missing required configuration are rejected.
- Existing Quark target metadata and a 64 KiB HTTP range read were verified read-only. No production upload/deletion was used for implementation testing.
- Git history before this change contains five commits; targeted credential/operational-value scan found no credential assignments. The public license retains authorship.
- Source migration required updating the installed legacy host timer to the packaged upload module. Its next scheduled run returned success; it retains the legacy single-room configuration and existing files. The recording container remains on its existing image until an explicitly scheduled deployment.

Private configuration now uses room directories for future recordings and verified archive deletion. Historical top-level recordings remain in their existing layout and are not enrolled in the new deletion policy. Do not copy or publish private configuration, cloud credentials, media or `.runtime/`.

Session storage v2 persists relative part paths and omits the derived process lock path. The same state can be read under different container/host mount paths; the archival scenario relocates its recording directory before processing. Multi-room configurations reject mirror mode to prevent one room from deleting another room's cloud archive.

Final candidate verification:
- `docker build --network=host -t bili-recorder:multiroom-candidate .`: exit 0.
- Same 18 tests inside the built Python 3.10 image: exit 0, 23.305 seconds, network disabled, 2 CPUs / 2 GiB memory, no production data/config mounted.
- Installed candidate package matches every source module by SHA256; private config is absent from the image.
- Normalized archival and concurrency manifests are written to `.runtime/test-results/`. Full logs are `.runtime/tests.log` and `.runtime/container-tests.log` (ignored).
- Existing recorder is still running/healthy with zero restarts; deployment remains separate from source publication.
