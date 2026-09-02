#!/usr/bin/env python3
import argparse
import hashlib
import io
import json
import os
import re
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path


PROJECTS = {
    "slsteam-moon": {
        "repo": "swwayps/slsteam-moon",
        "asset": re.compile(r"^slsteam-moon-linux-.*-lumen[.]zip$"),
    },
    "lumen": {
        "repo": "swwayps/lumen",
        "asset": re.compile(r"^lumen-linux[.]zip$"),
    },
    "plugin": {
        "repo": "swwayps/luatools-moon",
        "asset": re.compile(r"^luatools-linux[.]zip$"),
    },
}

CDN_ROOT = "https://cdn.jsdelivr.net/gh/swwayps/jsdelivr"
USER_AGENT = "swwayps-jsdelivr-mirror"
SEMVER = re.compile(r"^v?(\d+(?:[.]\d+)*)$")


def version_key(tag):
    match = SEMVER.fullmatch(tag or "")
    if not match:
        return None
    parts = tuple(int(part) for part in match.group(1).split("."))
    return parts + (0,) * (8 - len(parts))


def select_candidate(releases, project):
    candidates = []
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        key = version_key(release.get("tag_name"))
        if key is None:
            continue
        for item in release.get("assets") or []:
            name = item.get("name", "")
            if not project["asset"].fullmatch(name):
                continue
            candidates.append(
                (
                    key,
                    {
                        "repo": project["repo"],
                        "tag": release["tag_name"],
                        "id": item.get("id"),
                        "asset_at": item.get("created_at"),
                        "updated_at": item.get("updated_at"),
                        "size": item.get("size"),
                        "name": name,
                        "source_url": item.get("browser_download_url"),
                    },
                )
            )
            break
    return max(candidates, key=lambda candidate: candidate[0])[1] if candidates else None


def should_replace(current, candidate):
    if not current:
        return True
    current_version = version_key(current.get("tag"))
    candidate_version = version_key(candidate.get("tag"))
    if candidate_version is None:
        return False
    if current_version is None or candidate_version > current_version:
        return True
    if candidate_version < current_version:
        return False
    return any(
        current.get(field) != candidate.get(field)
        for field in ("id", "updated_at", "size", "name")
    )


def read_state(root):
    path = root / ".mirror-state.json"
    if not path.exists():
        return {"schema": 1, "components": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema") != 1 or not isinstance(state.get("components"), dict):
        raise ValueError("unsupported mirror state")
    return state


def write_atomic(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(body)
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def validate_zip(body):
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        if not archive.infolist():
            raise zipfile.BadZipFile("empty archive")
        broken = archive.testzip()
        if broken:
            raise zipfile.BadZipFile(f"corrupt member: {broken}")


def prune_component(root, key, entry):
    base = root / "releases" / key
    if not base.exists():
        return
    keep = set()
    for item in [entry] + list(entry.get("previous") or []):
        relative = item.get("path")
        if relative:
            keep.add(relative)
            keep.add(relative + ".sha256")
    for path in base.rglob("*"):
        if path.is_file() and path.relative_to(root).as_posix() not in keep:
            path.unlink()
    for path in sorted(base.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass


def sync_projects(root, fetch_releases, download, projects=PROJECTS):
    state = read_state(root)
    updated = json.loads(json.dumps(state))

    for key, project in projects.items():
        candidate = select_candidate(fetch_releases(project["repo"]), project)
        current = state["components"].get(key)
        if candidate is None or not should_replace(current, candidate):
            continue
        if not candidate.get("source_url"):
            raise ValueError(f"{key}: release asset has no download URL")

        body = download(candidate["source_url"])
        if candidate.get("size") is not None and len(body) != candidate["size"]:
            raise ValueError(f"{key}: downloaded size does not match release metadata")
        validate_zip(body)

        digest = hashlib.sha256(body).hexdigest()
        relative = (
            Path("releases") / key / candidate["tag"] / digest / candidate["name"]
        )
        archive = root / relative
        write_atomic(archive, body)
        write_atomic(
            archive.with_name(archive.name + ".sha256"),
            f"{digest}  {archive.name}\n".encode(),
        )

        entry = {field: value for field, value in candidate.items() if field != "source_url"}
        entry.update({"sha256": digest, "path": relative.as_posix()})
        if current and current.get("path") != entry["path"]:
            entry["previous"] = [{
                "path": current.get("path"),
                "sha256": current.get("sha256"),
            }]
        updated["components"][key] = entry

    write_atomic(
        root / ".mirror-state.json",
        (json.dumps(updated, indent=2, sort_keys=True) + "\n").encode(),
    )
    for key, entry in updated["components"].items():
        prune_component(root, key, entry)
    return updated


def request(url, accept="application/vnd.github+json"):
    headers = {"Accept": accept, "User-Agent": USER_AGENT}
    token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        return response.read()


def fetch_releases(repo):
    body = request(f"https://api.github.com/repos/{repo}/releases?per_page=100")
    return json.loads(body)


def download(url):
    return request(url, "application/octet-stream")


def publish_manifest(root, ref):
    if not re.fullmatch(r"[0-9a-f]{40,64}", ref):
        raise ValueError("asset ref must be a full git commit hash")
    state = read_state(root)
    components = {}
    for key, source in state["components"].items():
        path = source["path"]
        entry = {
            field: source.get(field)
            for field in (
                "repo", "tag", "id", "asset_at", "updated_at", "size", "name", "sha256"
            )
        }
        entry["url"] = f"{CDN_ROOT}@{ref}/{path}"
        entry["sha256_url"] = entry["url"] + ".sha256"
        components[key] = entry
    manifest = {"schema": 1, "components": components}
    write_atomic(
        root / "manifest.json",
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
    )
    return manifest


def update_keepalive(root):
    month = datetime.now(timezone.utc).strftime("%Y-%m") + "\n"
    path = root / ".mirror-keepalive"
    if not path.exists() or path.read_text(encoding="utf-8") != month:
        write_atomic(path, month.encode())


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync")
    publish = subparsers.add_parser("publish")
    publish.add_argument("--ref", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]

    if args.command == "sync":
        sync_projects(root, fetch_releases, download)
        update_keepalive(root)
    else:
        publish_manifest(root, args.ref)


if __name__ == "__main__":
    main()
