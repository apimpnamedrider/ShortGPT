"""Procedural, zero-API mechanical CGI renderer for Shorts Forge.

Runs inside Blender in background mode. It creates a reusable 3D mechanical
transmission assembly and animates real geometry, gears, shafts, clutch packs,
a cutaway housing, and a camera move. It is intentionally not an image slideshow.
"""
import bpy
import math
import json
import os
import sys
from mathutils import Vector

def arg_value(flag, default=None):
    args = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return default

TOPIC = arg_value("--topic", "automatic transmission")
STORYBOARD_PATH = arg_value("--storyboard")
OUTPUT_PATH = arg_value("--output", "/tmp/shortsforge-cgi.mp4")
FPS = int(arg_value("--fps", "12"))
SECONDS = int(arg_value("--seconds", "60"))
WIDTH = int(arg_value("--width", "540"))
HEIGHT = int(arg_value("--height", "960"))

with open(STORYBOARD_PATH, "r", encoding="utf-8") as f:
    STORYBOARD = json.load(f)
SCENES = STORYBOARD.get("scenes", []) if isinstance(STORYBOARD, dict) else []

# Clean startup file.
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
for datablocks in (bpy.data.meshes, bpy.data.curves, bpy.data.materials, bpy.data.cameras, bpy.data.lights):
    for block in list(datablocks):
        if block.users == 0:
            datablocks.remove(block)

def material(name, color, metallic=0.0, roughness=0.35):
    m = bpy.data.materials.new(name)
    m.diffuse_color = (*color, 1.0)
    m.use_nodes = True
    bs = m.node_tree.nodes.get("Principled BSDF")
    if bs:
        bs.inputs["Base Color"].default_value = (*color, 1.0)
        bs.inputs["Metallic"].default_value = metallic
        bs.inputs["Roughness"].default_value = roughness
    return m

steel = material("Machined satin steel", (0.48, 0.55, 0.63), 0.82, 0.24)
bright = material("Polished aluminium", (0.78, 0.82, 0.87), 0.78, 0.19)
dark = material("Graphite steel", (0.095, 0.13, 0.18), 0.72, 0.3)
copper = material("Warm clutch steel", (0.55, 0.25, 0.10), 0.72, 0.28)
blue = material("Cool blue anodised metal", (0.035, 0.22, 0.42), 0.72, 0.24)
rubber = material("Seal rubber", (0.025, 0.03, 0.04), 0.05, 0.68)
floor_mat = material("Midnight studio", (0.012, 0.023, 0.045), 0.18, 0.48)

def finish(obj, mat, bevel=0.0):
    obj.data.materials.append(mat)
    if bevel:
        mod = obj.modifiers.new("Precision edge radii", "BEVEL")
        mod.width = bevel
        mod.segments = 2
        obj.modifiers.new("Weighted machined normals", "WEIGHTED_NORMAL")
    return obj

def cube(name, loc, scale, mat, bevel=0.08):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    o = bpy.context.object
    o.name = name
    o.dimensions = scale
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return finish(o, mat, bevel)

def cylinder_x(name, x, radius, depth, mat, vertices=64):
    bpy.ops.mesh.primitive_cylinder_add(vertices=vertices, radius=radius, depth=depth,
                                        location=(x, 0, 0), rotation=(0, math.pi / 2, 0))
    o = bpy.context.object
    o.name = name
    return finish(o, mat, 0.025)

def torus_x(name, x, major, minor, mat):
    bpy.ops.mesh.primitive_torus_add(major_segments=64, minor_segments=12,
                                     location=(x, 0, 0), rotation=(0, math.pi / 2, 0),
                                     major_radius=major, minor_radius=minor)
    o = bpy.context.object
    o.name = name
    return finish(o, mat)

def gear(name, x, radius, thickness, teeth, mat):
    # Extruded involute-like tooth outline, axis along X.
    n = teeth * 4
    outline = []
    for i in range(n):
        a = 2.0 * math.pi * i / n
        phase = i % 4
        rr = radius * (1.0 if phase in (1, 2) else 0.91)
        outline.append((rr * math.cos(a), rr * math.sin(a)))
    verts = []
    for xx in (-thickness / 2, thickness / 2):
        for y, z in outline:
            verts.append((xx, y, z))
    faces = []
    for i in range(n):
        j = (i + 1) % n
        faces.append((i, j, n + j, n + i))
    faces.append(tuple(range(n - 1, -1, -1)))
    faces.append(tuple(range(n, 2 * n)))
    mesh = bpy.data.meshes.new(name + " precision tooth mesh")
    mesh.from_pydata(verts, [], faces)
    mesh.materials.append(mat)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = (x, 0, 0)
    bpy.context.collection.objects.link(obj)
    bevel = obj.modifiers.new("Gear tooth edge bevel", "BEVEL")
    bevel.width = 0.012
    bevel.segments = 2
    obj.modifiers.new("Gear weighted normals", "WEIGHTED_NORMAL")
    return obj

