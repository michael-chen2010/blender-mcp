"""Worker orchestration and live background Blender prepare tests."""

from __future__ import annotations

import asyncio
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
            Path(job["observationCachePath"]).write_text(
                json.dumps(
                    {
                        "prepareId": job["prepareId"],
                        "sections": {
                            "STRUCTURE": {"objects": {"items": []}},
                            "GEOMETRY": {},
                            "MATERIALS": {"items": {"items": []}},
                            "DEFORMATION": {},
                        },
                    }
                ),
                encoding="utf-8",
            )
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
        assert kwargs["stdin"] is subprocess.DEVNULL
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


def test_asset_pipeline_registers_preview_and_retains_immutable_source_snapshot(
    monkeypatch, tmp_path: Path
):
    from blender_mcp import asset_pipeline
    from blender_mcp.prepared_artifacts import PreparedArtifactStore

    source = tmp_path / "retained-source.blend"
    source_bytes = b"BLENDER-source-before-prepare"
    source.write_bytes(source_bytes)
    output_dir = tmp_path / "prepare-retained"
    store = PreparedArtifactStore(ttl_seconds=60)

    class FakeProcess:
        returncode = 0

        def __init__(self, command):
            self.command = command

        def communicate(self, timeout=None):
            job_path = Path(self.command[-1])
            job = json.loads(job_path.read_text(encoding="utf-8"))
            Path(job["previewPath"]).write_bytes(b"\x89PNG\r\n\x1a\npreview")
            Path(job["observationCachePath"]).write_text(
                json.dumps(
                    {
                        "prepareId": job["prepareId"],
                        "sections": {
                            "STRUCTURE": {"objectCount": 1, "objects": {"items": []}},
                            "GEOMETRY": {"triangleCount": 1},
                            "MATERIALS": {"items": {"items": []}},
                            "DEFORMATION": {"hasArmature": False},
                        },
                    }
                ),
                encoding="utf-8",
            )
            Path(job["resultPath"]).write_text(
                json.dumps(
                    {
                        "status": "READY",
                        "prepareId": job["prepareId"],
                        "profile": job["profile"],
                        "observation": {
                            "prepareId": job["prepareId"],
                            "source": {"sourceSha256": _sha256(Path(job["sourcePath"]))},
                        },
                        "previewPath": job["previewPath"],
                        "timings": {"openMs": 1.0, "inspectMs": 2.0, "previewMs": 3.0},
                    }
                ),
                encoding="utf-8",
            )
            return ("", "")

    monkeypatch.setattr(
        asset_pipeline.subprocess,
        "Popen",
        lambda command, **_kwargs: FakeProcess(command),
    )

    result = asset_pipeline.prepare_blend_file(
        source,
        profile="METADATA",
        output_dir=output_dir,
        runtime=BlenderRuntime("C:/Blender/blender.exe", "ENV"),
        artifact_store=store,
    )

    assert [artifact["kind"] for artifact in result["artifacts"]] == ["PREVIEW"]
    preview_ref = result["artifacts"][0]
    assert store.resolve(preview_ref["artifact_id"]).read_bytes().startswith(b"\x89PNG")
    retained = store.retained_source_path(result["prepareId"])
    assert retained.read_bytes() == source_bytes
    observation_cache = store.observation_cache_path(result["prepareId"])
    assert observation_cache.name == "observation-cache.json"
    assert json.loads(observation_cache.read_text(encoding="utf-8"))["prepareId"] == result["prepareId"]

    source.write_bytes(b"BLENDER-source-changed-after-prepare")
    assert retained.read_bytes() == source_bytes


