# ScenePlayer3D — the 3D twin of the P0 runtime. Same contract, third axis.
#
# Reads the same Scene Document family (dimension: "3d"), plays the same
# timeline tracks with frame-indexed arithmetic, and quits at the last frame.
# Nothing stochastic at render time: one seeded RNG (blinks), no physics, no
# particles. The placeholder biped is primitives on purpose -- P0 judges the
# MOTION and the pipeline; the modelled character is P1's business.

extends Node3D

const EPS := 0.0001

var doc: Dictionary
var assets_root := ""
var fps := 24
var total_frames := 0
var frame_idx := -1
var rng := RandomNumberGenerator.new()

var cam: Camera3D
var sun: DirectionalLight3D
var world_env: WorldEnvironment
var actors := {}

var actions: Array = []
var camera_ops: Array = []
var gaze_events: Array = []
var expr_events: Array = []
var light_events: Array = []
var audio_events: Array = []
var blink_frames := {}

func _ready() -> void:
	var args := _user_args()
	var scene_path: String = args.get("scene", "")
	assets_root = args.get("assets", "")
	var parsed = JSON.parse_string(FileAccess.get_file_as_string(scene_path))
	if parsed == null:
		push_error("scene JSON invalid"); get_tree().quit(2); return
	doc = parsed

	fps = int(doc.get("fps", 24))
	total_frames = int(round(float(doc["duration_s"]) * fps))
	rng.seed = int(doc.get("seed_lock", 0))

	_build_environment()
	_build_characters()
	_build_camera()
	_preprocess_timeline()
	_precompute_blinks()

func _user_args() -> Dictionary:
	var out := {}
	var argv := OS.get_cmdline_user_args()
	var i := 0
	while i < argv.size() - 1:
		if argv[i].begins_with("--"):
			out[argv[i].trim_prefix("--")] = argv[i + 1]
			i += 2
		else:
			i += 1
	return out

# ---------------------------------------------------------------- build ----

func _flat(color_hex: String) -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.albedo_color = Color(color_hex)
	material.roughness = 0.9
	return material

func _mesh(parent: Node3D, mesh: Mesh, at: Vector3, color_hex: String) -> MeshInstance3D:
	var instance := MeshInstance3D.new()
	instance.mesh = mesh
	instance.position = at
	instance.material_override = _flat(color_hex)
	parent.add_child(instance)
	return instance

func _build_environment() -> void:
	var env: Dictionary = doc.get("environment", {})

	world_env = WorldEnvironment.new()
	var environment := Environment.new()
	environment.background_mode = Environment.BG_COLOR
	environment.background_color = Color(env.get("sky", "#a8cdbd"))
	environment.ambient_light_source = Environment.AMBIENT_SOURCE_COLOR
	environment.ambient_light_color = Color(env.get("ambient", "#cfe3d6"))
	environment.ambient_light_energy = 0.8
	environment.fog_enabled = true
	environment.fog_light_color = Color(env.get("sky", "#a8cdbd"))
	environment.fog_density = 0.012
	world_env.environment = environment
	add_child(world_env)

	sun = DirectionalLight3D.new()
	sun.rotation_degrees = Vector3(-42, -35, 0)
	sun.light_color = Color("#fff6e6")
	sun.light_energy = 1.1
	sun.shadow_enabled = true
	add_child(sun)

	# ground: one wide plane
	var ground := PlaneMesh.new()
	ground.size = Vector2(80, 40)
	_mesh(self, ground, Vector3(4, 0, 0), String(env.get("ground", "#4f8266")))

	# soft hills: squashed spheres receding into the fog
	var hill := SphereMesh.new()
	hill.radius = 6.0
	hill.height = 4.0
	for spec in env.get("hills", [
		{"x": -6.0, "z": -9.0, "c": "#5d9276"}, {"x": 5.0, "z": -12.0, "c": "#548a6e"},
		{"x": 13.0, "z": -8.0, "c": "#5d9276"}, {"x": -14.0, "z": -13.0, "c": "#4f8266"},
	]):
		var m := _mesh(self, hill, Vector3(float(spec["x"]), -0.6, float(spec["z"])),
			String(spec["c"]))
		m.scale = Vector3(1.6, 1.0, 1.2)

	# a couple of stylised trees along the path
	for spec in env.get("trees", [
		{"x": 1.5, "z": -3.5}, {"x": 7.5, "z": -4.5}, {"x": 11.0, "z": -2.5},
	]):
		var trunk := CylinderMesh.new()
		trunk.height = 1.6; trunk.top_radius = 0.14; trunk.bottom_radius = 0.2
		var x := float(spec["x"]); var z := float(spec["z"])
		_mesh(self, trunk, Vector3(x, 0.8, z), "#6b503a")
		var crown := SphereMesh.new(); crown.radius = 0.9; crown.height = 1.5
		_mesh(self, crown, Vector3(x, 2.0, z), "#3f7a5c")

	# the rock the story will one day peek behind
	var rock := BoxMesh.new()
	rock.size = Vector3(1.4, 0.8, 1.0)
	var rock_at: Array = env.get("rock", [6.2, 0.4, 0.9])
	_mesh(self, rock, Vector3(rock_at[0], rock_at[1], rock_at[2]), "#6b6156")

