# beads-s3-skill

[日本語](README.ja.md)

An agent skill that lets you run [beads](https://github.com/gastownhall/beads) (`bd`) inside the sandboxes of Claude and ChatGPT. Because the sandbox filesystem is wiped after each session, the issue database is stored in an S3-compatible bucket.

It pairs the upstream beads skill with a setup script. Once setup finishes, the agent can use `bd` as normal.

## How it works

- Each project database is stored as a single git bundle: `s3://<bucket>/<prefix><project>.bundle`.
- `git-remote-s3bundle` acts as a git remote helper for the bundle. Because bd's Dolt backend natively supports git remotes, `bd dolt push`, `bd dolt pull`, and `bd bootstrap` work directly against the bucket.
- Setup enables bd's `dolt.auto-push`, pushing changes to the bucket on every write.
- Uploads use conditional writes to verify the bundle hasn't changed since it was read. If someone else pushed in the meantime, the upload fails with "fetch first" and `bd dolt pull` merges the changes. For services that don't support conditional request headers, it falls back to comparing the ETag right before uploading.
- The bd release pinned to the bucket is recorded in `<prefix>bd/VERSION`, and the archive is cached in the bucket. Sessions reuse this cached binary rather than re-downloading it from GitHub.

Enable object versioning on the bucket so every push is preserved as a snapshot you can roll back to.

## Requirements

- A sandbox with `bash`, `git`, and `python3` (`boto3` will be installed during setup if missing).
- Network access to the bucket endpoint. The initial session also needs access to GitHub (or npm) to fetch bd.
- An S3-compatible bucket and an access key with read, write, and list permissions.

## Setup

1. Create a bucket and an access key scoped to it.
2. Copy `skills/beads-s3-skill/auth/credentials.example.json` to `credentials.json` in the same directory and fill it in.
3. Zip the `skills/beads-s3-skill` folder (or take the `.skill` file from Releases and add `credentials.json`), then upload it to your AI app.

| Key | Required | Description |
| --- | --- | --- |
| `endpoint_url` | yes | S3 API endpoint, e.g. `https://s3.example.com` |
| `region` | yes | Region name used for request signing |
| `bucket` | yes | Bucket name |
| `access_key_id`, `secret_access_key` | yes | Access key |
| `prefix` | no | Key prefix for everything the skill stores. Default `beads/` |
| `default_project` | no | Project used when none is given. Default `beads` |
| `addressing_style` | no | `path` (default) or `virtual` |
| `conditional_writes` | no | `auto` (default), `always` or `never` |
| `git_user_name`, `git_user_email` | no | Git identity used when the sandbox has none. Default `beads` / `beads@localhost` |

## Usage

Tell the agent to track work with beads. At the start of each session, it runs:

```bash
bash scripts/setup.sh [project]
```

This installs bd, restores the project from the bucket (or creates it if missing), and prints `bd prime`. The project name also serves as the issue ID prefix (`<project>-xxx`).

To upgrade all projects in the bucket to a newer bd release:

```bash
bash scripts/setup.sh --upgrade [version]   # latest release if omitted
```

This restores each project with the new release, runs `bd migrate --force` and `bd dolt push`, then updates `<prefix>bd/VERSION`. Subsequent sessions will pick up the new release automatically.

## Bucket layout

```
<prefix>
├── bd/
│   ├── VERSION                       # bd release used with this bucket
│   └── <version>/<os>-<arch>.tar.gz  # cached release archive
└── <project>.bundle                  # project database (git bundle)
```

## Using the same bucket from your own machine

You can share a project with a local bd install (for instance, when working with Claude Code):

1. Put `skills/beads-s3-skill/scripts/git-remote-s3bundle` on your `PATH` and install `boto3`.
2. Save your settings to `~/.config/beads-s3/config.json` (same format as `credentials.json`), or point `BEADS_S3_CONFIG` at the file.
3. Run `git config --global url.s3bundle://.insteadOf https://s3bundle.invalid/`.
4. In the project, run `bd dolt remote add origin git+https://s3bundle.invalid/<bucket>/<prefix><project>.bundle`.

Use the bd version specified in `<prefix>bd/VERSION`. bd cannot open a database whose schema is newer than the binary.

## License

MIT. Files ported from the upstream beads skill remain under their original beads MIT license. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