def test_async_batch_prepare_opens_original_source_to_preserve_relative_dependencies(monkeypatch, tmp_path: Path):
    from blender_mcp import asset_pipeline
    from blender_mcp.prepared_artifacts import PreparedArtifactStore

    source_dir = tmp_path / "asset"
    source_dir.mkdir()
    source = source_dir / "asset.blend"
    source_bytes = b"BLENDER-source-with-relative-texture"
    source.write_bytes(source_bytes)
    texture = source_dir / "Textures" / "albedo.png"
    texture.parent.mkdir()
    texture.write_bytes(b"texture")
    output_dir = tmp_path / "prepare"
    store = PreparedArtifactStore(ttl_seconds=60)
    opened_source_paths = []

    class FakeProcess:
        returncode = 0

        def __init__(self, command):
            self.command = command

        async def communicate(self):
            job_path = Path(self.command[-1])
            job = json.loads(job_path.read_text(encoding="utf-8"))
            opened_source = Path(job["sourcePath"])
            opened_source_paths.append(opened_source)
            assert opened_source.parent / "Textures" / "albedo.png" == texture
            Path(job["previewPath"]).write_bytes(b"\x89PNG\r\n\x1a\npreview")
            Path(job["observationCachePath"]).write_text(
                json.dumps({"prepareId": job["prepareId"], "sections": {}}),
                encoding="utf-8",
            )
            Path(job["resultPath"]).write_text(
                json.dumps(
                    {
                        "status": "READY",
                        "prepareId": job["prepareId"],
                        "profile": job["profile"],
                        "observation": {
                            "prepareId": job["prepareId"],
                            "source": {"sourceSha256": _sha256(opened_source)},
                        },
                        "previewPath": job["previewPath"],
                        "timings": {"openMs": 1.0, "inspectMs": 2.0, "previewMs": 3.0},
                    }
                ),
                encoding="utf-8",
            )
            return (b"", b"")

    async def fake_create_subprocess_exec(*command, **_kwargs):
        return FakeProcess(command)

    monkeypatch.setattr(asset_pipeline.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(
        asset_pipeline._prepare_blend_file_async(
            source,
            profile="METADATA",
            runtime=BlenderRuntime("C:/Blender/blender.exe", "ENV"),
            prepare_id="prepare-relative-dependencies",
            output_dir=output_dir,
            artifact_store=store,
            expected_source_fingerprint=_sha256(source),
        )
    )

    assert opened_source_paths == [source.resolve()]
    retained = store.retained_source_path(result["prepareId"])
    assert retained != source.resolve()
    assert retained.read_bytes() == source_bytes


def test_async_supplemental_render_uses_shared_worker_slot_and_worker_mode(monkeypatch, tmp_path: Path):
    from contextlib import asynccontextmanager
    from blender_mcp import asset_pipeline

    source = tmp_path / "retained.blend"
    source.write_bytes(b"BLENDER-retained")
    output = tmp_path / "front.png"
    events = []

    @asynccontextmanager
    async def fake_slot():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    class FakeProcess:
        returncode = 0

        def __init__(self, command):
            self.command = command

        async def communicate(self):
            events.append("communicate")
            job_path = Path(self.command[-1])
            job = json.loads(job_path.read_text(encoding="utf-8"))
            assert job["mode"] == "SUPPLEMENTAL_VIEW"
            assert job["prepareId"] == "prepare-supplemental"
            assert job["sourcePath"] == str(source)
            assert job["previewPath"] == str(output)
            assert job["view"] == "FRONT"
            output.write_bytes(b"\x89PNG\r\n\x1a\nfront")
            Path(job["resultPath"]).write_text(
                json.dumps(
                    {
                        "status": "READY",
                        "prepareId": job["prepareId"],
                        "view": job["view"],
                        "previewPath": job["previewPath"],
                        "timings": {"openMs": 1.0, "renderMs": 2.0, "totalMs": 3.0},
                    }
                ),
                encoding="utf-8",
            )
            return (b"worker stdout", b"")

        def kill(self):
            events.append("kill")

    async def fake_create_subprocess_exec(*command, **kwargs):
        events.append("spawn")
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is asyncio.subprocess.PIPE
        assert kwargs["stderr"] is asyncio.subprocess.PIPE
        return FakeProcess(command)

    monkeypatch.setattr(asset_pipeline, "asset_worker_slot", fake_slot, raising=False)
    monkeypatch.setattr(asset_pipeline.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    timings = asyncio.run(
        asset_pipeline.render_supplemental_view(
            source_path=source,
            output_path=output,
            view="FRONT",
            prepare_id="prepare-supplemental",
            runtime=BlenderRuntime("C:/Blender/blender.exe", "ENV"),
        )
    )

    assert output.read_bytes().startswith(b"\x89PNG")
    assert set(("openMs", "renderMs", "processSpawnMs", "totalMs")).issubset(timings)
    assert events == ["enter", "spawn", "communicate", "exit"]


def test_async_supplemental_render_cancellation_kills_process_and_releases_slot(monkeypatch, tmp_path: Path):
    from contextlib import asynccontextmanager
    from blender_mcp import asset_pipeline

    source = tmp_path / "retained.blend"
    source.write_bytes(b"BLENDER-retained")
    output = tmp_path / "left.png"
    events = []

    @asynccontextmanager
    async def fake_slot():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.communicate_calls = 0

        async def communicate(self):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                events.append("communicate")
                await asyncio.Future()
            events.append("drain")
            return (b"", b"")

        def kill(self):
            events.append("kill")
            self.returncode = -9

    async def fake_create_subprocess_exec(*_command, **_kwargs):
        events.append("spawn")
        return FakeProcess()

    async def scenario():
        task = asyncio.create_task(
            asset_pipeline.render_supplemental_view(
                source_path=source,
                output_path=output,
                view="LEFT",
                prepare_id="prepare-cancel",
                runtime=BlenderRuntime("C:/Blender/blender.exe", "ENV"),
            )
        )
        while "communicate" not in events:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(asset_pipeline, "asset_worker_slot", fake_slot, raising=False)
    monkeypatch.setattr(asset_pipeline.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    asyncio.run(scenario())

    assert events == ["enter", "spawn", "communicate", "kill", "drain", "exit"]
    assert not output.exists()


def test_publish_fact_comparison_rejects_payload_mismatch():
    expected = {
        "structure": {"objectCount": 1},
        "geometry": {
            "triangleCount": 12,
            "dimensionsMeters": {"x": 1.0, "y": 2.0, "z": 3.0},
        },
        "materials": {"materialCount": 1, "materialWorkflow": "PBR"},
    }
    reopened = json.loads(json.dumps(expected))
    reopened["geometry"]["triangleCount"] = 10

    with pytest.raises(worker.PublishValidationError) as exc_info:
        worker.compare_publish_facts(expected, reopened, dimension_tolerance=1e-5)
    assert exc_info.value.code == "PAYLOAD_FACT_MISMATCH"


def test_live_publish_prepare_writes_verified_self_contained_payload(tmp_path: Path):
    try:
        runtime = resolve_blender_runtime()
    except FileNotFoundError as exc:
        pytest.skip(f"Blender runtime unavailable: {exc}")

    from blender_mcp.asset_pipeline import prepare_blend_file
    from blender_mcp.prepared_artifacts import PreparedArtifactStore

    source = tmp_path / "publish-fixture.blend"
    fixture_script = tmp_path / "make-publish-fixture.py"
    fixture_script.write_text(
        f'''import bpy
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
for collection in list(bpy.data.collections):
    bpy.data.collections.remove(collection)
scene = bpy.context.scene
scene.unit_settings.system = "METRIC"
scene.unit_settings.scale_length = 0.01
asset_root = bpy.data.collections.new("ASSET_ROOT")
scene.collection.children.link(asset_root)
mesh = bpy.data.meshes.new("AssetMesh")
mesh.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)])
mesh.update()
material = bpy.data.materials.new("PublishPBR")
material.use_nodes = True
mesh.materials.append(material)
asset = bpy.data.objects.new("AssetBody", mesh)
asset.scale = (2.0, 3.0, 1.5)
asset_root.objects.link(asset)
solidify = asset.modifiers.new("EvaluatedSolidify", "SOLIDIFY")
solidify.thickness = 0.5
solidify.offset = 0.0
unrelated_mesh = bpy.data.meshes.new("UnrelatedMesh")
unrelated_mesh.from_pydata([(0,0,0),(1,0,0),(0,1,0)], [], [(0,1,2)])
unrelated = bpy.data.objects.new("UnrelatedMeshObject", unrelated_mesh)
unrelated.location = (100, 100, 100)
scene.collection.objects.link(unrelated)
camera_data = bpy.data.cameras.new("SourceCameraData")
camera = bpy.data.objects.new("SourceCamera", camera_data)
scene.collection.objects.link(camera)
light_data = bpy.data.lights.new("SourceLightData", "POINT")
light = bpy.data.objects.new("SourceLight", light_data)
scene.collection.objects.link(light)
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

    source_hash_before = _sha256(source)
    store = PreparedArtifactStore(ttl_seconds=120)
    result = prepare_blend_file(
        source,
        profile="PUBLISH",
        output_dir=tmp_path / "published",
        runtime=runtime,
        artifact_store=store,
        timeout=180,
    )
    assert _sha256(source) == source_hash_before

    observation = result["observation"]
    assert observation["observationScope"] == "PUBLISH_PAYLOAD"
    assert observation["materials"]["materialWorkflow"] == "PBR"
    assert observation["geometry"]["triangleCount"] > 2
    assert "UnrelatedMeshObject" not in observation["evidenceSummary"]["objectNames"]
    assert "SourceCamera" not in observation["evidenceSummary"]["objectNames"]
    assert "SourceLight" not in observation["evidenceSummary"]["objectNames"]

    assert result["validation"]["status"] == "PASSED"
    assert result["validation"]["factsVerifiedAfterReopen"] is True
    assert result["validation"]["dimensionToleranceMeters"] == pytest.approx(1e-5)
    assert set(("packMs", "payloadWriteMs", "payloadReopenMs", "hashMs")).issubset(
        result["timings"]
    )

    artifacts_by_kind = {artifact["kind"]: artifact for artifact in result["artifacts"]}
    assert set(artifacts_by_kind) == {"PAYLOAD", "PREVIEW"}
    payload_path = store.resolve(artifacts_by_kind["PAYLOAD"]["artifact_id"])
    assert payload_path.read_bytes().startswith(b"BLENDER")
    assert result["payloadEvidence"]["size"] == payload_path.stat().st_size
    assert result["payloadEvidence"]["sha256"] == _sha256(payload_path)
    assert result["payloadEvidence"]["compression"] == "NONE"
    assert result["payloadEvidence"]["factsVerifiedAfterReopen"] is True
    assert store.retained_source_path(result["prepareId"]) == payload_path.resolve()

    inspect_script = tmp_path / "inspect-payload.py"
    inspect_result = tmp_path / "payload-inspection.json"
    inspect_script.write_text(
        f'''import bpy, json
from mathutils import Vector
root = bpy.data.collections.get("ASSET_ROOT")
assert root is not None
objects = sorted(list(root.all_objects), key=lambda obj: obj.name)
depsgraph = bpy.context.evaluated_depsgraph_get()
points = []
triangles = 0
materials = set()
for obj in objects:
    if obj.type != "MESH":
        continue
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        mesh.calc_loop_triangles()
        triangles += len(mesh.loop_triangles)
        points.extend(evaluated.matrix_world @ Vector(vertex.co) for vertex in mesh.vertices)
        for material in mesh.materials:
            if material is not None:
                materials.add(material.name)
    finally:
        evaluated.to_mesh_clear()
scale_length = float(bpy.context.scene.unit_settings.scale_length or 1.0)
low = [min(point[i] for point in points) * scale_length for i in range(3)]
high = [max(point[i] for point in points) * scale_length for i in range(3)]
dimensions = {{axis: high[i] - low[i] for i, axis in enumerate(("x", "y", "z"))}}
workflow = "PBR"
for name in materials:
    material = bpy.data.materials[name]
    if not material.use_nodes or material.node_tree.nodes.get("Principled BSDF") is None:
        workflow = "NON_PBR"
preview_pollution = [obj.name for obj in objects if obj.type in {{"CAMERA", "LIGHT"}} or obj.name.startswith("__BLENDERMCP_PREVIEW")]
with open({str(inspect_result)!r}, "w", encoding="utf-8") as handle:
    json.dump({{
        "objectCount": len(objects),
        "objectNames": [obj.name for obj in objects],
        "triangleCount": triangles,
        "materialCount": len(materials),
        "materialWorkflow": workflow,
        "dimensionsMeters": dimensions,
        "previewPollution": preview_pollution,
        "scaleLength": scale_length,
        "objectScales": {{obj.name: list(obj.scale) for obj in objects}},
    }}, handle)
''',
        encoding="utf-8",
    )
    inspected = subprocess.run(
        [runtime.executable, "--background", str(payload_path), "--factory-startup", "--python", str(inspect_script)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert inspected.returncode == 0, f"stdout:\n{inspected.stdout}\nstderr:\n{inspected.stderr}"
    payload_facts = json.loads(inspect_result.read_text(encoding="utf-8"))

    assert payload_facts["previewPollution"] == []
    assert "UnrelatedMeshObject" not in payload_facts["objectNames"]
    assert payload_facts["scaleLength"] == pytest.approx(1.0)
    for scale in payload_facts["objectScales"].values():
        assert scale == pytest.approx([1.0, 1.0, 1.0], abs=1e-6)
    assert payload_facts["objectCount"] == observation["structure"]["objectCount"]
    assert payload_facts["triangleCount"] == observation["geometry"]["triangleCount"]
    assert payload_facts["materialCount"] == observation["materials"]["materialCount"]
    assert payload_facts["materialWorkflow"] == observation["materials"]["materialWorkflow"]
    for axis in ("x", "y", "z"):
        assert payload_facts["dimensionsMeters"][axis] == pytest.approx(
            observation["geometry"]["dimensionsMeters"][axis], abs=1e-5
        )


def test_pack_self_contained_rejects_linked_blender_library():
    fake_bpy = types.SimpleNamespace(
        data=types.SimpleNamespace(
            libraries=[types.SimpleNamespace(filepath="//linked-library.blend")],
            images=[],
        ),
        ops=types.SimpleNamespace(file=types.SimpleNamespace(pack_all=lambda: None)),
    )

    with pytest.raises(worker.PublishValidationError) as exc_info:
        worker.pack_self_contained(fake_bpy)
    assert exc_info.value.code == "PAYLOAD_NOT_SELF_CONTAINED"


def test_publish_scale_normalization_handles_static_hierarchical_empty_without_moving_child_world():
    child = types.SimpleNamespace(
        name="Child",
        type="MESH",
        scale=(1.0, 1.0, 1.0),
        children=[],
        matrix_world=["child-world"],
    )
    parent = types.SimpleNamespace(
        name="ScaledParent",
        type="EMPTY",
        scale=(2.0, 1.0, 1.0),
        children=[child],
    )
    fake_bpy = types.SimpleNamespace()

    worker._apply_publish_object_scales(fake_bpy, [parent, child])

    assert parent.scale == (1.0, 1.0, 1.0)
    assert child.matrix_world == ["child-world"]


def test_publish_scale_normalization_preserves_deforming_hierarchy_source_scales():
    child = types.SimpleNamespace(
        name="model",
        type="MESH",
        scale=(100.0, 100.0, 100.0),
        children=[],
        modifiers=[types.SimpleNamespace(type="ARMATURE")],
        constraints=[],
        data=types.SimpleNamespace(animation_data=None, shape_keys=None),
    )
    parent = types.SimpleNamespace(
        name="Armature",
        type="ARMATURE",
        scale=(0.01, 0.01, 0.01),
        children=[child],
        modifiers=[],
        constraints=[],
        animation_data=object(),
        data=types.SimpleNamespace(animation_data=None),
    )
    fake_bpy = types.SimpleNamespace()

    worker._apply_publish_object_scales(fake_bpy, [parent, child])

    assert parent.scale == (0.01, 0.01, 0.01)
    assert child.scale == (100.0, 100.0, 100.0)


def test_supplemental_view_job_opens_retained_source_once_and_skips_observation(monkeypatch, tmp_path: Path):
    source = tmp_path / "retained.blend"
    source.write_bytes(b"BLENDER-retained")
    preview = tmp_path / "front.png"
    result_path = tmp_path / "supplemental-result.json"
    open_calls = []
    render_calls = []

    fake_bpy = types.SimpleNamespace(
        ops=types.SimpleNamespace(
            wm=types.SimpleNamespace(open_mainfile=lambda filepath: open_calls.append(filepath))
        )
    )
    monkeypatch.setattr(worker, "_import_bpy", lambda: fake_bpy)
    monkeypatch.setattr(
        worker,
        "extract_observation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("supplemental render must not rerun observation extraction")
        ),
    )

    def render_view(_bpy, output_path, view):
        render_calls.append((Path(output_path), view))
        Path(output_path).write_bytes(b"\x89PNG\r\n\x1a\nfront")
        return {"width": 512, "height": 512, "view": view}

    monkeypatch.setattr(worker, "render_preview_view", render_view, raising=False)

    result = worker.run_job(
        {
            "mode": "SUPPLEMENTAL_VIEW",
            "prepareId": "prepare-supplemental",
            "sourcePath": str(source),
            "previewPath": str(preview),
            "resultPath": str(result_path),
            "view": "FRONT",
        }
    )

    assert open_calls == [str(source)]
    assert render_calls == [(preview, "FRONT")]
    assert result["status"] == "READY"
    assert result["prepareId"] == "prepare-supplemental"
    assert result["view"] == "FRONT"
    assert result["previewPath"] == str(preview)
    assert set(("openMs", "renderMs", "totalMs")).issubset(result["timings"])
    assert json.loads(result_path.read_text(encoding="utf-8"))["view"] == "FRONT"


def test_supplemental_worker_rejects_non_axis_view_before_open(monkeypatch, tmp_path: Path):
    source = tmp_path / "retained.blend"
    source.write_bytes(b"BLENDER-retained")
    open_calls = []
    fake_bpy = types.SimpleNamespace(
        ops=types.SimpleNamespace(
            wm=types.SimpleNamespace(open_mainfile=lambda filepath: open_calls.append(filepath))
        )
    )
    monkeypatch.setattr(worker, "_import_bpy", lambda: fake_bpy)

    with pytest.raises(ValueError, match="Unsupported supplemental view"):
        worker.run_job(
            {
                "mode": "SUPPLEMENTAL_VIEW",
                "prepareId": "prepare-bad-view",
                "sourcePath": str(source),
                "previewPath": str(tmp_path / "bad.png"),
                "resultPath": str(tmp_path / "bad-result.json"),
                "view": "BOTTOM",
            }
        )

    assert open_calls == []
