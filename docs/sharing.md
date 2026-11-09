# Sharing the runner source

This is a standalone source repository for Python 3.11+ on POSIX. It includes a Linux systemd scheduler; other POSIX hosts need their own scheduler. The implementation currently supports Trello, Cursor and GitHub. The [adaptation guide](architecture.md) describes future Jira, Kiro and GitLab integrations.

## Build an uploadable archive

From the repository root:

```sh
python3 -m unittest discover -s tests -v
python3 scripts/package_source.py
```

The outputs are `dist/trello-cursor-runner.zip` and its adjacent `.sha256` checksum file. The ZIP contains a single `trello-cursor-runner/` folder and a `SHA256SUMS` manifest of the packaged files. The packager reads the current working files, including reviewed uncommitted changes; it does not archive Git history or require a commit. ZIP contents are deterministic for unchanged source files.

An explicit file list includes only:

- Runner source and tests.
- README, portable architecture and sharing documentation.
- Placeholder-only example configuration and systemd service environment.
- Systemd service/timer and optional Grafana dashboard.
- Standalone executor contract.
- GitHub Actions test workflow, Git ignore rules and the packaging script.

Local settings, credentials, logs, SQLite databases, worktrees, cached bytecode, Git history, the generated `dist/` folder and machine-specific handoff notes are excluded. The file list limits accidental inclusion of extra files; it does not detect secrets pasted into source. Inspect source changes before sharing. No third-party license has been selected for this repository.

Verify and extract from `dist/` on a host with `sha256sum` and `unzip`:

```sh
sha256sum -c trello-cursor-runner.zip.sha256
unzip trello-cursor-runner.zip
cd trello-cursor-runner
sha256sum -c SHA256SUMS
python3 -m unittest discover -s tests -v
```

## Put it on GitHub

The source checkout already has Git metadata and a GitHub origin. Inspect `git remote -v` and `git status` before choosing a destination. Review and commit the intended files, then push the chosen branch and open a pull request according to that repository's workflow.

For a new repository, extract the source ZIP, initialize Git in the extracted project folder and add the new repository as its remote. To upload files through the GitHub interface, upload the extracted contents, including `.github/workflows/ci.yml` and `.gitignore`; uploading just the ZIP stores an archive rather than making its contents the repository source.

The bundled GitHub Actions workflow runs the unit/integration fixtures without real task updates, agent calls or hosted PR creation. Repository tests need Python and Git; actual execution additionally needs authenticated Trello, Cursor and GitHub access plus the target project's tools.

## Configure another computer

Follow the README's requirements and local configuration instructions. Keep configuration and credentials outside the checkout. Set `skill_dir` in local configuration to the absolute path of `skills/runner-execution` in the new checkout, or use an installed compatible shared contract.

Run the live read-only `preview` before starting a scheduler. Do not copy runtime claims from another installation without reconciling their processes, branches and remote PRs. Importing this source or uploading it does not activate automation.
