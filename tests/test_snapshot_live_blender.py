"""Live Blender verification for zero-mutation current-selection snapshots."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from blender_mcp.blender_runtime import resolve_blender_runtime
from conftest import ROOT_ADDON


def test_live_snapshot_preserves_source_document_state(tmp_path: Path):
    try:
        runtime = resolve_blender_runtime()
    except FileNotFoundError as exc:
        pytest.skip(f"Blender runtime unavailable: {exc}")

    source_path = tmp_path / "source.blend"
    snapshot_path = tmp_path / "snapshot.blend"
    evidence_path = tmp_path / "snapshot-evidence.json"
    script_path = tmp_path / "snapshot-live.py"

    script_path.write_text(
        f'''import importlib.util
import json
import bpy

for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)

scene = bpy.context.scene

def mesh_object(name, offset):
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = offset
    scene.collection.objects.link(obj)
    return obj

root = mesh_object("Root", (1.0, 2.0, 3.0))
child = mesh_object("Child", (0.5, 0.0, 0.0))
child.parent = root
loose = mesh_object("Loose", (-2.0, 0.0, 0.0))

camera_data = bpy.data.cameras.new("UnrelatedCameraData")
camera = bpy.data.objects.new("UnrelatedCamera", camera_data)
scene.collection.objects.link(camera)
light_data = bpy.data.lights.new("UnrelatedLightData", type="POINT")
light = bpy.data.objects.new("UnrelatedLight", light_data)
scene.collection.objects.link(light)

bpy.ops.wm.save_as_mainfile(filepath={str(source_path)!r})
root.location.x = 1.25

for obj in bpy.context.selected_objects:
    obj.select_set(False)
root.select_set(True)
loose.select_set(True)
bpy.context.view_layer.objects.active = root

def vec(value):
    return [round(float(component), 12) for component in value]

def capture():
    objects = sorted(bpy.data.objects, key=lambda obj: obj.name)
    return {{
        "filepath": bpy.data.filepath,
        "scene": bpy.context.scene.name,
        "selected": sorted(obj.name for obj in bpy.context.selected_objects),
        "active": bpy.context.view_layer.objects.active.name if bpy.context.view_layer.objects.active else None,
        "objectCount": len(objects),
        "objectNames": [obj.name for obj in objects],
        "transforms": {{
            obj.name: {{
                "location": vec(obj.location),
                "rotation": vec(obj.rotation_euler),
                "scale": vec(obj.scale),
            }}
            for obj in objects
        }},
        "isDirty": bool(bpy.data.is_dirty),
    }}

before = capture()
spec = importlib.util.spec_from_file_location("blender_mcp_snapshot_live", {str(ROOT_ADDON)!r})
addon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(addon)
server = addon.BlenderMCPServer()
api_result = server.create_blend_snapshot(
    filepath={str(snapshot_path)!r},
    selectionMode="CURRENT_SELECTION",
    closureMode="ASSET_CLOSURE",
)
after = capture()

with bpy.data.libraries.load({str(snapshot_path)!r}, link=False) as (data_from, _data_to):
    snapshot_objects = sorted(data_from.objects)

with open({str(evidence_path)!r}, "w", encoding="utf-8") as handle:
    json.dump(
        {{
            "before": before,
            "after": after,
            "apiResult": api_result,
            "snapshotObjects": snapshot_objects,
        }},
        handle,
        ensure_ascii=False,
        indent=2,
    )
''',
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            runtime.executable,
            "--background",
            "--factory-startup",
            "--python",
            str(script_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, (
        f"Blender live snapshot failed with exit {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["after"] == evidence["before"]
    assert set(evidence["snapshotObjects"]) >= {"Root", "Child", "Loose"}
    assert "UnrelatedCamera" not in evidence["snapshotObjects"]
    assert "UnrelatedLight" not in evidence["snapshotObjects"]
    assert set(evidence["apiResult"]["rootObjectNames"]) == {"Root", "Loose"}
    assert evidence["apiResult"]["sourceBlendPath"] == str(source_path)