func _build_characters() -> void:
	for spec in doc.get("characters", []):
		var biped := Biped3D.new()
		var spawn: Dictionary = spec["spawn"]
		var p: Array = spawn["pos"]
		# 3d spawn: [x, z] on the ground plane
		biped.position = Vector3(float(p[0]), 0.0, float(p[1]) if p.size() > 1 else 0.0)
		add_child(biped)
		actors[spec["as"]] = biped

func _build_camera() -> void:
	cam = Camera3D.new()
	cam.fov = 55
	add_child(cam)
	cam.make_current()

func _preprocess_timeline() -> void:
	for ev in doc.get("timeline", []):
		match String(ev["track"]):
			"action": actions.append(ev)
			"camera": camera_ops.append(ev)
			"gaze": gaze_events.append(ev)
			"expression": expr_events.append(ev)
			"lighting": light_events.append(ev)
			"audio":
				var copy := (ev as Dictionary).duplicate()
				copy["fired"] = false
				audio_events.append(copy)
	actions.sort_custom(func(a, b): return float(a["t"]) < float(b["t"]))
	camera_ops.sort_custom(func(a, b): return float(a["t"]) < float(b["t"]))

func _precompute_blinks() -> void:
	for name in actors.keys():
		var frames: Array[int] = []
		var t := rng.randf_range(1.0, 2.0)
		while t < float(doc["duration_s"]):
			frames.append(int(t * fps))
			t += rng.randf_range(2.5, 4.5)
		blink_frames[name] = frames

# ---------------------------------------------------------------- play ----

func _process(_delta: float) -> void:
	frame_idx += 1
	if frame_idx >= total_frames:
		get_tree().quit(0)
		return
	var t := float(frame_idx) / float(fps)
	for name in actors.keys():
		_drive_actor(String(name), actors[name] as Biped3D, t)
	_drive_camera(t)
	_drive_lighting(t)
	_fire_audio(t)

func _active_action(actor: String, t: float) -> Dictionary:
	var current := {}
	for ev in actions:
		if String(ev.get("actor", "")) != actor:
			continue
		var start := float(ev["t"])
		var until := float(ev.get("until", start + 0.6))
		if t + EPS >= start:
			if t < until:
				current = ev
			elif current.is_empty():
				current = ev
	return current