def add_bolt_ring(x, radius, count=12):
    for i in range(count):
        a = 2 * math.pi * i / count
        bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=6, radius=0.045,
            location=(x, radius * math.cos(a), radius * math.sin(a)))
        finish(bpy.context.object, bright, 0.0)

def make_housing():
    # Open-front split case: top/bottom rails and side cheeks frame the visible internals.
    cube("Transmission lower sump rail", (0, 0, -1.38), (5.8, 2.65, 0.28), blue, 0.16)
    cube("Transmission upper cutaway rail", (0, 0, 1.35), (5.8, 2.65, 0.22), steel, 0.12)
    cube("Rear casing cheek", (-2.72, 0, 0), (0.3, 2.65, 2.7), steel, 0.14)
    cube("Front open casing rim", (2.72, 0, 0), (0.3, 2.65, 2.7), bright, 0.14)
    cube("Lower mounting flange", (0, 0, -1.58), (6.2, 3.0, 0.16), dark, 0.05)
    for x in (-2.25, -1.2, 0.0, 1.2, 2.25):
        for y in (-1.27, 1.27):
            cylinder = cube("Housing rib", (x, y, 0), (0.12, 0.16, 2.25), bright, 0.035)
    # Case mounting bosses.
    for x in (-2.4, 2.4):
        for z in (-1.0, 1.0):
            bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=0.16,
                                                 location=(x, -1.4, z))
            finish(bpy.context.object, steel)

make_housing()

animated = []
# Torque converter / input assembly.
animated.append((cylinder_x("Torque converter rear plate", -2.15, 1.02, 0.26, dark), 1.2))
animated.append((torus_x("Torque converter polished shell", -2.34, 0.78, 0.22, bright), 1.2))
animated.append((torus_x("Converter stator ring", -1.98, 0.55, 0.11, copper), -0.7))
animated.append((cylinder_x("Converter central hub", -1.72, 0.24, 0.75, steel), 1.2))
for i in range(24):
    a = 2 * math.pi * i / 24
    # Curved-looking converter vanes arranged radially.
    o = cube("Torque converter vane %02d" % i, (-2.05, 0.69*math.cos(a), 0.69*math.sin(a)),
             (0.24, 0.12, 0.28), bright, 0.025)
    o.rotation_euler.x = a
    animated.append((o, 0.0))

# Input/output shafts.
animated.append((cylinder_x("Main input shaft", -0.9, 0.16, 4.9, bright), 1.1))
animated.append((cylinder_x("Output shaft", 0.85, 0.13, 5.4, steel), -0.8))
# Planetary gearset and compound gears.
for idx, (x, rad, teeth, mat) in enumerate([
    (-0.95, 0.64, 30, steel), (-0.15, 0.84, 42, bright),
    (0.62, 0.56, 26, copper), (1.35, 0.76, 36, steel),
]):
    g = gear("Planetary / compound gear %d" % (idx+1), x, rad, 0.24, teeth, mat)
    animated.append((g, (1 if idx % 2 == 0 else -1) * (1.5 + idx*0.12)))
    torus_x("Gear retaining ring %d" % idx, x, rad*0.82, 0.045, dark)
    add_bolt_ring(x, rad*0.91, 10)

# Planet pinions on a carrier.
for i in range(4):
    a = i * math.pi/2
    y, z = 0.48*math.cos(a), 0.48*math.sin(a)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=0.25,
                                         location=(0.25, y, z))
    finish(bpy.context.object, copper)
    pinion = gear("Planet pinion %d" % (i+1), 0.25, 0.25, 0.22, 14, bright)
    pinion.location.y = y
    pinion.location.z = z
    animated.append((pinion, -2.0))

# Multi-plate clutch packs: alternating steel and friction plates.
for pack, x in enumerate((-0.35, 1.62)):
    for j in range(8):
        px = x + (j - 3.5) * 0.075
        mat = copper if j % 2 == 0 else bright
        animated.append((torus_x("Clutch pack %d plate %d" % (pack+1, j+1), px, 0.88 - 0.05*(j%2), 0.055, mat),
                         0.12 if j % 2 else -0.12))
    torus_x("Clutch drum %d" % (pack+1), x, 1.04, 0.12, dark)

# Valve body beneath the gear train, with visible solenoids and channels.
cube("Hydraulic valve body", (0.35, -0.18, -1.03), (2.45, 1.55, 0.3), bright, 0.09)
for i in range(7):
    x = -0.62 + i*0.31
    cube("Valve channel %d" % i, (x, -0.18, -0.84), (0.08, 1.18, 0.055), dark, 0.015)
    cylinder_x("Valve solenoid %d" % i, x, 0.11, 0.28, copper, 24)
# Bearings and seals.
for x in (-2.55, -1.55, 2.25):
    torus_x("Large bearing race", x, 0.36, 0.075, bright)
    torus_x("Bearing seal", x+0.06, 0.26, 0.035, rubber)
    for i in range(10):
        a = 2*math.pi*i/10
        bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=8, radius=0.065,
            location=(x, 0.36*math.cos(a), 0.36*math.sin(a)))
        finish(bpy.context.object, steel)

