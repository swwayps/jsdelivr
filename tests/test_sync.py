import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "sync.py"
SPEC = importlib.util.spec_from_file_location("mirror_sync", SCRIPT)
mirror_sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mirror_sync)


def release(tag, *assets):
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": list(assets),
    }


def asset(asset_id, name, body=b"zip", updated_at="2026-09-02T00:00:00Z"):
    return {
        "id": asset_id,
        "name": name,
        "size": len(body),
        "created_at": updated_at,
        "updated_at": updated_at,
        "browser_download_url": f"https://example.invalid/{asset_id}/{name}",
    }


def zip_bytes(name="payload.txt", body=b"payload"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(name, body)
    return out.getvalue()


class ReleaseSelectionTests(unittest.TestCase):
    def test_temporary_name_is_ignored_until_v29_has_the_final_name(self):
        spec = mirror_sync.PROJECTS["lumen"]
        releases = [
            release("v2.9", asset(29, "lumen-linux-uploading.zip.tmp")),
            release("v2.8", asset(28, "lumen-linux.zip")),
        ]
        self.assertEqual(
            mirror_sync.select_candidate(releases, spec)["tag"], "v2.8"
        )

        releases[0]["assets"][0]["name"] = "lumen-linux.zip"
        self.assertEqual(
            mirror_sync.select_candidate(releases, spec)["tag"], "v2.9"
        )

    def test_new_release_is_selected_by_semver_not_api_order(self):
        spec = mirror_sync.PROJECTS["plugin"]
        releases = [
            release("v2.8", asset(28, "luatools-linux.zip")),
            release("v2.9", asset(29, "luatools-linux.zip")),
        ]
        self.assertEqual(
            mirror_sync.select_candidate(releases, spec)["tag"], "v2.9"
        )

    def test_missing_current_asset_never_regresses_the_mirror(self):
        current = {"tag": "v2.8", "id": 28}
        older = {"tag": "v2.7", "id": 27}
        self.assertFalse(mirror_sync.should_replace(current, older))

    def test_same_tag_reupload_with_a_new_asset_id_is_selected(self):
        current = {"tag": "v2.8", "id": 28}
        replacement = {"tag": "v2.8", "id": 2801}
        self.assertTrue(mirror_sync.should_replace(current, replacement))


class SyncTests(unittest.TestCase):
    def test_sync_writes_a_valid_zip_to_a_content_addressed_path(self):
        body = zip_bytes()
        candidate_asset = asset(29, "lumen-linux.zip", body)
        releases = [release("v2.9", candidate_asset)]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = mirror_sync.sync_projects(
                root,
                fetch_releases=lambda _repo: releases,
                download=lambda _url: body,
                projects={"lumen": mirror_sync.PROJECTS["lumen"]},
            )
            digest = hashlib.sha256(body).hexdigest()
            entry = state["components"]["lumen"]
            expected = (
                root
                / "releases"
                / "lumen"
                / "v2.9"
                / digest
                / "lumen-linux.zip"
            )

            self.assertEqual(entry["sha256"], digest)
            self.assertEqual(entry["path"], expected.relative_to(root).as_posix())
            self.assertEqual(expected.read_bytes(), body)
            self.assertEqual(
                expected.with_name(expected.name + ".sha256").read_text(),
                f"{digest}  lumen-linux.zip\n",
            )

    def test_invalid_zip_does_not_replace_the_last_good_state(self):
        previous = {
            "schema": 1,
            "components": {
                "lumen": {
                    "tag": "v2.8",
                    "id": 28,
                    "path": "releases/lumen/v2.8/old/lumen-linux.zip",
                }
            },
        }
        releases = [release("v2.9", asset(29, "lumen-linux.zip", b"broken"))]

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".mirror-state.json").write_text(json.dumps(previous))
            with self.assertRaises(zipfile.BadZipFile):
                mirror_sync.sync_projects(
                    root,
                    fetch_releases=lambda _repo: releases,
                    download=lambda _url: b"broken",
                    projects={"lumen": mirror_sync.PROJECTS["lumen"]},
                )
            persisted = json.loads((root / ".mirror-state.json").read_text())
            self.assertEqual(persisted, previous)

    def test_sync_keeps_only_current_and_previous_assets_in_the_branch(self):
        bodies = {
            "v2.8": zip_bytes(body=b"28"),
            "v2.9": zip_bytes(body=b"29"),
            "v3.0": zip_bytes(body=b"30"),
        }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for asset_id, tag in enumerate(bodies, start=28):
                body = bodies[tag]
                releases = [release(tag, asset(asset_id, "lumen-linux.zip", body))]
                mirror_sync.sync_projects(
                    root,
                    fetch_releases=lambda _repo, value=releases: value,
                    download=lambda _url, value=body: value,
                    projects={"lumen": mirror_sync.PROJECTS["lumen"]},
                )

            archives = sorted(
                path.relative_to(root).as_posix()
                for path in (root / "releases" / "lumen").rglob("*.zip")
            )
            self.assertEqual(len(archives), 2)
            self.assertTrue(any("/v2.9/" in f"/{path}" for path in archives))
            self.assertTrue(any("/v3.0/" in f"/{path}" for path in archives))

    def test_manifest_pins_downloads_to_the_asset_commit(self):
        state = {
            "schema": 1,
            "components": {
                "plugin": {
                    "repo": "swwayps/luatools-moon",
                    "tag": "v2.9",
                    "id": 29,
                    "asset_at": "2026-09-02T00:00:00Z",
                    "updated_at": "2026-09-02T00:00:00Z",
                    "size": 123,
                    "name": "luatools-linux.zip",
                    "sha256": "b" * 64,
                    "path": "releases/plugin/v2.9/hash/luatools-linux.zip",
                }
            },
        }
        commit = "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".mirror-state.json").write_text(json.dumps(state))
            manifest = mirror_sync.publish_manifest(root, commit)
            entry = manifest["components"]["plugin"]
            self.assertEqual(
                entry["url"],
                "https://cdn.jsdelivr.net/gh/swwayps/jsdelivr@"
                + commit
                + "/releases/plugin/v2.9/hash/luatools-linux.zip",
            )
            self.assertEqual(entry["sha256_url"], entry["url"] + ".sha256")
            self.assertNotIn("previous", entry)


if __name__ == "__main__":
    unittest.main()
