import bpy, sys, json, math, os, re
import subprocess
from mathutils import Vector

argv = sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else []
storyboard_path, output_path = argv[0], argv[1]
with open(storyboard_path, "r", encoding="utf-8") as f:
    data = json.load(f)
shots = data.get("scenes", [])
if not 8 <= len(shots) <= 10:
    raise RuntimeError("Mechanical CGI renderer requires 8 to 10 narration-matched shots.")

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
scene = bpy.context.scene
scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = 1
scene.cycles.use_denoising = False
scene.render.threads_mode = "FIXED"
scene.render.threads = 1
scene.render.resolution_x = 540
scene.render.resolution_y = 960
scene.render.resolution_percentage = 100
scene.render.fps = 8
scene.render.fps_base = 1
scene.frame_start = 1
scene.frame_end = 480
scene.render.image_settings.file_format = "FFMPEG"
scene.render.ffmpeg.format = "MPEG4"
scene.render.ffmpeg.codec = "H264"
scene.render.ffmpeg.constant_rate_factor = "MEDIUM"
scene.render.ffmpeg.ffmpeg_preset = "REALTIME"
scene.render.filepath = output_path
scene.render.image_settings.color_mode = "RGB"
scene.view_settings.view_transform = "Standard"
scene.view_settings.look = "Medium High Contrast"
scene.view_settings.exposure = 0
scene.view_settings.gamma = 1
scene.render.film_transparent = False

def material(name, color, metallic=0.75, roughness=0.26, emission=0.0):
    m = bpy.data.materials.new(name)
    m.diffuse_color = (*color, 1)
    m.use_nodes = True
    p = m.node_tree.nodes.get("Principled BSDF")
    p.inputs["Base Color"].default_value = (*color, 1)
    p.inputs["Metallic"].default_value = metallic
    p.inputs["Roughness"].default_value = roughness
    if emission:
        p.inputs["Emission"].default_value = (*color, 1)
        p.inputs["Emission Strength"].default_value = emission
    return m

steel = material("Satin machined stainless steel", (0.55,0.63,0.70), .88, .22)
aluminium = material("Brushed aluminium housing", (0.36,0.46,0.55), .82, .27)
bright = material("Polished bearing steel", (0.82,0.87,0.92), .92, .16)
graphite = material("Dark cast alloy", (0.055,0.085,0.12), .68, .30)
friction = material("Clutch friction composite", (0.16,0.19,0.22), .35, .38)
amber = material("Amber hydraulic highlight", (1.0,0.28,0.035), .30, .23, .22)
cyan = material("Cool blue fluid", (0.015,0.38,0.85), .25, .18, .18)
black = material("Deep navy studio", (0.006,0.014,0.028), .05, .8)

world = bpy.data.worlds.new("Midnight engineering studio") if not bpy.data.worlds else bpy.data.worlds[0]
scene.world = world
world.use_nodes = True
world.node_tree.nodes["Background"].inputs[0].default_value = (0.004,0.012,0.026,1)
world.node_tree.nodes["Background"].inputs[1].default_value = 0.22

def bevel(obj, width=.025, segments=2):
    mod = obj.modifiers.new("Precision machined edge", "BEVEL")
    mod.width = width
    mod.segments = segments
    obj.modifiers.new("Weighted metal normals", "WEIGHTED_NORMAL")
    return obj

def cube(name, loc, dims, mat, width=.025):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    o = bpy.context.object
    o.name = name
    o.dimensions = dims
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    o.data.materials.append(mat)
    return bevel(o, width)

def cylinder_x(name, loc, radius, depth, mat, vertices=32):
    bpy.ops.mesh.primitive_cylinder_add(vertices=vertices, radius=radius, depth=depth, location=loc, rotation=(0,math.pi/2,0))
    o = bpy.context.object
    o.name = name
    o.data.materials.append(mat)
    bevel(o, .012, 2)
    return o

def torus_x(name, loc, major, minor, mat):
    bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor, major_segments=40, minor_segments=8, location=loc, rotation=(0,math.pi/2,0))
    o = bpy.context.object
    o.name = name
    o.data.materials.append(mat)
    return o