# Additional exposed fasteners and machining detail.
for i in range(16):
    a = 2*math.pi*i/16
    bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=6, radius=0.055,
        location=(2.9, 1.15*math.cos(a), 1.15*math.sin(a)))
    finish(bpy.context.object, dark)

# Stage plinth and studio floor.
cube("Floating presentation plinth", (0, 0, -1.82), (6.8, 3.5, 0.12), dark, 0.12)
cube("Studio floor", (0, 0, -2.02), (200, 200, 0.12), floor_mat, 0.0)

# Lighting: studio key, cool fill, warm accent.
def area(name, loc, power, color, size, target=(0,0,0)):
    bpy.ops.object.light_add(type="AREA", location=loc)
    l = bpy.context.object
    l.name = name
    l.data.energy = power
    l.data.color = color
    l.data.shape = "DISK"
    l.data.size = size
    l.rotation_euler = (Vector(target)-l.location).to_track_quat("-Z", "Y").to_euler()
    return l
area("Large cool key", (1,-6,7), 1800, (0.62,0.78,1.0), 7)
area("Blue rim strip", (-4,3,3), 2200, (0.18,0.42,1.0), 5)
area("Warm machined edge", (4,2,-0.2), 900, (1.0,0.42,0.16), 3)

# Camera with a real slow orbital reveal.
bpy.ops.object.camera_add(location=(7.8, -11.8, 6.6))
camera = bpy.context.object
camera.name = "Mechanical CGI camera"
camera.data.lens = 52
camera.rotation_euler = (Vector((0,0,-0.1))-camera.location).to_track_quat("-Z", "Y").to_euler()
bpy.context.scene.camera = camera
camera.keyframe_insert(data_path="location", frame=1)
camera.location = (6.3, -12.8, 5.4)
camera.rotation_euler = (Vector((0,0,-0.1))-camera.location).to_track_quat("-Z", "Y").to_euler()
camera.keyframe_insert(data_path="location", frame=FPS*SECONDS)
camera.keyframe_insert(data_path="rotation_euler", frame=FPS*SECONDS)

# Animate each real component: continuous rotation and a coordinated exploded/reassembly pulse.
for index, (obj, speed) in enumerate(animated):
    obj.rotation_mode = "XYZ"
    base = obj.location.copy()
    obj.keyframe_insert(data_path="location", frame=1)
    obj.keyframe_insert(data_path="rotation_euler", frame=1)
    obj.rotation_euler.x += speed * (SECONDS / (2*math.pi))
    obj.keyframe_insert(data_path="rotation_euler", frame=FPS*SECONDS)
    # Mid-film exploded assembly: alternate parts separate along the real shaft axis.
    direction = -1.0 if index % 2 else 1.0
    separation = direction * (0.42 if abs(speed) > 0.5 else 0.28)
    obj.location.x = base.x + separation
    obj.keyframe_insert(data_path="location", frame=FPS*15)
    obj.location.x = base.x
    obj.keyframe_insert(data_path="location", frame=FPS*30)
    obj.keyframe_insert(data_path="location", frame=FPS*SECONDS)

# Linear interpolation prevents unexpected easing stalls.
for obj in bpy.context.scene.objects:
    if obj.animation_data and obj.animation_data.action:
        for fc in obj.animation_data.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = "LINEAR"

scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
scene.display.shading.light = "STUDIO"
scene.display.shading.studio_light = "paint.sl"
scene.display.shading.color_type = "MATERIAL"
scene.display.shading.show_shadows = True
scene.display.shading.show_cavity = True
scene.display.shading.cavity_type = "BOTH"
scene.display.shading.curvature_ridge_factor = 1.35
scene.display.shading.curvature_valley_factor = 1.1
scene.display.shading.background_type = "WORLD"
scene.display.shading.background_color = (0.006, 0.012, 0.025)
scene.world.color = (0.006, 0.012, 0.025)
scene.render.resolution_x = WIDTH
scene.render.resolution_y = HEIGHT
scene.render.resolution_percentage = 100
scene.render.fps = FPS
scene.frame_start = 1
scene.frame_end = FPS * SECONDS
scene.render.image_settings.file_format = "FFMPEG"
scene.render.ffmpeg.format = "MPEG4"
scene.render.ffmpeg.codec = "H264"
scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
scene.render.ffmpeg.ffmpeg_preset = "REALTIME"
scene.render.filepath = OUTPUT_PATH
scene.render.use_file_extension = True
scene.render.film_transparent = False
scene.render.image_settings.color_mode = "RGB"
scene.render.threads_mode = "FIXED"
scene.render.threads = 1
scene.render.resolution_percentage = 100
scene.render.image_settings.color_depth = "8"
scene.render.use_compositing = False
scene.render.use_sequencer = False
scene.render.filepath = OUTPUT_PATH
scene.frame_set(1)
bpy.ops.render.render(animation=True)
print("MECHCGI_RENDER_COMPLETE " + OUTPUT_PATH, flush=True)
