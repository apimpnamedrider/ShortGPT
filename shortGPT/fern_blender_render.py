import bpy, sys, json, math, os, re
from mathutils import Vector

argv = sys.argv[sys.argv.index("--")+1:] if "--" in sys.argv else []
storyboard_path, output_path = argv[0], argv[1]
with open(storyboard_path, "r", encoding="utf-8") as f:
    data=json.load(f)
scenes=data.get("scenes",[])
if not 3 <= len(scenes) <= 10:
    raise RuntimeError("Fern renderer requires 3 to 10 storyboard scenes")

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete(use_global=False)
scene=bpy.context.scene
scene.render.engine="BLENDER_WORKBENCH"
scene.render.resolution_x=540
scene.render.resolution_y=960
scene.render.resolution_percentage=100
scene.render.fps=12
scene.render.fps_base=1
scene.frame_start=1
scene.frame_end=720
scene.render.image_settings.file_format="FFMPEG"
scene.render.ffmpeg.format="MPEG4"
scene.render.ffmpeg.codec="H264"
scene.render.ffmpeg.constant_rate_factor="HIGH"
scene.render.ffmpeg.ffmpeg_preset="GOOD"
scene.render.filepath=output_path
scene.display.shading.light="STUDIO"
scene.display.shading.studio_light="paint.sl"
scene.display.shading.color_type="MATERIAL"
scene.display.shading.show_shadows=True
scene.display.shading.show_cavity=True
scene.display.shading.cavity_type="BOTH"
scene.display.shading.curvature_ridge_factor=1.35
scene.display.shading.curvature_valley_factor=1.1
scene.display.shading.cavity_ridge_factor=1.2
scene.display.shading.cavity_valley_factor=1.0
scene.render.image_settings.color_mode="RGB"
scene.view_settings.view_transform="Standard"
scene.view_settings.look="Medium High Contrast"
scene.view_settings.exposure=0
scene.view_settings.gamma=1

def mat(name, color, metallic=0.0, roughness=0.45):
    m=bpy.data.materials.new(name)
    m.diffuse_color=(*color,1)
    m.metallic=metallic
    m.roughness=roughness
    return m
ivory=mat("Matte porcelain resin",(0.82,0.85,0.88),0.08,0.38)
silver=mat("Brushed anodized aluminium",(0.42,0.49,0.55),0.72,0.28)
dark=mat("Graphite polymer",(0.075,0.09,0.115),0.18,0.42)
glass=mat("Frosted translucent housing",(0.56,0.67,0.72),0.1,0.22)
floor_mat=mat("Void floor",(0.025,0.032,0.046),0.0,0.9)
accents=[
    mat("Amber energy",(1.0,0.55,0.035),0.15,0.24),
    mat("Electric cyan",(0.0,0.82,0.92),0.12,0.22),
    mat("Signal crimson",(0.88,0.075,0.09),0.1,0.26)
]
def bevel(obj, width=0.08):
    mod=obj.modifiers.new("Machined edge bevel","BEVEL")
    mod.width=width
    mod.segments=3
    obj.modifiers.new("Weighted corner normals","WEIGHTED_NORMAL")
    return obj
def cube(name, loc, scale, material, bevel_width=0.08):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    o=bpy.context.object
    o.name=name
    o.dimensions=scale
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    o.data.materials.append(material)
    return bevel(o,bevel_width)
def cyl(name, loc, radius, depth, material, vertices=48):
    bpy.ops.mesh.primitive_cylinder_add(vertices=vertices, radius=radius, depth=depth, location=loc)
    o=bpy.context.object
    o.name=name
    o.data.materials.append(material)
    bevel(o,0.035)
    return o
def torus(name, loc, major, minor, material):
    bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor, major_segments=64, minor_segments=12, location=loc)
    o=bpy.context.object
    o.name=name
    o.data.materials.append(material)
    return o
def text_obj(body, loc, size, material, rotation):
    bpy.ops.object.text_add(location=loc)
    o=bpy.context.object
    o.name="Scene annotation"
    o.data.body=body[:48].upper()
    o.data.size=size
    o.data.extrude=0.001
    o.rotation_euler=rotation
    o.data.materials.append(material)
    return o