def parent_group(name, objects, origin):
    bpy.ops.object.empty_add(type="PLAIN_AXES", location=origin)
    root = bpy.context.object
    root.name = name + " animated assembly"
    for obj in objects:
        world_matrix = obj.matrix_world.copy()
        obj.parent = root
        obj.matrix_world = world_matrix
    return root

def gear_mesh(name, center, radius, width, mat, teeth=12):
    x,y,z = center
    parts = [cylinder_x(name + " gear body", center, radius*.82, width, mat, 48),
             cylinder_x(name + " central hub", (x,y,z), radius*.25, width*1.32, bright, 32),
             torus_x(name + " machined rim", (x,y,z), radius*.78, .035, bright)]
    # Teeth are real separate solids distributed around the shaft axis.
    for n in range(teeth):
        a = math.tau*n/teeth
        tooth = cube(name + " precision tooth",
                     (x, y+math.cos(a)*radius*.91, z+math.sin(a)*radius*.91),
                     (width*.88, radius*.34, radius*.24), bright, .012)
        tooth.rotation_euler.x = a
        parts.append(tooth)
    return parts

def shaft_parts(name, center, length=2.25, radius=.12):
    x,y,z = center
    parts = [cylinder_x(name + " hardened shaft", center, radius, length, bright, 32)]
    for dx in (-length*.34, length*.28):
        parts.append(cylinder_x(name + " splined collar", (x+dx,y,z), radius*1.65, .22, steel, 32))
        parts.append(torus_x(name + " retaining ring", (x+dx+.12,y,z), radius*1.35, .025, amber))
    return parts

def bearing_parts(name, center, radius=.44):
    x,y,z = center
    parts = [cylinder_x(name + " outer race", center, radius, .25, aluminium, 48),
             cylinder_x(name + " inner race", center, radius*.52, .32, bright, 40),
             torus_x(name + " outer race lip", (x,y,z), radius*.91, .045, bright),
             torus_x(name + " inner race lip", (x,y,z), radius*.57, .04, steel)]
    for n in range(9):
        a = math.tau*n/9
        bpy.ops.mesh.primitive_uv_sphere_add(segments=12, ring_count=8, radius=.075,
            location=(x,y+math.cos(a)*radius*.73,z+math.sin(a)*radius*.73))
        ball = bpy.context.object
        ball.name = name + " polished roller"
        ball.data.materials.append(bright)
        parts.append(ball)
    return parts

def clutch_parts(name, center, radius=.52):
    x,y,z = center
    parts = []
    for n in range(7):
        dx = (n-3)*.105
        plate_mat = friction if n%2 == 0 else bright
        parts.append(cylinder_x(name + (" friction plate" if n%2==0 else " steel separator"),
                                (x+dx,y,z), radius if n%2==0 else radius*.91, .065, plate_mat, 48))
        parts.append(torus_x(name + " plate edge", (x+dx,y,z), radius*.94, .018, amber if n%2==0 else steel))
    parts.append(cylinder_x(name + " clutch drum", center, radius*1.12, .82, aluminium, 48))
    return parts

def housing_parts(name, center):
    x,y,z = center
    # Open-front transmission casing: base, roof rails and end walls reveal internals.
    return [
        cube(name + " lower cast sump", (x,y,z-.34), (2.8,1.55,.20), aluminium,.09),
        cube(name + " rear case wall", (x,y+.66,z+.15), (2.8,.16,1.18), aluminium,.07),
        cube(name + " upper case rail", (x,y,z+.78), (2.8,1.45,.14), aluminium,.06),
        cube(name + " left end flange", (x-1.31,y,z+.18), (.18,1.48,1.0), steel,.06),
        cube(name + " right end flange", (x+1.31,y,z+.18), (.18,1.48,1.0), steel,.06),
        torus_x(name + " output bearing seat", (x+1.42,y,z+.20), .40,.065,bright)
    ]

def converter_parts(name, center):
    x,y,z = center
    parts = [cylinder_x(name + " converter shell", center, .76,.65,aluminium,56),
             torus_x(name + " outer converter rim", (x,y,z), .70,.08,bright),
             cylinder_x(name + " turbine hub", (x,y,z), .22,.92,steel,32)]
    for n in range(18):
        a=math.tau*n/18
        vane=cube(name + " curved turbine vane",
            (x,y+math.cos(a)*.47,z+math.sin(a)*.47),(.30,.12,.24),bright,.025)
        vane.rotation_euler.x=a
        parts.append(vane)
    return parts

