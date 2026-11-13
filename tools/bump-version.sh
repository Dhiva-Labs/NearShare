#!/usr/bin/env bash
# Set the version everywhere it is written down, ready for a release.
#
#   tools/bump-version.sh 1.2.0
#
# Updates nearshare/__init__.py, snap/snapcraft.yaml, debian/changelog,
# the AppStream metainfo and CHANGELOG.md (the "Unreleased" notes become
# the new version's). tests/test_ui_logic.py checks that they agree, and
# CI refuses a tag that does not match. Review the diff, then follow
# packaging/RELEASING.md.

set -euo pipefail

version="${1:-}"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "usage: tools/bump-version.sh X.Y.Z" >&2
    exit 2
fi
cd "$(dirname "${BASH_SOURCE[0]}")/.."

today="$(date +%Y-%m-%d)"
stamp="$(date -R)"
maintainer="$(sed -n 's/^ -- \(.*>\)  .*/\1/p' debian/changelog | head -n1)"

sed -i "s/^__version__ = \".*\"$/__version__ = \"$version\"/" nearshare/__init__.py
sed -i "s/^version: '.*'$/version: '$version'/" snap/snapcraft.yaml

if ! grep -q "^## $version - " CHANGELOG.md; then
    grep -q '^## Unreleased$' CHANGELOG.md || {
        echo "CHANGELOG.md has no '## Unreleased' section" >&2; exit 1; }
    sed -i "s/^## Unreleased$/## Unreleased\n\n## $version - $today/" CHANGELOG.md
fi

if ! head -n1 debian/changelog | grep -q "^nearshare ($version~"; then
    {
        printf 'nearshare (%s~noble1) noble; urgency=medium\n\n' "$version"
        printf '  * Release %s. See CHANGELOG.md.\n\n' "$version"
        printf ' -- %s  %s\n\n' "$maintainer" "$stamp"
        cat debian/changelog
    } > debian/changelog.new
    mv debian/changelog.new debian/changelog
fi

metainfo=packaging/flatpak/dev.dhivalabs.nearshare.metainfo.xml
if ! grep -q "<release version=\"$version\"" "$metainfo"; then
    sed -i "s|^  <releases>$|  <releases>\n    <release version=\"$version\" date=\"$today\"/>|" "$metainfo"
fi

echo "Version set to $version. Now:"
echo "  1. write the release notes under '## $version' in CHANGELOG.md"
echo "     and the summary in debian/changelog"
echo "  2. python3 -m tests.test_ui_logic   (checks the files agree)"
echo "  3. follow packaging/RELEASING.md"