func _drive_actor(name: String, biped: Biped3D, t: float) -> void:
	var ev := _active_action(name, t)
	var anim := String(ev.get("anim", "idle"))
	var start := float(ev.get("t", 0.0))
	var until := float(ev.get("until", start + 0.6))
	var local := t - start

	match anim:
		"walk":
			var params: Dictionary = ev.get("params", {})
			var to_a: Array = params["to"]
			var to := Vector3(float(to_a[0]), 0.0, float(to_a[1]) if to_a.size() > 1 else 0.0)
			if biped.walk_key != start:
				biped.walk_from = biped.position
				biped.walk_key = start
			var k := clampf(local / maxf(until - start, EPS), 0.0, 1.0)
			biped.position = biped.walk_from.lerp(to, k)
			biped.face_travel(local)
			biped.pose_walk(local, float(params.get("speed", 1.0)))
		"stop_settle":
			biped.face_camera(local)
			biped.pose_settle(local)
		"talk":
			biped.face_camera(1.0)
			biped.pose_talk(local, _viseme_open(ev, t))
		"sad_idle":
			biped.face_camera(1.0)
			biped.pose_sad_idle(local)
		_:
			biped.pose_idle(local)

	for g in gaze_events:
		if String(g.get("actor", "")) != name:
			continue
		var gt := float(g["t"])
		var hold := float(g.get("hold", 0.5))
		if t >= gt and t <= gt + hold + 0.35:
			var dir := -1.0 if String(g.get("dir", "left")) == "left" else 1.0
			var ease_in := clampf((t - gt) / 0.25, 0.0, 1.0)
			var ease_out := clampf((gt + hold + 0.35 - t) / 0.35, 0.0, 1.0)
			biped.gaze(dir * minf(ease_in, ease_out))

	for x in expr_events:
		if String(x.get("actor", "")) == name and t >= float(x["t"]):
			biped.expression = String(x.get("set", "neutral"))

	biped.blink_now = false
	if blink_frames.has(name):
		for bf in blink_frames[name]:
			if frame_idx >= int(bf) and frame_idx < int(bf) + 3:
				biped.blink_now = true
				break

func _viseme_open(ev: Dictionary, t: float) -> float:
	var path := String(ev.get("visemes", ""))
	if path == "":
		return 0.5 + 0.45 * sin(t * 18.0)
	if not ev.has("_vis"):
		var parsed = JSON.parse_string(
			FileAccess.get_file_as_string(assets_root.path_join(path)))
		ev["_vis"] = parsed if parsed != null else {"frames": []}
	var rel_f := int((t - float(ev["t"])) * fps)
	var open := 0.1
	for fr in ev["_vis"].get("frames", []):
		if int(fr["f"]) <= rel_f:
			open = float(fr.get("open", 0.5))
	return open

func _drive_camera(t: float) -> void:
	var hero: Biped3D = null
	for a in actors.values():
		hero = a as Biped3D
		break
	var look_at_point := Vector3(4, 1.0, 0)
	var pos := Vector3(0, 1.3, 4.6)
	var fov := 55.0
	for op in camera_ops:
		var ot := float(op["t"])
		if t + EPS < ot:
			break
		match String(op.get("op", "state")):
			"state":
				var v: Dictionary = op["value"]
				var pa: Array = v.get("pos", [0, 1.3, 4.6])
				pos = Vector3(float(pa[0]), float(pa[1]), float(pa[2]))
				fov = float(v.get("fov", 55.0))
			"follow":
				if hero == null:
					continue
				var params: Dictionary = op.get("params", {})
				var until := float(op.get("until", 1e9))
				var k := 1.0
				if t <= until:
					k = 1.0 - pow(float(params.get("lag", 0.1)),
						(t - ot) * fps / 10.0 + 1.0)
				var target := hero.position + Vector3(0, 1.25, 4.4)
				pos = pos.lerp(Vector3(target.x, pos.y, pos.z), clampf(k, 0.0, 1.0))
			"push":
				if hero == null:
					continue
				var params2: Dictionary = op.get("params", {})
				var over := float(params2.get("over", 2.0))
				var k2 := clampf((t - ot) / maxf(over, EPS), 0.0, 1.0)
				k2 = 0.5 - 0.5 * cos(PI * k2)
				var near := hero.position + Vector3(0.25, 1.15, 2.6)
				pos = pos.lerp(near, k2)
				fov = lerpf(fov, 46.0, k2)
	if hero != null:
		look_at_point = hero.position + Vector3(0, 1.05, 0)
	cam.fov = fov
	cam.position = pos
	cam.look_at(look_at_point, Vector3.UP)