def valve_body_parts(name, center):
    x,y,z=center
    parts=[cube(name+" machined hydraulic valve body",center,(1.55,.86,.34),aluminium,.055)]
    for n in range(5):
        px=x-.58+n*.29
        parts.append(cylinder_x(name+" solenoid bore",(px,y,z+.20),.105,.22,graphite,24))
        parts.append(cylinder_x(name+" valve spool",(px,y,z+.34),.07,.42,bright,24))
        parts.append(torus_x(name+" oil channel",(px,y,z+.18),.15,.018,amber))
    return parts

def pump_parts(name, center):
    x,y,z=center
    parts=[cylinder_x(name+" pump rotor",center,.48,.24,steel,48),
           torus_x(name+" pump stator",(x,y,z),.42,.07,bright)]
    for n in range(10):
        a=math.tau*n/10
        vane=cube(name+" pump vane",(x,y+math.cos(a)*.30,z+math.sin(a)*.30),(.18,.09,.22),amber,.015)
        vane.rotation_euler.x=a
        parts.append(vane)
    return parts

def generic_mechanical_part(name, center):
    # A unique machined metal part fallback, never a flat illustration.
    x,y,z=center
    return [cube(name+" forged alloy block",center,(.92,.68,.58),aluminium,.08),
            cylinder_x(name+" machined port",(x,y,z),.22,.98,bright,32),
            torus_x(name+" retaining lip",(x,y,z),.29,.035,steel)]

def build_named_part(label, center):
    k=label.lower()
    if any(t in k for t in ("housing","casing","gearbox case","transmission case","bell housing")):
        return housing_parts(label,center)
    if any(t in k for t in ("torque converter","converter","turbine","impeller")):
        return converter_parts(label,center)
    if any(t in k for t in ("clutch","friction plate","clutch pack","separator plate")):
        return clutch_parts(label,center)
    if any(t in k for t in ("bearing","race","roller")):
        return bearing_parts(label,center)
    if any(t in k for t in ("shaft","axle","input shaft","output shaft","splined")):
        return shaft_parts(label,center)
    if any(t in k for t in ("valve body","solenoid","hydraulic valve","mechatronic")):
        return valve_body_parts(label,center)
    if any(t in k for t in ("pump","oil pump")):
        return pump_parts(label,center)
    if any(t in k for t in ("gear","planetary","sun gear","ring gear","pinion","differential")):
        return gear_mesh(label,center,.48,.22,steel,14)
    if any(t in k for t in ("oil","fluid","hydraulic")):
        return [cylinder_x(label+" fluid channel",center,.10,1.65,cyan,24),
                torus_x(label+" flow ring",(center[0],center[1],center[2]),.22,.045,cyan)]
    return generic_mechanical_part(label,center)

def add_area(name, location, energy, size, color):
    bpy.ops.object.light_add(type="AREA", location=location)
    o=bpy.context.object
    o.name=name
    o.data.energy=energy
    o.data.shape="DISK"
    o.data.size=size
    o.data.color=color
    o.rotation_euler=(Vector((0,0,0))-o.location).to_track_quat("-Z","Y").to_euler()

add_area("Large cool overhead softbox",(1,-3,7),850,6,(.72,.84,1.0))
add_area("Blue metallic rim light",(-5,1,3),950,4,(.22,.48,1.0))
add_area("Warm precision highlight",(4,4,4),650,3,(1.0,.48,.20))
cube("Dark studio floor",(0,0,-1.12),(200,200,.15),black,.01)

bpy.ops.object.empty_add(type="PLAIN_AXES", location=(0,0,0))
camera_rig=bpy.context.object
bpy.ops.object.camera_add(location=(0,-9,4.2))
camera=bpy.context.object
camera.name="Mechanical CGI cinema camera"
camera.parent=camera_rig
camera.location=(0,-9,4.2)
camera.rotation_euler=(Vector((0,0,0))-Vector(camera.location)).to_track_quat("-Z","Y").to_euler()
camera.data.type="ORTHO"
camera.data.ortho_scale=7.6
scene.camera=camera