# Matte void and soft studio illumination.
cube("Floating studio plinth",(0,0,-0.48),(7.8,6.2,0.22),dark,0.12)
cube("Infinite charcoal floor",(0,0,-0.68),(200,200,0.12),floor_mat,0.02)
world=bpy.data.worlds.new("Deep charcoal navy void") if not bpy.data.worlds else bpy.data.worlds[0]
scene.world=world
world.use_nodes=True
world.node_tree.nodes["Background"].inputs[0].default_value=(0.012,0.016,0.025,1)
world.node_tree.nodes["Background"].inputs[1].default_value=0.35
def area(name, loc, power, size, color):
    bpy.ops.object.light_add(type="AREA", location=loc)
    o=bpy.context.object
    o.name=name
    o.data.energy=power
    o.data.shape="DISK"
    o.data.size=size
    o.data.color=color
    o.rotation_euler=(Vector((0,0,0))-o.location).to_track_quat("-Z","Y").to_euler()
area("Large overhead softbox",(1,-3,8),1150,7,(0.82,0.88,1.0))
area("Cool rim light",(-5,1,4),700,5,(0.46,0.62,0.82))
area("Warm fill",(4,4,5),450,4,(1.0,0.86,0.7))

# Motion-control camera rig: each shot uses one deliberate movement axis.
bpy.ops.object.empty_add(type="PLAIN_AXES", location=(0,0,0))
rig=bpy.context.object
rig.name="Motion control camera rig"
bpy.ops.object.camera_add(location=(0,-10,7))
cam=bpy.context.object
cam.name="Isometric cinema camera"
cam.parent=rig
cam.location=(0,-10,7)
cam.rotation_euler=(Vector((0,0,0))-Vector(cam.location)).to_track_quat("-Z","Y").to_euler()
cam.data.type="ORTHO"
cam.data.ortho_scale=11.5
scene.camera=cam
# Fixed 35-degree-ish isometric viewing angle; camera rig animates per scene.

def parse_time(value, fallback_start, fallback_end):
    nums=re.findall(r"\d+(?:\.\d+)?",str(value or ""))
    if len(nums)>=2: return float(nums[0]),float(nums[1])
    if len(nums)==1: return fallback_start,float(nums[0])
    return fallback_start,fallback_end

cursor=0.0
groups=[]
for i,s in enumerate(scenes):
    start,end=parse_time(s.get("time"),cursor,60*(i+1)/len(scenes))
    start=max(cursor,start)
    end=max(start+2,min(60,end))
    groups.append((s,start,end))
    cursor=end
groups[0]=(groups[0][0],0.0,groups[0][2])
for i in range(len(groups)-1):
    groups[i]=(groups[i][0],groups[i][1],groups[i+1][1])
groups[-1]=(groups[-1][0],groups[-1][1],60.0)