func _drive_lighting(t: float) -> void:
	for ev in light_events:
		var lt := float(ev["t"])
		if t + EPS < lt:
			continue
		var target := Color(ev.get("value", {}).get("ambient", "#ffffff"))
		match String(ev.get("op", "state")):
			"state":
				sun.light_color = target
			"fade":
				var over := float(ev.get("params", {}).get("over", 1.0))
				var k := clampf((t - lt) / maxf(over, EPS), 0.0, 1.0)
				if not ev.has("_from"):
					ev["_from"] = sun.light_color
				var from_c: Color = ev["_from"]
				sun.light_color = from_c.lerp(target, k)
				sun.rotation_degrees.x = lerpf(-42.0, -18.0, k)  # sun lowers
				world_env.environment.ambient_light_color = \
					Color("#cfe3d6").lerp(Color("#e8cdb0"), k)

func _fire_audio(t: float) -> void:
	for ev in audio_events:
		if bool(ev["fired"]) or t + EPS < float(ev["t"]):
			continue
		ev["fired"] = true
		var path: String = assets_root.path_join(String(ev["file"]))
		if not FileAccess.file_exists(path):
			push_warning("audio missing, skipped: " + path)
			continue
		var player := AudioStreamPlayer.new()
		var stream := AudioStreamWAV.new()
		var bytes := FileAccess.get_file_as_bytes(path)
		stream.format = AudioStreamWAV.FORMAT_16_BITS
		stream.mix_rate = bytes.decode_u32(24)
		stream.stereo = bytes.decode_u16(22) == 2
		stream.data = bytes.slice(44)
		player.stream = stream
		player.volume_db = float(ev.get("gain_db", 0.0))
		add_child(player)
		player.play()