def parse_time(value, fallback_start, fallback_end):
    nums=re.findall(r"\d+(?:\.\d+)?",str(value or ""))
    if len(nums)>=2: return float(nums[0]),float(nums[1])
    if len(nums)==1: return fallback_start,float(nums[0])
    return fallback_start,fallback_end

cursor=0.0
timeline=[]
for i,shot in enumerate(shots):
    start,end=parse_time(shot.get("time"),cursor,60*(i+1)/len(shots))
    start=max(cursor,start)
    end=max(start+1,min(60,end))
    timeline.append((shot,start,end))
    cursor=end
timeline[0]=(timeline[0][0],0.0,timeline[0][2])
for i in range(len(timeline)-1):
    timeline[i]=(timeline[i][0],timeline[i][1],timeline[i+1][1])
timeline[-1]=(timeline[-1][0],timeline[-1][1],60.0)

for si,(shot,start,end) in enumerate(timeline):
    f0=max(1,int(start*8)+1)
    f1=max(f0+2,int((start+(end-start)*.46)*8))
    f2=max(f1+2,min(480,int(end*8)))
    labels=[str(x).strip()[:42] for x in (shot.get("components") or [])[:4] if str(x).strip()]
    if len(labels)<3:
        raise RuntimeError("Every mechanical CGI shot requires three specifically named components.")
    created=[]
    roots=[]
    # Parts align on a common mechanical axis, like the supplied exploded transmission references.
    count=len(labels)
    for j,label in enumerate(labels):
        final_x=(j-(count-1)/2)*1.72
        final=(final_x,0.0,.25+(j%2)*.12)
        exploded=(final_x*1.34,0.0,final[2]+.82)
        parts=build_named_part(label,exploded)
        root=parent_group(label,parts,exploded)
        root.location=exploded
        root.keyframe_insert("location",frame=f0)
        root.location=final
        root.keyframe_insert("location",frame=f1)
        root.keyframe_insert("location",frame=f2)
        # Gear/shaft assemblies physically rotate; other parts slide into place.
        kind=label.lower()
        if any(t in kind for t in ("gear","shaft","axle","rotor","turbine","converter","pump")):
            root.rotation_euler.x=0
            root.keyframe_insert("rotation_euler",frame=f0)
            root.rotation_euler.x=math.tau
            root.keyframe_insert("rotation_euler",frame=f2)
            for fc in root.animation_data.action.fcurves:
                for kp in fc.keyframe_points:
                    kp.interpolation="LINEAR"
        roots.append(root)
        created.extend(parts)
    # A cutaway casing is present where the shot names a transmission body.
    if any(any(t in x.lower() for t in ("housing","casing","gearbox case","transmission case")) for x in labels):
        pass
    # Narrated hydraulic flow: animated blue oil beads travel through the mechanism.
    narrative=(str(shot.get("voice",""))+" "+str(shot.get("animation",""))).lower()
    if any(t in narrative for t in ("oil","fluid","hydraulic","pressure","flows","circulates")):
        for n in range(5):
            bpy.ops.mesh.primitive_uv_sphere_add(segments=12,ring_count=8,radius=.075,
                location=(-1.5+n*.7,-.20,.75))
            bead=bpy.context.object
            bead.name="Animated hydraulic fluid particle"
            bead.data.materials.append(cyan)
            bead.keyframe_insert("location",frame=f0)
            bead.location.x+=.8
            bead.keyframe_insert("location",frame=f2)
            created.append(bead)
    # Make the shot's geometry visible only during its narration interval.
    for obj in created+[root]:
        obj.hide_render=True
        obj.keyframe_insert("hide_render",frame=max(1,f0-1))
        obj.hide_render=False
        obj.keyframe_insert("hide_render",frame=f0)
        obj.hide_render=False
        obj.keyframe_insert("hide_render",frame=max(f0,f2-1))
        obj.hide_render=True
        obj.keyframe_insert("hide_render",frame=min(480,f2+1))
    # Distinct camera move for each beat, with a gentle focus shift and real parallax.
    camera_rig.location=(0,0,0)
    camera_rig.rotation_euler=(0,0,0)
    camera_rig.keyframe_insert("location",frame=f0)
    camera_rig.keyframe_insert("rotation_euler",frame=f0)
    mode=str(shot.get("camera") or "macro push-in").lower()
    detail=str(shot.get("cameraDetail") or "").lower()
    camera.data.ortho_scale=8.0 if any(t in detail for t in ("macro","close")) else 9.0
    camera.data.keyframe_insert("ortho_scale",frame=f0)
    if "left-to-right" in mode:
        camera_rig.location.x=-.55
    elif "right-to-left" in mode:
        camera_rig.location.x=.55
    elif "orbit clockwise" in mode:
        camera_rig.rotation_euler.z=math.radians(9)
    elif "orbit counterclockwise" in mode:
        camera_rig.rotation_euler.z=math.radians(-9)
    elif "top-down" in mode:
        camera.location=(0,-7,9)
        camera.rotation_euler=(Vector((0,0,0))-Vector(camera.location)).to_track_quat("-Z","Y").to_euler()
    elif "pull-back" in mode:
        camera.data.ortho_scale=10.0
    elif "macro push-in" in mode:
        camera.data.ortho_scale=6.7
    camera_rig.keyframe_insert("location",frame=f2)
    camera_rig.keyframe_insert("rotation_euler",frame=f2)
    camera.data.keyframe_insert("ortho_scale",frame=f2)
    # Minimal technical annotation in the upper negative space.
    bpy.ops.object.text_add(location=(-2.4,0,2.05))
    caption=bpy.context.object
    caption.name="Mechanical shot annotation"
    caption.data.body=str(shot.get("visual") or labels[0])[:52].upper()
    caption.data.size=.105
    caption.data.extrude=.001
    caption.data.materials.append(bright)
    caption.rotation_euler=camera.rotation_euler
    caption.hide_render=True
    caption.keyframe_insert("hide_render",frame=max(1,f0-1))
    caption.hide_render=False
    caption.keyframe_insert("hide_render",frame=f0)
    caption.keyframe_insert("hide_render",frame=max(f0,f2-1))
    caption.hide_render=True
    caption.keyframe_insert("hide_render",frame=min(480,f2+1))
    scene.frame_set(f0)

