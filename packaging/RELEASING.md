# Releasing NearShare

A release is one pushed tag. CI builds and tests everything on x64 and
arm64, and only if all of it passes does it publish:

| Where | What | Needs |
|---|---|---|
| GitHub release | `.deb`, both snaps, `.rpm`, Arch package, AUR recipe, both Flatpak bundles, `SHA256SUMS`, notes from `CHANGELOG.md` | nothing |
| Snap Store, `stable` | `amd64` and `arm64` snaps | `SNAPCRAFT_STORE_CREDENTIALS` secret |
| `ppa:dhiva-labs/apps` | source packages for each series in `packaging/ppa-upload.sh` (24.04, 26.04, 26.10) | `PPA_GPG_PRIVATE_KEY` secret (and `PPA_GPG_PASSPHRASE`) |

The Snap Store and PPA jobs skip themselves with a warning while their
secret is missing, so a release still produces the GitHub release. The AUR
and Flathub are not automated: the release carries the files to submit.

## Cutting a release

```bash
tools/bump-version.sh 1.2.0
```

That writes the version into `nearshare/__init__.py`, `snap/snapcraft.yaml`,
`debian/changelog`, the AppStream metainfo and `CHANGELOG.md`. Then:

1. Write the notes under `## 1.2.0` in `CHANGELOG.md` (they become the
   GitHub release text) and a short summary in `debian/changelog`.
2. Check that the files agree and the tests pass:

   ```bash
   .venv/bin/python -m tests.test_ui_logic
   ```

3. Commit, open a pull request, and merge it once CI is green. On a pull
   request the `release` job runs everything except the upload, so a
   missing package is found here.
4. Tag the merge commit and push the tag:

   ```bash
   git switch main && git pull
   git tag -a v1.2.0 -m "NearShare 1.2.0"
   git push origin v1.2.0
   ```

CI refuses a tag that does not match the version in the tree. Nothing can
be un-published afterwards: the Snap Store keeps every revision and
Launchpad never accepts the same version twice. If something is wrong,
fix it and release the next patch version.

## One-time setup

### Snap Store

Export a login that can only upload and release this one snap, and store
it as a repository secret:

```bash
snapcraft export-login --snaps nearshare --acls package_access,package_push,package_update,package_release -
```

Copy the printed text into the secret `SNAPCRAFT_STORE_CREDENTIALS`
(repository Settings, then Secrets and variables, then Actions). The login
expires after a year by default; `--expires` sets another date.

### Launchpad PPA

Launchpad accepts an upload only if it is signed by a key registered on
the uploader's Launchpad account, and whose email matches the
`Maintainer` in `debian/changelog`. An upload signed by any other key is
dropped without an error. Check which keys are registered:

```bash
curl -s https://api.launchpad.net/1.0/~dhivakar1010/gpg_keys | python3 -m json.tool | grep fingerprint
```

A dedicated signing subkey or a separate CI key registered on Launchpad
is safer than putting a personal primary key into a secret. Export the
key and store it:

```bash
gpg --armor --export-secret-keys <KEYID>
```

- `PPA_GPG_PRIVATE_KEY`: the armored private key
- `PPA_GPG_PASSPHRASE`: its passphrase (leave unset if it has none)

The PPA must also publish for arm64 (its *Change details* page, under
*Processors*), or arm64 machines cannot install from it.

## Publishing by hand

Everything CI does can be run locally.

**PPA**, from a clean checkout of the tag, with `python3-grpc-tools`,
`devscripts` and `dput` installed:

```bash
packaging/ppa-upload.sh --dry-run     # build the source packages only
packaging/ppa-upload.sh               # sign with the default key and upload
PPA_REVISION=2 packaging/ppa-upload.sh noble   # a packaging-only fix for one series
```

**Snap Store**, with the snaps from the release (or from `snapcraft pack`):

```bash
snapcraft upload --release=stable nearshare_1.2.0_amd64.snap
snapcraft upload --release=stable nearshare_1.2.0_arm64.snap
```

**AUR**: `nearshare-<version>-aur.tar.gz` from the release holds the
`PKGBUILD` and `.SRCINFO` to push to the AUR repository.

## After a release

```bash
snap info nearshare | sed -n '/channels/,$p'
curl -s "https://api.launchpad.net/1.0/~dhiva-labs/+archive/ubuntu/apps?ws.op=getPublishedSources&source_name=nearshare&status=Published" \
  | python3 -c 'import json,sys; [print(e["source_package_version"]) for e in json.load(sys.stdin)["entries"]]'
```

Launchpad needs a few minutes to build and publish after accepting an
upload; it emails the uploader either way.

## Snap interfaces that are not auto-connected

`bluez` and `network-manager` are connected by hand after installing
(`sudo snap connect nearshare:bluez`). Only the Snap Store can change
that: `packaging/store-request-bluez.md` is the request to post on
<https://forum.snapcraft.io> in the *store-requests* category. Until it
is granted, the app shows the commands itself.