# =========================================================================
# Biped3D — primitives with the same motion craft as the 2D twin: bob and
# lag, overshoot settle, eased turns, viseme mouth. The silhouette is a
# capsule creature; the LIFE is in the curves, and that is what P0 judges.
# =========================================================================
class Biped3D:
	extends Node3D

	var expression := "neutral"
	var blink_now := false
	var walk_key := -1.0
	var walk_from := Vector3.ZERO
	var yaw_current := 0.0

	var root: Node3D
	var body: MeshInstance3D
	var head_pivot: Node3D
	var ear_l: Node3D
	var ear_r: Node3D
	var eye_l: MeshInstance3D
	var eye_r: MeshInstance3D
	var pupil_l: MeshInstance3D
	var pupil_r: MeshInstance3D
	var brow_l: MeshInstance3D
	var brow_r: MeshInstance3D
	var mouth: MeshInstance3D
	var arm_l: Node3D
	var arm_r: Node3D
	var leg_l: Node3D
	var leg_r: Node3D
	var tail: MeshInstance3D

	const FUR := "#b9b4ac"
	const BELLY := "#f3ead9"
	const DARK := "#4a3526"

	func _init() -> void:
		root = Node3D.new(); add_child(root)

		leg_l = _limb(Vector3(-0.14, 0.52, 0), 0.5, 0.085, FUR)
		leg_r = _limb(Vector3(0.14, 0.52, 0), 0.5, 0.085, FUR)

		var body_mesh := CapsuleMesh.new()
		body_mesh.radius = 0.3; body_mesh.height = 0.85
		body = _part(root, body_mesh, Vector3(0, 0.78, 0), FUR)
		var belly_mesh := SphereMesh.new()
		belly_mesh.radius = 0.22; belly_mesh.height = 0.5
		var belly := _part(root, belly_mesh, Vector3(0, 0.72, 0.14), BELLY)
		belly.scale = Vector3(0.85, 1.0, 0.6)

		arm_l = _limb(Vector3(-0.32, 0.98, 0), 0.42, 0.06, FUR)
		arm_r = _limb(Vector3(0.32, 0.98, 0), 0.42, 0.06, FUR)

		var tail_mesh := SphereMesh.new()
		tail_mesh.radius = 0.11; tail_mesh.height = 0.22
		tail = _part(root, tail_mesh, Vector3(0, 0.62, -0.3), BELLY)

		head_pivot = Node3D.new()
		head_pivot.position = Vector3(0, 1.28, 0)
		root.add_child(head_pivot)

		var head_mesh := SphereMesh.new()
		head_mesh.radius = 0.3; head_mesh.height = 0.56
		_part(head_pivot, head_mesh, Vector3.ZERO, FUR)
		var muzzle := SphereMesh.new()
		muzzle.radius = 0.14; muzzle.height = 0.22
		_part(head_pivot, muzzle, Vector3(0, -0.06, 0.22), BELLY)

		ear_l = _ear(Vector3(-0.13, 0.24, -0.02))
		ear_r = _ear(Vector3(0.13, 0.24, -0.02))

		eye_l = _ball(head_pivot, 0.055, Vector3(-0.12, 0.05, 0.245), "#ffffff")
		eye_r = _ball(head_pivot, 0.055, Vector3(0.12, 0.05, 0.245), "#ffffff")
		pupil_l = _ball(head_pivot, 0.026, Vector3(-0.115, 0.05, 0.292), DARK)
		pupil_r = _ball(head_pivot, 0.026, Vector3(0.125, 0.05, 0.292), DARK)

		var brow_mesh := BoxMesh.new()
		brow_mesh.size = Vector3(0.11, 0.02, 0.02)
		brow_l = _part(head_pivot, brow_mesh, Vector3(-0.12, 0.15, 0.26), DARK)
		brow_r = _part(head_pivot, brow_mesh, Vector3(0.12, 0.15, 0.26), DARK)

		var mouth_mesh := SphereMesh.new()
		mouth_mesh.radius = 0.055; mouth_mesh.height = 0.1
		mouth = _part(head_pivot, mouth_mesh, Vector3(0, -0.155, 0.315), DARK)
		mouth.scale = Vector3(1.0, 0.15, 0.4)

	func _flat3(color_hex: String) -> StandardMaterial3D:
		var material := StandardMaterial3D.new()
		material.albedo_color = Color(color_hex)
		material.roughness = 0.95
		return material

	func _part(parent: Node3D, mesh: Mesh, at: Vector3, color_hex: String) -> MeshInstance3D:
		var instance := MeshInstance3D.new()
		instance.mesh = mesh
		instance.position = at
		instance.material_override = _flat3(color_hex)
		parent.add_child(instance)
		return instance

	func _ball(parent: Node3D, r: float, at: Vector3, color_hex: String) -> MeshInstance3D:
		var mesh := SphereMesh.new()
		mesh.radius = r; mesh.height = r * 2.0
		return _part(parent, mesh, at, color_hex)

	func _limb(at: Vector3, length: float, r: float, color_hex: String) -> Node3D:
		var pivot := Node3D.new(); pivot.position = at; root.add_child(pivot)
		var mesh := CapsuleMesh.new()
		mesh.radius = r; mesh.height = length
		var shape := _part(pivot, mesh, Vector3(0, -length * 0.5, 0), color_hex)
		shape.rotation_degrees.x = 0
		return pivot

	func _ear(at: Vector3) -> Node3D:
		var pivot := Node3D.new(); pivot.position = at; head_pivot.add_child(pivot)
		var mesh := CapsuleMesh.new()
		mesh.radius = 0.055; mesh.height = 0.5
		var inner := SphereMesh.new(); inner.radius = 0.028; inner.height = 0.3
		_part(pivot, mesh, Vector3(0, 0.22, 0), FUR)
		_part(pivot, inner, Vector3(0, 0.2, 0.03), "#e8cdb0")
		return pivot

	# -- facing ------------------------------------------------------------

	func face_travel(local: float) -> void:
		var k := clampf(local / 0.4, 0.0, 1.0)
		yaw_current = lerpf(yaw_current, deg_to_rad(-62.0), k * 0.3 + 0.1)
		root.rotation.y = yaw_current

	func face_camera(local: float) -> void:
		var k := clampf(local / 0.5, 0.0, 1.0)
		k = 0.5 - 0.5 * cos(PI * k)
		yaw_current = lerpf(yaw_current, 0.0, k)
		root.rotation.y = yaw_current

	# -- poses -------------------------------------------------------------

	func pose_idle(t: float) -> void:
		root.position.y = sin(t * 1.6) * 0.012
		head_pivot.rotation.z = sin(t * 0.9) * 0.02
		_swing(0.0, 0.0)
		_ears(0.08 + sin(t * 1.6) * 0.03, 0.0)
		_apply_face(0.08)

	func pose_walk(t: float, speed: float) -> void:
		var w := t * 7.0 * speed
		root.position.y = absf(sin(w)) * 0.05
		root.rotation.z = sin(w) * 0.02
		_swing(sin(w) * 0.65, -sin(w) * 0.5)
		head_pivot.rotation.x = 0.06 - absf(sin(w)) * 0.04
		_ears(0.12, -sin(w - 0.7) * 0.28)
		tail.position.z = -0.3 + sin(w) * 0.02
		_apply_face(0.08)

	func pose_settle(t: float) -> void:
		var k := exp(-t * 5.0) * sin(t * 14.0)
		root.position.y = k * 0.04
		root.rotation.z = k * 0.03
		_swing(k * 0.35, -k * 0.3)
		_ears(0.1, k * 0.35)
		_apply_face(0.08)

	func pose_talk(t: float, open: float) -> void:
		root.position.y = sin(t * 2.2) * 0.008
		root.rotation.x = 0.03
		head_pivot.rotation.z = sin(t * 3.1) * 0.025
		_swing(0.14, -0.06)
		_ears(0.1 + sin(t * 2.2) * 0.03, 0.0)
		_apply_face(open)

	func pose_sad_idle(t: float) -> void:
		root.position.y = sin(t * 1.2) * 0.008 - 0.015
		head_pivot.rotation.x = 0.16
		_swing(0.12, 0.1)
		_ears(0.65, 0.0)
		expression = "sad"
		_apply_face(0.04)

	func gaze(dir: float) -> void:
		head_pivot.rotation.y = dir * 0.5
		pupil_l.position.x = -0.115 + dir * 0.016
		pupil_r.position.x = 0.125 + dir * 0.016

	func _swing(arms: float, legs: float) -> void:
		arm_l.rotation.x = arms
		arm_r.rotation.x = -arms
		leg_l.rotation.x = legs
		leg_r.rotation.x = -legs

	func _ears(droop: float, lag: float) -> void:
		ear_l.rotation.x = -droop + lag
		ear_r.rotation.x = -droop - lag * 0.7
		ear_l.rotation.z = 0.12
		ear_r.rotation.z = -0.12

	func _apply_face(mouth_open: float) -> void:
		mouth.scale = Vector3(1.0 - mouth_open * 0.3, 0.15 + mouth_open * 1.3,
			0.4 + mouth_open * 0.3)
		var blink_scale := 0.1 if blink_now else 1.0
		eye_l.scale.y = blink_scale
		eye_r.scale.y = blink_scale
		match expression:
			"worried":
				brow_l.rotation.z = 0.4; brow_r.rotation.z = -0.4
				brow_l.position.y = 0.13; brow_r.position.y = 0.13
			"sad":
				brow_l.rotation.z = 0.55; brow_r.rotation.z = -0.55
				brow_l.position.y = 0.12; brow_r.position.y = 0.12
			_:
				brow_l.rotation.z = 0.0; brow_r.rotation.z = 0.0
				brow_l.position.y = 0.15; brow_r.position.y = 0.15
