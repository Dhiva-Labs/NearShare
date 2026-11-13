#!/usr/bin/env bash
# Build signed source packages of the checked-out version and upload them
# to the Launchpad PPA, one per Ubuntu series.
#
#   packaging/ppa-upload.sh                    # every series in SERIES
#   packaging/ppa-upload.sh noble resolute     # just these
#   packaging/ppa-upload.sh --dry-run          # build, do not sign or upload
#
# Environment:
#   PPA_TARGET               dput target (default: ppa:dhiva-labs/apps)
#   PPA_KEY                  GPG key id to sign with (default: gpg's default key)
#   PPA_REVISION             the N in 1.1.0~noble<N> (default: 1). Launchpad
#                            refuses a version it has already seen, so raise
#                            this to upload a packaging-only fix.
#   PPA_GPG_PASSPHRASE_FILE  file holding the key's passphrase, for
#                            unattended signing in CI
#   OUT                      where the source packages are written
#                            (default: a new temporary directory)
#
# Run it on Ubuntu 24.04 (CI does) or anywhere whose protoc is no newer
# than the oldest series': the source package ships the generated protobuf
# bindings, and bindings from an older protoc load on a newer protobuf
# runtime but not the other way round. Every supported series currently
# has protobuf 3.21.12, so the system python3-grpc-tools of any of them
# is fine. See packaging/PPA.md.

set -euo pipefail

# Ubuntu series to publish for: the supported LTS releases and the current
# interim release. All need GTK 4.10+ and libadwaita 1.5+, which rules
# out 22.04. Update this list when a series is released or goes EOL.
SERIES=(noble resolute stonking)

dry_run=0
wanted=()
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry_run=1 ;;
        -h|--help) sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) wanted+=("$arg") ;;
    esac
done
[ ${#wanted[@]} -gt 0 ] && SERIES=("${wanted[@]}")

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target="${PPA_TARGET:-ppa:dhiva-labs/apps}"
revision="${PPA_REVISION:-1}"
out="${OUT:-$(mktemp -d -t nearshare-ppa-XXXXXX)}"
mkdir -p "$out"

first_line="$(head -n1 "$root/debian/changelog")"
if [[ ! "$first_line" =~ ^nearshare\ \(([0-9.]+)~[a-z]+[0-9]+\)\ [a-z]+\;\ urgency= ]]; then
    echo "unexpected first line in debian/changelog: $first_line" >&2
    exit 1
fi
upstream="${BASH_REMATCH[1]}"

app_version="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$root/nearshare/__init__.py")"
if [ "$upstream" != "$app_version" ]; then
    echo "debian/changelog is at $upstream but nearshare/__init__.py says" \
         "$app_version; run tools/bump-version.sh" >&2
    exit 1
fi

for tool in dpkg-buildpackage dpkg-source; do
    command -v "$tool" >/dev/null || {
        echo "$tool not found: sudo apt install dpkg-dev debhelper" >&2; exit 1; }
done
if [ "$dry_run" = 0 ]; then
    for tool in debsign dput gpg; do
        command -v "$tool" >/dev/null || {
            echo "$tool not found: sudo apt install devscripts dput gnupg" >&2; exit 1; }
    done
fi
/usr/bin/python3 -c 'import grpc_tools.protoc' 2>/dev/null || {
    echo "the system python3 cannot import grpc_tools:" \
         "sudo apt install python3-grpc-tools" >&2
    exit 1
}

# An upload is a clean export of HEAD: nothing untracked, no .venv, no
# stale bindings, and exactly what the tag will point at. A dry run may
# rehearse uncommitted work, so it exports the working tree instead.
dirty=0
if [ -n "$(git -C "$root" status --porcelain)" ]; then
    dirty=1
    if [ "$dry_run" = 0 ]; then
        echo "the working tree has uncommitted changes; commit them first" \
             "(or use --dry-run to rehearse)" >&2
        exit 1
    fi
    echo "dry run: packaging the working tree, including uncommitted changes"
fi

export_tree() {
    if [ "$dirty" = 1 ]; then
        (cd "$root" && git ls-files -z -co --exclude-standard \
            | tar --null -T - -c --ignore-failed-read) | tar -x -C "$1"
    else
        git -C "$root" archive HEAD | tar -x -C "$1"
    fi
}

sign_args=()
if [ -n "${PPA_KEY:-}" ]; then
    sign_args+=("-k$PPA_KEY")
fi
if [ -n "${PPA_GPG_PASSPHRASE_FILE:-}" ]; then
    wrapper="$out/gpg-loopback"
    cat > "$wrapper" <<WRAP
#!/bin/sh
exec gpg --batch --pinentry-mode loopback --passphrase-file "$PPA_GPG_PASSPHRASE_FILE" "\$@"
WRAP
    chmod 700 "$wrapper"
    sign_args+=("-p$wrapper")
fi

echo "NearShare $upstream -> $target (${SERIES[*]})"
uploaded=()
for series in "${SERIES[@]}"; do
    version="${upstream}~${series}${revision}"
    work="$out/$series/nearshare-$version"
    rm -rf "$out/$series"
    mkdir -p "$work"
    export_tree "$work"

    # The same changelog entry, re-targeted at this series.
    sed -i "1s/.*/nearshare ($version) $series; urgency=medium/" \
        "$work/debian/changelog"

    # Bindings ship in the source package; the buildd cannot generate them.
    (cd "$work" && bash tools/genproto.sh /usr/bin/python3 >/dev/null)
    test -f "$work/nearshare/proto/offline_wire_formats_pb2.py"

    echo "--- $series: building source package $version"
    (cd "$work" && dpkg-buildpackage -S -sa -d -us -uc >"$out/$series/build.log" 2>&1) || {
        tail -n 30 "$out/$series/build.log" >&2; exit 1; }
    changes="$out/$series/nearshare_${version}_source.changes"
    test -f "$changes"

    # Nothing that does not belong may be in the tarball, and the
    # bindings must be.
    listing="$(tar -tJf "$out/$series/nearshare_${version}.tar.xz")"
    if grep -E '(^|/)(\.git|\.venv|__pycache__)(/|$)' <<<"$listing"; then
        echo "the source tarball contains files that must not ship" >&2; exit 1
    fi
    [ "$(grep -c '_pb2\.py$' <<<"$listing")" -eq 7 ] || {
        echo "expected 7 generated protobuf modules in the tarball" >&2; exit 1; }

    if [ "$dry_run" = 1 ]; then
        echo "    dry run: built $changes"
        continue
    fi

    debsign "${sign_args[@]}" "$changes"
    # -f: dput records a finished upload next to the package and silently
    # skips a second attempt, which hides a retry after a rejected upload.
    dput -f "$target" "$changes"
    uploaded+=("$version")
done

if [ "$dry_run" = 1 ]; then
    echo "Source packages are in $out (nothing was signed or uploaded)."
    exit 0
fi

cat <<DONE

Uploaded: ${uploaded[*]}
dput only confirms the transfer. Launchpad accepts or rejects by email and
then builds; check in a few minutes:
  https://launchpad.net/~dhiva-labs/+archive/ubuntu/apps/+packages
DONE
