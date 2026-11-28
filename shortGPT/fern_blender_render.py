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
scene.render.engine="CYCLES"
scene.cycles.device="CPU"
scene.cycles.samples=2
# Headless Blender package has no available OpenImageDenoiser device.
scene.cycles.use_denoising=False
scene.render.threads_mode="FIXED"
scene.render.threads=1
scene.render.resolution_x=720
scene.render.resolution_y=1280
scene.render.resolution_percentage=100
scene.render.fps=8
scene.render.fps_base=1
scene.frame_start=1
scene.frame_end=480
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
    m.use_nodes=True
    shader=m.node_tree.nodes.get("Principled BSDF")
    if shader:
        shader.inputs["Base Color"].default_value=(*color,1)
        shader.inputs["Metallic"].default_value=metallic
        shader.inputs["Roughness"].default_value=roughness
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

def build_component(label, center, material, accent):
    """Build a recognisable physical form from the actual named storyboard part."""
    name = str(label)[:34]
    kind = name.lower()
    x,y,z = center
    made = []
    if any(k in kind for k in ("solar panel","roof array","photovoltaic","panel","silicon cell","silicon wafer","glass cover","tempered glass","cell array")):
        base = cube(name, center, (1.55,1.05,0.12), dark, 0.035); made.append(base)
        for row in range(3):
            for col in range(4):
                px=x-0.56+col*0.37; py=y-0.36+row*0.36
                cell=cube("Photovoltaic cell",(px,py,z+0.085),(0.31,0.29,0.035),accents[1],0.012)
                made.append(cell)
        made.append(cube("Panel aluminium frame",(x,y,z+0.12),(1.66,1.14,0.045),silver,0.025))
    elif any(k in kind for k in ("gear","wheel","rotor","fan","turbine","flywheel")):
        body=cyl(name,center,0.58,0.20,material,64); made.append(body)
        made.append(torus("Machined gear rim",(x,y,z+0.12),0.48,0.065,silver))
        for n in range(12):
            a=math.tau*n/12
            tooth=cube("Gear tooth",(x+math.cos(a)*0.57,y+math.sin(a)*0.57,z),(0.17,0.15,0.22),material,0.025)
            tooth.rotation_euler.z=a; made.append(tooth)
        made.append(cyl("Gear hub",center,0.19,0.27,accent,32))
    elif any(k in kind for k in ("house","home","room","building","appliance","fridge","refrigerator")):
        made.append(cube(name,(x,y,z+0.35),(1.15,0.9,0.75),material,0.04))
        made.append(cube("Roof plane",(x,y,z+0.82),(1.35,1.02,0.16),accents[0],0.035))
        made.append(cube("Front window",(x-0.18,y-0.46,z+0.43),(0.32,0.035,0.27),glass,0.01))
        made.append(cube("Door",(x+0.32,y-0.47,z+0.27),(0.22,0.04,0.52),dark,0.015))
    elif any(k in kind for k in ("inverter","battery","junction box","breaker","controller","power supply","transformer","motor housing","casing","housing")):
        made.append(cube(name,center,(0.95,0.72,0.85),dark,0.10))
        made.append(cube("Status display",(x,y-0.37,z+0.12),(0.48,0.025,0.22),accents[1],0.018))
        for n in range(4):
            made.append(cube("Cooling vent",(x-0.29+n*0.19,y-0.39,z-0.22),(0.11,0.025,0.035),silver,0.008))
    elif any(k in kind for k in ("pipe","valve","tube","shaft","cable","wire","conduit","hose","busbar","rail","axle")):
        obj=cyl(name,center,0.13 if "cable" in kind or "wire" in kind else 0.22,1.15,material,32)
        obj.rotation_euler.y=math.radians(90); made.append(obj)
        if "valve" in kind:
            made.append(torus("Valve handwheel",(x,y,z+0.25),0.32,0.055,accent))
    elif any(k in kind for k in ("silicon wafer","silicon cell","semiconductor","photovoltaic cell","solar cell","microchip","processor","circuit board","pcb")):
        board=cube(name,center,(1.15,0.82,0.10),dark,0.025); made.append(board)
        for row in range(3):
            for col in range(4):
                chip=cube("Semiconductor element",(x-0.39+col*0.26,y-0.25+row*0.24,z+0.075),(0.17,0.14,0.035),accent,0.008)
                made.append(chip)
        for n in range(5):
            made.append(cube("Copper trace",(x-0.48+n*0.24,y+0.39,z+0.065),(0.035,0.12,0.025),silver,0.004))
    elif any(k in kind for k in ("battery","cell pack","lithium cell","accumulator")):
        made.append(cube(name,center,(1.05,0.78,0.62),dark,0.07))
        for n in range(4):
            made.append(cyl("Battery cell",(x-0.34+n*0.22,y,z+0.34),0.095,0.55,accent,24))
        made.append(cube("Positive terminal",(x-0.34,y,z+0.67),(0.12,0.12,0.08),silver,0.01))
        made.append(cube("Negative terminal",(x+0.34,y,z+0.67),(0.12,0.12,0.08),silver,0.01))
    elif any(k in kind for k in ("light bulb","lamp","led","light","bulb")):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=20, radius=0.34, location=(x,y,z+0.30))
        bulb=bpy.context.object; bulb.name=name; bulb.scale=(0.78,0.78,1.0); bulb.data.materials.append(accent); made.append(bulb)
        made.append(cyl("Lamp socket",(x,y,z-0.08),0.19,0.22,silver,32))
        made.append(torus("Socket thread",(x,y,z-0.12),0.18,0.025,dark))
    elif any(k in kind for k in ("fan","propeller","impeller")):
        made.append(cyl(name,center,0.22,0.22,silver,32))
        for n in range(5):
            blade=cube("Fan blade",(x+math.cos(math.tau*n/5)*0.38,y+math.sin(math.tau*n/5)*0.38,z),(0.62,0.14,0.055),accent,0.025)
            blade.rotation_euler.z=math.tau*n/5; made.append(blade)
    elif any(k in kind for k in ("screen","monitor","display","television")):
        made.append(cube(name,center,(1.25,0.10,0.78),dark,0.055))
        made.append(cube("Illuminated display",(x,y-0.058,z),(1.12,0.018,0.64),accents[1],0.015))
        made.append(cyl("Display stand",(x,y,z-0.52),0.10,0.28,silver,24))
    elif any(k in kind for k in ("switch","breaker","fuse","relay","toggle")):
        made.append(cube(name,center,(0.72,0.58,0.48),dark,0.055))
        lever=cube("Switch lever",(x,y-0.34,z+0.24),(0.12,0.10,0.42),accent,0.025)
        lever.rotation_euler.y=math.radians(-24); made.append(lever)
        made.append(cube("Contact terminals",(x,y+0.34,z),(0.48,0.10,0.12),silver,0.018))
    elif any(k in kind for k in ("bearing","seal","ring","race")):
        made.append(cyl(name,center,0.48,0.18,dark,64))
        made.append(torus("Bearing race",(x,y,z+0.12),0.34,0.09,silver))
        for n in range(8):
            a=math.tau*n/8
            bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=0.075, location=(x+math.cos(a)*0.34,y+math.sin(a)*0.34,z+0.13))
            ball=bpy.context.object; ball.name="Bearing ball"; ball.data.materials.append(accent); made.append(ball)
    elif any(k in kind for k in ("water","droplet","fluid","reservoir","tank","pump")):
        if "droplet" in kind or "water" in kind:
            bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=20, radius=0.42, location=center)
            obj=bpy.context.object; obj.name=name; obj.scale=(0.72,0.72,1.15); obj.data.materials.append(glass); made.append(obj)
        else:
            made.append(cyl(name,center,0.48,0.85,material,48))
            made.append(torus("Tank rim",(x,y,z+0.43),0.42,0.045,silver))
    elif any(k in kind for k in ("heart","lung","brain","organ","cell","blood")):
        bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=24, radius=0.48, location=center)
        obj=bpy.context.object; obj.name=name; obj.scale=(0.82,0.72,1.12); obj.data.materials.append(accents[2]); made.append(obj)
        if "lung" in kind:
            for dx in (-0.24,0.24):
                bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=16, radius=0.29, location=(x+dx,y,z))
                lob=bpy.context.object; lob.name="Lung lobe"; lob.data.materials.append(material); made.append(lob)
    else:
        # Distinct physical fallback: a labelled machined part, not a repeated mechanism.
        made.append(cube(name,center,(0.82,0.68,0.42),material,0.07))
        made.append(cyl("Machined connector",(x,y,z+0.28),0.18,0.18,silver,32))
    return made

