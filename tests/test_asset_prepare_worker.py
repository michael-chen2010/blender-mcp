"""Worker orchestration and live background Blender prepare tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import types
from pathlib import Path

import pytest

from blender_mcp.blender_runtime import BlenderRuntime, resolve_blender_runtime
from blender_mcp.bundled import asset_prepare_worker as worker


def test_worker_source_has_no_gui_viewport_or_source_save_dependency():
    source = Path(worker.__file__).read_text(encoding="utf-8")

    assert "bpy.context.screen" not in source
    assert "VIEW_3D" not in source
    assert "save_as_mainfile" not in source
    assert hasattr(worker, "render_main_preview")


def test_run_job_opens_source_once_and_records_stage_timings(monkeypatch, tmp_path: Path):
    source = tmp_path / "source.blend"
    source.write_bytes(b"BLENDER-fixture")
    preview = tmp_path / "main.png"
    result_path = tmp_path / "result.json"
    open_calls = []

    fake_bpy = types.SimpleNamespace(
        ops=types.SimpleNamespace(
            wm=types.SimpleNamespace(open_mainfile=lambda filepath: open_calls.append(filepath))
        )
    )
    monkeypatch.setattr(worker, "_import_bpy", lambda: fake_bpy)
    monkeypatch.setattr(
        worker,
        "extract_observation",
        lambda _bpy, **kwargs: {
            "schemaVersion": 1,
            "prepareId": kwargs["prepare_id"],
            "observationScope": "SOURCE_ASSET",
            "source": {"kind": "BLEND_FILE", "displayName": "source.blend"},
        },
    )

    def render(_bpy, _observation, output_path):
        Path(output_path).write_bytes(b"\x89PNG\r\n\x1a\npreview")
        return {"width": 512, "height": 512, "view": "THREE_QUARTER"}

    monkeypatch.setattr(worker, "render_main_preview", render)

    result = worker.run_job(
        {
            "prepareId": "prepare-1",
            "profile": "PREVIEW",
            "sourcePath": str(source),
            "sourceKind": "BLEND_FILE",
            "previewPath": str(preview),
            "resultPath": str(result_path),
        }
    )

    assert open_calls == [str(source)]
    assert result["observation"]["prepareId"] == "prepare-1"
    assert result["previewPath"] == str(preview)
    assert set(("openMs", "inspectMs", "previewMs", "totalMs")).issubset(
        result["timings"]
    )
    assert all(result["timings"][key] >= 0 for key in result["timings"])
    assert json.loads(result_path.read_text(encoding="utf-8"))["status"] == "READY"


def test_asset_pipeline_spawns_packaged_worker_and_adds_process_timing(
    monkeypatch, tmp_path: Path
):
    from blender_mcp import asset_pipeline

    source = tmp_path / "asset.blend"
    source.write_bytes(b"BLENDER-source")
    output_dir = tmp_path / "prepare"
    commands = []

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            job_path = Path(commands[0][-1])
            job = json.loads(job_path.read_text(encoding="utf-8"))
            Path(job["previewPath"]).write_bytes(b"\x89PNG\r\n\x1a\npreview")
            Path(job["resultPath"]).write_text(
                json.dumps(
                    {
                        "status": "READY",
                        "prepareId": job["prepareId"],
                        "profile": job["profile"],
                        "observation": {"prepareId": job["prepareId"]},
                        "previewPath": job["previewPath"],
                        "timings": {"openMs": 1.0, "inspectMs": 2.0, "previewMs": 3.0},
                    }
                ),
                encoding="utf-8",
            )
            return ("worker stdout", "")

    def popen(command, **kwargs):
        commands.append(command)
        assert kwargs["text"] is True
        return FakeProcess()

    monkeypatch.setattr(asset_pipeline.subprocess, "Popen", popen)
    result = asset_pipeline.prepare_blend_file(
        source,
        profile="PREVIEW",
        output_dir=output_dir,
        runtime=BlenderRuntime("C:/Blender/blender.exe", "ENV"),
    )

    command = commands[0]
    assert command[0] == "C:/Blender/blender.exe"
    assert "--background" in command
    assert "--factory-startup" in command
    assert "--python" in command
    assert command[-2:] == ["--job", str(output_dir / "job.json")]
    assert result["status"] == "READY"
    assert result["timings"]["processSpawnMs"] >= 0
    assert result["timings"]["totalMs"] >= result["timings"]["processSpawnMs"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def test_live_background_prepare_generates_main_preview_without_changing_source(tmp_path: Path):
    try:
        runtime = resolve_blender_runtime()
    except FileNotFoundError as exc:
        pytest.skip(f"Blender runtime unavailable: {exc}")

    from blender_mcp.asset_pipeline import prepare_blend_file

    source = tmp_path / "observation-fixture.blend"
    fixture_script = tmp_path / "make-fixture.py"
    fixture_script.write_text(
        f'''import bpy
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
scene = bpy.context.scene
material = bpy.data.materials.new("FixturePBR")
material.use_nodes = True
bsdf = material.node_tree.nodes.get("Principled BSDF")
bsdf.inputs["Base Color"].default_value = (0.15, 0.25, 0.35, 1.0)
bsdf.inputs["Metallic"].default_value = 0.6
bsdf.inputs["Roughness"].default_value = 0.3
for index in range(12):
    mesh = bpy.data.meshes.new(f"Mesh{{index:02d}}")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    mesh.materials.append(material)
    obj = bpy.data.objects.new(f"Part{{index:02d}}", mesh)
    obj.location = (index * 1.1, 0.0, 0.0)
    scene.collection.objects.link(obj)
bpy.ops.wm.save_as_mainfile(filepath={str(source)!r})
''',
        encoding="utf-8",
    )
    fixture = subprocess.run(
        [runtime.executable, "--background", "--factory-startup", "--python", str(fixture_script)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert fixture.returncode == 0, f"stdout:\n{fixture.stdout}\nstderr:\n{fixture.stderr}"

    before_hash = _sha256(source)
    result = prepare_blend_file(
        source,
        profile="PREVIEW",
        output_dir=tmp_path / "prepared",
        runtime=runtime,
        timeout=180,
    )
    after_hash = _sha256(source)

    assert after_hash == before_hash
    observation = result["observation"]
    assert observation["structure"]["objectCount"] == 12
    assert observation["structure"]["objects"]["totalCount"] == 12
    assert observation["geometry"]["triangleCount"] == 12
    assert observation["materials"]["materialWorkflow"] == "PBR"
    assert observation["previewEvidence"]["presetVersion"] == "asset-preview-v1"
    preview_path = Path(result["previewPath"])
    assert preview_path.is_file()
    assert preview_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert preview_path.stat().st_size > 1000
