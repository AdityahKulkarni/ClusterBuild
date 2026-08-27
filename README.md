# ClusterBuild

A standalone, local-first CLI that automates OpenShift cluster installation
end-to-end -- credentials, bastion/install-directory setup, `install-config.yaml`
/ `agent-config.yaml` generation, platform-specific provisioning, and kicking
off the actual install -- grounded strictly in official Red Hat/OpenShift
documentation (`docs.redhat.com` and `github.com/openshift`). Built for
internal QE/test teams who currently do all of this by hand from a bastion.

There is **no server** and **no shared database**. Every team member installs
and runs ClusterBuild themselves; each person's own local state lives in
`~/.clusterbuild`.

## What it can install

| Platform | IPI | UPI | Agent-based | Assisted Installer | Static IP |
|---|---|---|---|---|---|
| vSphere | ✅ | ✅ | ✅ | ✅ | ✅ |
| Nutanix | ✅ | ✅ | ✅ | ✅ | ✅ |
| `none` (platform-agnostic, on vSphere infra) | -- | ✅ | ✅ | -- | ✅ |
| AWS | ✅ | -- | -- | -- | DHCP only |
| Azure | ✅ | -- | -- | -- | DHCP only |
| GCP | ✅ | -- | -- | -- | DHCP only |
| KubeVirt (Hosted Control Planes) | -- | -- | -- | via `hcp create cluster` | n/a |

Cells left blank aren't a ClusterBuild limitation so much as a documentation
one: e.g. the Agent-based Installer's own "Supported platforms" list
(`docs.redhat.com`) does not include AWS/Azure/GCP, so ClusterBuild doesn't
fabricate a catalog entry for it. Static IP (NMState) is only supported on
vSphere/Nutanix per the plan this tool was built from -- the public clouds
provision their own DHCP/dynamic-IP infrastructure via `openshift-install`.

Every selectable field and networking prerequisite lives in a versioned,
doc-sourced YAML **Catalog** (`clusterbuild/catalog/`) -- see
"How correctness is enforced" below.

## Install

### Recommended: one-line installer