for si,(s,start,end) in enumerate(groups):
    accent=accents[si%3]
    components=[str(x)[:34] for x in (s.get("components") or [])[:4]]
    if len(components)<3:
        raise RuntimeError("Each scene must name at least three physical components.")
    f0=max(1,int(start*8)+1)
    f1=max(f0+1,int((start+(end-start)*0.48)*8))
    f2=max(f1+1,int(end*8))
    created=[]
    # Each beat is built from its own narrated parts; no generic central mechanism is reused.
    count=len(components)
    for j,label in enumerate(components):
        # Give every beat a different blocking pattern so the film does not
        # repeat the same circular product arrangement in every scene.
        spread = j-(count-1)/2
        if si % 4 == 0:
            final=(spread*1.65,0.0,0.22+(j%2)*0.16)                 # horizontal reveal
        elif si % 4 == 1:
            final=(0.0,spread*1.30,0.22+(j%2)*0.16)                 # vertical exploded stack
        elif si % 4 == 2:
            final=(spread*1.35,spread*0.72,0.22+(j%2)*0.16)         # diagonal process chain
        else:
            final=((j%2)*1.65-0.82,(j//2)*1.45-0.72,0.22+(j%2)*0.16) # modular cutaway grid
        exploded=(final[0]*1.30,final[1]*1.30,final[2]+1.05)
        parts=build_component(label,exploded,ivory,accent)
        for part in parts:
            part.keyframe_insert("location",frame=f0)
            delta=Vector(final)-Vector(exploded)
            part.location += delta
            part.keyframe_insert("location",frame=f1)
            part.keyframe_insert("location",frame=f2)
            part.rotation_euler.z += math.radians(12 if j%2 else -12)
            part.keyframe_insert("rotation_euler",frame=f0)
            part.rotation_euler.z -= math.radians(12 if j%2 else -12)
            part.keyframe_insert("rotation_euler",frame=f1)
            created.append(part)
    # Show a moving signal only when the narration actually describes transfer or flow.
    story=(str(s.get("visual",""))+" "+str(s.get("voice",""))).lower()
    if any(k in story for k in ("flow","moves","transfer","travels","current","energy","power","water","light","signal","rotates")):
        for n in range(5):
            x=-1.7+n*0.85
            bpy.ops.mesh.primitive_uv_sphere_add(segments=20, ring_count=12, radius=0.105, location=(x,-0.15,1.35))
            bead=bpy.context.object; bead.name="Narrated transfer pulse"; bead.data.materials.append(accent)
            bead.keyframe_insert("location",frame=f0)
            bead.location=(x+0.45,-0.15,1.35)
            bead.keyframe_insert("location",frame=f1)
            created.append(bead)
    shot_caption = str(s.get("visual") or components[0]).split(";")[0][:48]
    label=text_obj(shot_caption,(-2.5,0,2.65),0.13,ivory,cam.rotation_euler)
    created.append(label)
    # Hide each beat outside its exact timeline interval.
    for obj in created:
        obj.hide_render=True
        obj.keyframe_insert("hide_render",frame=max(1,f0-1))
        obj.hide_render=False
        obj.keyframe_insert("hide_render",frame=f0)
        obj.hide_render=False
        obj.keyframe_insert("hide_render",frame=max(f0,f2-1))
        obj.hide_render=True
        obj.keyframe_insert("hide_render",frame=min(720,f2+1))
    # Reset and keyframe a distinct, purposeful camera path for every narration beat.
    mode=str(s.get("camera") or "macro push-in").lower()
    rig.rotation_euler=(0,0,0)
    rig.location=(0,0,0)
    rig.keyframe_insert("rotation_euler",frame=f0)
    rig.keyframe_insert("location",frame=f0)
    cam.location=(0,-10,7)
    cam.rotation_euler=(Vector((0,0,0))-Vector(cam.location)).to_track_quat("-Z","Y").to_euler()
    cam.keyframe_insert("location",frame=f0)
    cam.keyframe_insert("rotation_euler",frame=f0)
    cam.data.ortho_scale=12.0
    cam.data.keyframe_insert("ortho_scale",frame=f0)
    if "left-to-right" in mode:
        cam.location.x=-1.8
        cam.keyframe_insert("location",frame=f2)
    elif "right-to-left" in mode:
        cam.location.x=1.8
        cam.keyframe_insert("location",frame=f2)
    elif "top-down" in mode:
        cam.location=(0,-7,10)
        cam.rotation_euler=(Vector((0,0,0))-Vector(cam.location)).to_track_quat("-Z","Y").to_euler()
        cam.keyframe_insert("location",frame=f2)
        cam.keyframe_insert("rotation_euler",frame=f2)
    elif "orbit clockwise" in mode or "orbit counterclockwise" in mode:
        direction=1 if "clockwise" in mode else -1
        rig.rotation_euler.z=math.radians(14*direction)
        rig.keyframe_insert("rotation_euler",frame=f2)
    elif "pull-back" in mode:
        cam.data.ortho_scale=14.0
        cam.data.keyframe_insert("ortho_scale",frame=f2)
    else:
        cam.data.ortho_scale=10.0
        cam.data.keyframe_insert("ortho_scale",frame=f2)
    scene.frame_set(f0)

# Report actual rendered-frame progress to the worker's job tracker.
_rendered = {"count": 0}
def _report_render_progress(_scene):
    _rendered["count"] += 1
    if _rendered["count"] % 8 == 0 or _rendered["count"] >= scene.frame_end:
        print(f"FERN_PROGRESS {_rendered['count']} {scene.frame_end}", flush=True)
bpy.app.handlers.render_post.append(_report_render_progress)

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
print("FERN_3D_RENDER_COMPLETE",os.path.getsize(output_path), flush=True)