for si,(s,start,end) in enumerate(groups):
    accent=accents[si%3]
    components=s.get("components") or []
    if len(components)<3:
        components=(components+["Drive assembly","Transfer chamber","Output coupling"])[:3]
    components=[str(x)[:34] for x in components[:4]]
    f0=max(1,int(start*12)+1)
    f1=max(f0+1,int((start+(end-start)*0.42)*12))
    f2=max(f1+1,int(end*12))
    created=[]
    # Topic-specific miniature assembly with multiple distinct machined forms.
    core=cyl("Central mechanism housing",(0,0,0.35),0.88,0.72,dark)
    core.keyframe_insert("location",frame=f0)
    core.keyframe_insert("location",frame=f2)
    created.append(core)
    ring=torus("Precision retaining ring",(0,0,0.78),0.9,0.075,silver)
    ring.keyframe_insert("rotation_euler",frame=f0)
    ring.rotation_euler.z=math.radians(120)
    ring.keyframe_insert("rotation_euler",frame=f2)
    created.append(ring)
    shaft=cyl("Central drive shaft",(0,0,1.05),0.22,1.35,silver,32)
    shaft.keyframe_insert("rotation_euler",frame=f0)
    shaft.rotation_euler.z=math.radians(540)
    shaft.keyframe_insert("rotation_euler",frame=f2)
    created.append(shaft)
    for j,label in enumerate(components):
        angle=(j/max(1,len(components)))*math.tau
        final=(math.cos(angle)*1.35,math.sin(angle)*1.35,0.28+(j%2)*0.28)
        exploded=(final[0]*1.85,final[1]*1.85,final[2]+1.1+(j%2)*0.45)
        kind=(label.lower())
        if any(k in kind for k in ("gear","rotor","wheel","fan")):
            obj=cyl("Component · "+label,exploded,0.48,0.22,ivory,48)
            tooth=torus("Machined gear rim",exploded,0.48,0.07,silver)
        elif any(k in kind for k in ("pipe","valve","tube","shaft","cable")):
            obj=cyl("Component · "+label,exploded,0.22,1.05,ivory,32)
            obj.rotation_euler.y=math.radians(90)
        else:
            obj=cube("Component · "+label,exploded,(0.92,0.72,0.34),ivory,0.09)
        obj.keyframe_insert("location",frame=f0)
        obj.location=final
        obj.keyframe_insert("location",frame=f1)
        obj.rotation_euler.z=angle+math.radians(20)
        obj.keyframe_insert("rotation_euler",frame=f0)
        obj.rotation_euler.z=angle
        obj.keyframe_insert("rotation_euler",frame=f1)
        created.append(obj)
        # Accent pin on the component that transfers energy.
        pin=cyl("Single accent indicator",(final[0],final[1],final[2]+0.24),0.10,0.08,accent,24)
        pin.keyframe_insert("location",frame=f0)
        pin.location=(0,0,1.35)
        pin.keyframe_insert("location",frame=f1)
        created.append(pin)
    # Glowing energy bead traverses the mechanism during the explanatory beat.
    bead_loc=(-2.1,0,1.2)
    bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=12, radius=0.14, location=bead_loc)
    bead=bpy.context.object
    bead.name="Energy transfer particle"
    bead.data.materials.append(accent)
    for frame,loc in [(f0,(-2.1,0,1.2)),((f0+f2)//2,(0,0,1.55)),(f2,(2.1,0,1.2))]:
        bead.location=loc
        bead.keyframe_insert("location",frame=frame)
    created.append(bead)
    # Scene title is kept small and away from the Shorts UI safe-zone.
    label=text_obj(str(s.get("visual") or components[0]),(-2.7,0,2.6),0.13,ivory,cam.rotation_euler)
    created.append(label)
    # Scene objects are visible only during their own beat.
    for obj in created:
        obj.hide_render=True
        obj.keyframe_insert("hide_render",frame=max(1,f0-1))
        obj.hide_render=False
        obj.keyframe_insert("hide_render",frame=f0)
        obj.hide_render=False
        obj.keyframe_insert("hide_render",frame=f2-1)
        obj.hide_render=True
        obj.keyframe_insert("hide_render",frame=min(720,f2+1))
    # Animate a single camera axis per scene.
    mode=str(s.get("camera") or "slow push-in").lower()
    rig.rotation_euler=(0,0,0)
    rig.keyframe_insert("rotation_euler",frame=f0)
    if "orbit" in mode or "rotate" in mode:
        rig.rotation_euler.z=math.radians(9)
        rig.keyframe_insert("rotation_euler",frame=f2)
    else:
        rig.rotation_euler.z=0
        rig.keyframe_insert("rotation_euler",frame=f2)
        if "push" in mode or "zoom" in mode:
            cam.data.ortho_scale=11.5
            cam.data.keyframe_insert("ortho_scale",frame=f0)
            cam.data.ortho_scale=10.3
            cam.data.keyframe_insert("ortho_scale",frame=f2)
    scene.frame_set(f0)

# Smooth interpolation; preserve single-axis camera choreography.
for obj in bpy.data.objects:
    if obj.animation_data and obj.animation_data.action:
        for fc in obj.animation_data.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation="BEZIER"
                kp.handle_left_type="AUTO_CLAMPED"
                kp.handle_right_type="AUTO_CLAMPED"
scene.frame_set(1)
scene.render.filepath=output_path
bpy.ops.render.render(animation=True)
if not os.path.isfile(output_path) or os.path.getsize(output_path)<10000:
    raise RuntimeError("Blender did not produce a usable MP4")
print("FERN_3D_RENDER_COMPLETE",os.path.getsize(output_path))
