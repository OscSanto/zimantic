#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
dev=0
case "${1:-}" in
    "")
        ;;
    --dev)
        dev=1
        shift
        ;;
    *)
        echo "usage: $0 [--dev]" >&2
        exit 2
        ;;
esac
if (( $# != 0 )); then
    echo "usage: $0 [--dev]" >&2
    exit 2
fi

host="${DEPLOY_HOST:-192.168.1.219}"
repo="${DEPLOY_PATH:-/opt/iiab/zimantic}"

if (( dev )); then
    if git -C "$ROOT" diff --staged --quiet; then
        echo "--dev requires at least one staged change" >&2
        exit 2
    fi
    remote_repo=$(printf "'%s'" "${repo//\'/\'\\\'\'}")
    git -C "$ROOT" diff --staged |
        ssh "$host" "sudo git -C $remote_repo reset --hard HEAD && sudo git -C $remote_repo apply -"
    tag=dev
else
    version="${VERSION:-$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$ROOT/pyproject.toml" | head -n 1)}"
    version="${version#v}"
    if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "VERSION must have the form X.Y.Z, with an optional leading v" >&2
        exit 2
    fi
    tag="v$version"
fi

ssh "$host" sh -s -- "$tag" "$repo" "$dev" <<'REMOTE'
set -eu

tag=$1
repo=$2
dev=$3
venv="$repo/.venv"

if [ "$dev" = 0 ]; then
    expected_version=${tag#v}
    sudo git -C "$repo" fetch --depth 1 origin tag "$tag"
    sudo git -C "$repo" checkout --detach "tags/$tag"
fi
sudo "$venv/bin/python" -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --upgrade \
    "$repo" >/tmp/zimantic-pip-release.log
sudo systemctl restart zimantic

ready=
for _ in $(seq 1 20); do
    if curl -fsS --max-time 2 http://127.0.0.1:8090/app.js >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done
test "$ready" = 1
curl -fsS http://127.0.0.1:8090/app.css -o /dev/null
installed_version=$(
    sudo "$venv/bin/python" -m pip show zimantic |
        awk -F': ' '/^Version:/{print $2}'
)
if [ "$dev" = 0 ]; then
    test "$installed_version" = "$expected_version"
fi
sudo systemctl is-active --quiet zimantic
echo "deployed=$tag package=$installed_version"
REMOTE