No Python environment needed. This downloads the prebuilt single-file
`clusterbuild` binary for your OS/arch from the
[latest GitHub Release](https://github.com/AdityahKulkarni/ClusterBuild/releases),
verifies its checksum, and puts it on your `PATH`:

```bash
curl -fsSL https://raw.githubusercontent.com/AdityahKulkarni/ClusterBuild/main/scripts/install.sh | bash
```

Then:

```bash
clusterbuild version
clusterbuild doctor run
```

Supports Linux and macOS, on x86_64 and arm64. To upgrade, just re-run the
same command -- it always fetches the latest release (or pass
`CLUSTERBUILD_VERSION=v0.2.0` to pin a specific one). See
[`scripts/install.sh`](scripts/install.sh) for what it does and its other env
vars (`CLUSTERBUILD_INSTALL_DIR`, `CLUSTERBUILD_BASE_URL` for an internal
mirror) before piping it into `bash`, as with any installer script.

<details>
<summary>Other ways to install (pipx, editable/dev)</summary>

**`pipx`, from source (no release binary required):**

```bash
pipx install "git+https://github.com/AdityahKulkarni/ClusterBuild.git"
clusterbuild version
```

Upgrade with `pipx upgrade clusterbuild`.

**Editable install for development:**

```bash
git clone https://github.com/AdityahKulkarni/ClusterBuild.git && cd ClusterBuild
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

</details>

### Releasing a new version (maintainers)

The one-line installer expects a GitHub Release tagged `vX.Y.Z` with one
binary + checksum pair per supported OS/arch, named exactly
`clusterbuild-<os>-<arch>` / `clusterbuild-<os>-<arch>.sha256` (`os` is
`linux`/`darwin`, `arch` is `x86_64`/`arm64`). PyInstaller can't cross-compile,
so build once per platform (e.g. on a Linux x86_64 box and a macOS box you
have access to):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[build]"
./scripts/build_binary.sh
# -> dist/clusterbuild-<os>-<arch> and dist/clusterbuild-<os>-<arch>.sha256
```

Then, once you have all the platform assets you intend to ship gathered in
one place:

```bash
git tag v0.2.0 && git push origin v0.2.0
gh release create v0.2.0 dist/clusterbuild-*-* --title v0.2.0 --generate-notes
```

(`gh release create` accepts multiple assets in one call -- repeat the build
step on each platform first and collect every `dist/clusterbuild-*` file
before running it.)

## Quickstart

```bash
# 0. See what DNS/LB/VIP/NTP prerequisites you need before touching credentials:
clusterbuild checklist generate --platform vsphere --method ipi

# 1. Store your platform credentials (OS keyring by default; never plaintext):
clusterbuild credentials set --platform vsphere

# 2. Register the RHEL bastion you'll install from (verifies SSH + tooling):
clusterbuild bastion register --host bastion01.lab.example.com --user myuser

# 3. Run the interactive wizard and kick off the install as a background job:
clusterbuild cluster create --platform vsphere --method ipi

# 4. Watch it go:
clusterbuild cluster logs <job-id> --follow
clusterbuild cluster status <cluster-name>
clusterbuild cluster kubeconfig <cluster-name>
```

Run `clusterbuild doctor run` any time to self-check your local
`~/.clusterbuild` foundation (keyring backend, SQLite state, the detached job
runner) independent of any specific cluster.

## Configuring your own lab environment

The bundled `clusterbuild/environments/*.yaml` profiles are mostly
placeholders (only the vSphere PNQ2 lab profile has real values baked in).
**Don't edit the installed package** to fill in your own Nutanix/AWS/Azure/GCP
details -- it won't survive an upgrade. Instead, drop a same-named file into
your own per-user override directory:

```bash
mkdir -p ~/.clusterbuild/environments
cp "$(python -c 'from clusterbuild.core.config import bundled_environments_dir; print(bundled_environments_dir())')/nutanix-lab.yaml" \
   ~/.clusterbuild/environments/nutanix-lab.yaml
# edit ~/.clusterbuild/environments/nutanix-lab.yaml with your lab's real values
```

Anything in `~/.clusterbuild/environments/<id>.yaml` takes precedence over the
bundled copy of the same name. Then pass `--environment-profile nutanix-lab`
to `cluster create` (or just let it default -- see
`cli/cluster.py`'s `DEFAULT_ENV_PROFILE_BY_PLATFORM`).

## Keeping the catalog in sync across the team

The Catalog (every install-method's fields + prerequisite checklists, each
citing its `docs.redhat.com`/`github.com/openshift` source) ships bundled
inside the package, but a team can also point ClusterBuild at a shared
internal git repo so catalog fixes/new-OCP-version entries don't require a
full package reinstall:

```bash
export CLUSTERBUILD_CATALOG_REPO=https://github.com/<your-org>/clusterbuild-catalog.git
clusterbuild catalog update          # clones/pulls into ~/.clusterbuild/catalog
clusterbuild catalog check-versions  # flags new OCP y-streams vs. docs.redhat.com/openshift/installer
```

Entries under `~/.clusterbuild/catalog/` take precedence over the bundled
copy, same override pattern as environment profiles above.

## How correctness is enforced

Manifest generation is **deterministic and schema-driven, never
free-form/LLM-generated**. Every value written into `install-config.yaml` /
`agent-config.yaml` comes from exactly one of four sources declared in the
Catalog: a hardcoded `constant`, an `environment_profile` value, interactive
`user_input`, or the OS `keyring`. An unknown field fails closed rather than
being guessed. This keeps every team member's install grounded in the same
official-doc-derived rules with no shared server, and gives an auditable
"why is this field here" trail for a security-sensitive tool.

`install-config.yaml`/`agent-config.yaml` (and, for Assisted/HCP, the
equivalent API payloads) are always backed up locally to
`~/.clusterbuild/backups/<cluster>/<timestamp>/` (with a SHA-256 checksum
recorded in local state) before the install starts.

## Repository layout

```
clusterbuild/
  catalog/            doc-grounded field/checklist definitions, versioned by OCP release
  environments/       lab-specific profiles (vCenter/Prism Central/cloud region, etc.)
  cli/                Typer command groups (one module per `clusterbuild <group>`)
  core/
    installers/       one job handler per install method (ipi/upi/agent/assisted/hcp)
    drivers/          one platform driver per infra target (vsphere/nutanix), used by
                       upi/agent/assisted for actual VM provisioning
    manifest_builder.py   turns Catalog + environment profile + answers into real YAML
    catalog_loader.py     reads the versioned Catalog
    jobs.py                detached background job runner + log tailing
    secrets.py             OS keyring / optional Vault credential storage
    state.py                local SQLite models (bastions, clusters, jobs, audit log)
scripts/
  install.sh          one-line installer (see "Install" above)
  build_binary.sh     PyInstaller single-file build (see "Releasing a new version" above)
tests/
```

## Security notes

- Credentials never touch ClusterBuild's local SQLite state or config files --
  they live in the requesting user's own OS keychain (or an optional
  self-hosted Vault under the user's own token).
- The bastion SSH executor (`core/bastion_exec.py`) refuses unknown host keys
  by design (`RejectPolicy`) -- add the bastion's key to `~/.ssh/known_hosts`
  first.
- AWS/Azure/GCP credentials are staged onto the bastion's filesystem
  (`~/.aws/credentials`, `~/.azure/osServicePrincipal.json`,
  `~/.gcp/osServiceAccount.json`) with owner-only permissions immediately
  before `openshift-install create cluster` runs, never written into a
  backed-up manifest.
- Any change to credential handling, the SSH executor, or the job runner
  should get a focused security review (dependency/secrets/subprocess
  surface) before it reaches the team -- these are the highest-value places
  for that review to catch something.