# Smooth camera motion, linear continuous rotations and eased assembly slides.
for obj in bpy.data.objects:
    if obj.animation_data and obj.animation_data.action:
        for fc in obj.animation_data.action.fcurves:
            for kp in fc.keyframe_points:
                if "rotation_euler" in fc.data_path and obj.name.endswith("animated assembly"):
                    kp.interpolation="LINEAR"
                else:
                    kp.interpolation="BEZIER"
                    kp.handle_left_type="AUTO_CLAMPED"
                    kp.handle_right_type="AUTO_CLAMPED"

_rendered={"count":0}
def report_progress(_scene):
    _rendered["count"]+=1
    if _rendered["count"]%8==0 or _rendered["count"]>=scene.frame_end:
        print("MECHCGI_PROGRESS {} {}".format(_rendered["count"],scene.frame_end),flush=True)
bpy.app.handlers.render_post.append(report_progress)
scene.frame_set(1)
scene.render.filepath=output_path
bpy.ops.render.render(animation=True)
if not os.path.isfile(output_path) or os.path.getsize(output_path)<10000:
    raise RuntimeError("Mechanical CGI renderer did not create a usable MP4.")

# Blender 3.4 may log a delayed-frame disk error but still return from render().
# Never report completion until FFprobe confirms a readable H.264 video stream.
probe = subprocess.run(
    ["ffprobe", "-v", "error", "-select_streams", "v:0",
     "-show_entries", "stream=codec_name,width,height",
     "-of", "default=noprint_wrappers=1", output_path],
    capture_output=True, text=True, timeout=30
)
if probe.returncode != 0 or "codec_name=h264" not in probe.stdout or "width=540" not in probe.stdout or "height=960" not in probe.stdout:
    raise RuntimeError("Mechanical CGI MP4 failed validation; Blender may have run out of disk space. " + (probe.stderr or probe.stdout)[-800:])
print("MECHANICAL_CGI_RENDER_COMPLETE",os.path.getsize(output_path),flush=True)
