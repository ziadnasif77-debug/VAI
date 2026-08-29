# ScenePlayer — the whole P0 runtime.
#
# Contract (approved architecture v2): this script is a RENDERER. It reads one
# Scene Document (JSON), builds the scene graph, plays the timeline with
# frame-indexed arithmetic, and quits at the last frame. It decides nothing a
# story engine should decide, and it never generates anything stochastic:
# the one RNG (blinks) is seeded from the document, so the same JSON is the
# same performance, frame for frame.
#
# Run:
#   godot --headless --path animation/godot \
#         --write-movie <out>/frames.png --fixed-fps 24 \
#         -- --scene <abs path to scene.json> --assets <abs assets root>
#
# Determinism rules honoured throughout:
#   * every motion value is a pure function of the frame index (never
#     accumulated deltas, never wall time);
#   * the blink RNG is seeded from seed_lock;
#   * particles and physics are absent by design in P0.

extends Node2D

const EPS := 0.0001

var doc: Dictionary
var assets_root := ""
var fps := 24
var total_frames := 0
var frame_idx := -1
var rng := RandomNumberGenerator.new()

var cam: Camera2D
var layers := {}            # id -> {node, depth, base_pos}
var actors := {}            # as -> PlaceholderBiped
var modulate_node: CanvasModulate

# preprocessed tracks
var actions := []           # {t, until, actor, anim, params, visemes}
var camera_ops := []        # sorted by t
var gaze_events := []
var expr_events := []
var light_events := []
var audio_events := []      # {t, file, gain_db, fired}
var blink_frames := {}      # actor -> Array[int] (precomputed, seeded)

func _ready() -> void:
	var args := _user_args()
	var scene_path: String = args.get("scene", "")
	assets_root = args.get("assets", "")
	if scene_path == "":
		push_error("--scene is required"); get_tree().quit(2); return
	var text := FileAccess.get_file_as_string(scene_path)
	if text == "":
		push_error("scene file unreadable: " + scene_path); get_tree().quit(2); return
	doc = JSON.parse_string(text)
	if doc == null:
		push_error("scene JSON invalid"); get_tree().quit(2); return

	fps = int(doc.get("fps", 24))
	total_frames = int(round(float(doc["duration_s"]) * fps))
	rng.seed = int(doc.get("seed_lock", 0))

	_build_layers()
	_build_characters()
	_build_camera()
	_build_lighting()
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

func _build_layers() -> void:
	var list: Array = doc.get("layers", [])
	for spec in list:
		var node: Node2D
		if spec.has("art"):
			var sprite := Sprite2D.new()
			var img := Image.load_from_file(assets_root.path_join(spec["art"]))
			sprite.texture = ImageTexture.create_from_image(img)
			sprite.centered = false
			node = sprite
		else:
			var rect := ColorRect.new()
			rect.color = Color(spec.get("color", "#ff00ff"))
			var r: Array = spec.get("rect", [0, 0, 1920, 1080])
			rect.position = Vector2(r[0], r[1])
			rect.size = Vector2(r[2], r[3])
			var holder := Node2D.new()
			holder.add_child(rect)
			node = holder
		if spec.has("pos"):
			node.position = Vector2(spec["pos"][0], spec["pos"][1])
		node.z_index = int(spec.get("z", 0))
		add_child(node)
		layers[spec["id"]] = {
			"node": node,
			"depth": float(spec.get("depth", 1.0)),
			"base_pos": node.position,
		}

func _build_characters() -> void:
	for spec in doc.get("characters", []):
		var biped := PlaceholderBiped.new()
		var spawn: Dictionary = spec["spawn"]
		biped.position = Vector2(spawn["pos"][0], spawn["pos"][1])
		biped.scale = Vector2.ONE * float(spawn.get("scale", 1.0))
		biped.facing = 1 if String(spawn.get("facing", "right")) == "right" else -1
		biped.z_index = int(spec.get("z", 5))
		add_child(biped)
		actors[spec["as"]] = biped

func _build_camera() -> void:
	cam = Camera2D.new()
	cam.anchor_mode = Camera2D.ANCHOR_MODE_FIXED_TOP_LEFT
	# Anchored top-left and positioned by half-viewport math below, so the
	# parallax arithmetic has one origin of truth.
	cam.make_current()
	add_child(cam)

func _build_lighting() -> void:
	modulate_node = CanvasModulate.new()
	modulate_node.color = Color(doc.get("lighting", {}).get("ambient", "#ffffff"))
	add_child(modulate_node)

func _preprocess_timeline() -> void:
	for ev in doc.get("timeline", []):
		match String(ev["track"]):
			"action":
				actions.append(ev)
			"camera":
				camera_ops.append(ev)
			"gaze":
				gaze_events.append(ev)
			"expression":
				expr_events.append(ev)
			"lighting":
				light_events.append(ev)
			"audio":
				var copy := (ev as Dictionary).duplicate()
				copy["fired"] = false
				audio_events.append(copy)
			"blink":
				pass  # handled by _precompute_blinks
	actions.sort_custom(func(a, b): return float(a["t"]) < float(b["t"]))
	camera_ops.sort_custom(func(a, b): return float(a["t"]) < float(b["t"]))

func _precompute_blinks() -> void:
	# Natural blinking, decided once from the seeded RNG: intervals of
	# 2.5-4.5s, each blink 3 frames. Precomputing keeps playback order
	# irrelevant — determinism by construction.
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
		_drive_actor(name, actors[name], t)
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
		if t + EPS >= start and (t < until or current.is_empty()):
			if t < until:
				current = ev
			elif current.is_empty() and t >= until:
				current = ev  # hold the last finished action's final pose
	return current

func _drive_actor(name: String, biped: PlaceholderBiped, t: float) -> void:
	var ev := _active_action(name, t)
	var anim := String(ev.get("anim", "idle"))
	var start := float(ev.get("t", 0.0))
	var until := float(ev.get("until", start + 0.6))
	var local := t - start

	match anim:
		"walk":
			var params: Dictionary = ev.get("params", {})
			var to := Vector2(params["to"][0], params["to"][1])
			var from: Vector2 = biped.walk_from if biped.walk_key == start else biped.position
			if biped.walk_key != start:
				biped.walk_from = biped.position
				biped.walk_key = start
				from = biped.walk_from
			var dur := until - start
			var k := clampf(local / maxf(dur, EPS), 0.0, 1.0)
			biped.position = from.lerp(to, k)
			biped.pose_walk(local, float(params.get("speed", 1.0)))
		"stop_settle":
			biped.pose_settle(local)
		"talk":
			biped.pose_talk(local, _viseme_open(ev, t))
		"sad_idle":
			biped.pose_sad_idle(local)
		_:
			biped.pose_idle(local)

	# gaze rides on top of the pose
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

	if blink_frames.has(name):
		for bf in blink_frames[name]:
			if frame_idx >= bf and frame_idx < bf + 3:
				biped.blink_now = true
				break
			biped.blink_now = false

func _viseme_open(ev: Dictionary, t: float) -> float:
	var path := String(ev.get("visemes", ""))
	if path == "":
		# fallback: a readable deterministic chatter
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
	var pos := Vector2(640, 620)
	var zoom := 1.0
	for op in camera_ops:
		var ot := float(op["t"])
		if t + EPS < ot:
			break
		match String(op.get("op", "state")):
			"state":
				pos = Vector2(op["value"]["pos"][0], op["value"]["pos"][1])
				zoom = float(op["value"].get("zoom", 1.0))
			"follow":
				var params: Dictionary = op.get("params", {})
				var until := float(op.get("until", 1e9))
				var target := actors.get(String(params.get("target", "")), null)
				if target != null:
					var off := Vector2(0, 0)
					if params.has("offset"):
						off = Vector2(params["offset"][0], params["offset"][1])
					var followed: Vector2 = target.position + off
					if t <= until:
						# deterministic lag: exponential approach computed
						# from elapsed frames, not accumulated state
						var k := 1.0 - pow(float(params.get("lag", 0.1)),
							(t - ot) * fps / 10.0 + 1.0)
						pos = pos.lerp(followed, clampf(k, 0.0, 1.0))
					else:
						pos = pos.lerp(followed, 1.0)
			"push":
				var params2: Dictionary = op.get("params", {})
				var over := float(params2.get("over", 2.0))
				var k2 := clampf((t - ot) / maxf(over, EPS), 0.0, 1.0)
				k2 = 0.5 - 0.5 * cos(PI * k2)  # inout_sine
				zoom = lerpf(zoom, float(params2.get("zoom_to", 1.1)), k2)

	var viewport := Vector2(1920, 1080)
	var half := viewport * 0.5 / zoom
	cam.zoom = Vector2(zoom, zoom)
	cam.position = pos - half

	# parallax: each layer slides against the camera by (1 - depth)
	for id in layers.keys():
		var L: Dictionary = layers[id]
		var shift := (pos - Vector2(640, 620)) * (1.0 - L["depth"])
		L["node"].position = L["base_pos"] + shift

func _drive_lighting(t: float) -> void:
	for ev in light_events:
		var lt := float(ev["t"])
		if t + EPS < lt:
			continue
		var target := Color(ev.get("value", {}).get("ambient", "#ffffff"))
		match String(ev.get("op", "state")):
			"state":
				modulate_node.color = target
			"fade":
				var over := float(ev.get("params", {}).get("over", 1.0))
				var k := clampf((t - lt) / maxf(over, EPS), 0.0, 1.0)
				if not ev.has("_from"):
					ev["_from"] = modulate_node.color
				modulate_node.color = (ev["_from"] as Color).lerp(target, k)

func _fire_audio(t: float) -> void:
	for ev in audio_events:
		if ev["fired"] or t + EPS < float(ev["t"]):
			continue
		ev["fired"] = true
		var path: String = assets_root.path_join(String(ev["file"]))
		if not FileAccess.file_exists(path):
			push_warning("audio missing, skipped: " + path)
			continue
		var player := AudioStreamPlayer.new()
		var stream := AudioStreamWAV.new()
		var f := FileAccess.open(path, FileAccess.READ)
		# minimal PCM16 WAV reader (44-byte canonical header)
		var bytes := f.get_buffer(f.get_length())
		stream.format = AudioStreamWAV.FORMAT_16_BITS
		stream.mix_rate = bytes.decode_u32(24)
		stream.stereo = bytes.decode_u16(22) == 2
		stream.data = bytes.slice(44)
		player.stream = stream
		player.volume_db = float(ev.get("gain_db", 0.0))
		add_child(player)
		player.play()


# =========================================================================
# PlaceholderBiped — flat-shape test character. Its only job: prove the
# MOTION reads as alive. Follow-through ears, eased turns, bob and settle
# overshoot — the difference between a character and a puppet lives here.
# =========================================================================
class PlaceholderBiped:
	extends Node2D

	var facing := 1
	var expression := "neutral"
	var blink_now := false
	var walk_key := -1.0
	var walk_from := Vector2.ZERO

	var hip: Node2D
	var torso: Polygon2D
	var head_pivot: Node2D
	var head: Polygon2D
	var ear_l: Polygon2D
	var ear_r: Polygon2D
	var eye_l: Polygon2D
	var eye_r: Polygon2D
	var pupil_l: Polygon2D
	var pupil_r: Polygon2D
	var brow_l: Polygon2D
	var brow_r: Polygon2D
	var mouth: Polygon2D
	var arm_l: Node2D
	var arm_r: Node2D
	var leg_l: Node2D
	var leg_r: Node2D

	const BODY := Color("#b9b4ac")
	const BELLY := Color("#f3ead9")
	const DARK := Color("#5a3d2b")

	func _init() -> void:
		hip = Node2D.new(); add_child(hip)
		leg_l = _limb(hip, Vector2(-16, 0), 44, 12, BODY)
		leg_r = _limb(hip, Vector2(16, 0), 44, 12, BODY)
		torso = _ellipse(hip, Vector2(0, -52), 42, 56, BODY)
		_ellipse(hip, Vector2(0, -44), 26, 38, BELLY)
		arm_l = _limb(hip, Vector2(-34, -70), 38, 10, BODY)
		arm_r = _limb(hip, Vector2(34, -70), 38, 10, BODY)
		head_pivot = Node2D.new(); head_pivot.position = Vector2(0, -108); hip.add_child(head_pivot)
		ear_l = _ellipse(head_pivot, Vector2(-18, -54), 10, 34, BODY)
		ear_r = _ellipse(head_pivot, Vector2(18, -54), 10, 34, BODY)
		head = _ellipse(head_pivot, Vector2.ZERO, 34, 30, BODY)
		eye_l = _ellipse(head_pivot, Vector2(-13, -6), 7, 9, Color.WHITE)
		eye_r = _ellipse(head_pivot, Vector2(13, -6), 7, 9, Color.WHITE)
		pupil_l = _ellipse(head_pivot, Vector2(-12, -5), 3, 4, DARK)
		pupil_r = _ellipse(head_pivot, Vector2(14, -5), 3, 4, DARK)
		brow_l = _ellipse(head_pivot, Vector2(-13, -18), 8, 2, DARK)
		brow_r = _ellipse(head_pivot, Vector2(13, -18), 8, 2, DARK)
		mouth = _ellipse(head_pivot, Vector2(0, 12), 8, 4, DARK)

	func _ellipse(parent: Node2D, at: Vector2, rx: float, ry: float, col: Color) -> Polygon2D:
		var poly := Polygon2D.new()
		var points: PackedVector2Array = []
		for i in range(20):
			var a := TAU * i / 20.0
			points.append(Vector2(cos(a) * rx, sin(a) * ry))
		poly.polygon = points
		poly.color = col
		poly.position = at
		parent.add_child(poly)
		return poly

	func _limb(parent: Node2D, at: Vector2, length: float, width: float, col: Color) -> Node2D:
		var pivot := Node2D.new(); pivot.position = at; parent.add_child(pivot)
		var poly := Polygon2D.new()
		poly.polygon = PackedVector2Array([
			Vector2(-width / 2, 0), Vector2(width / 2, 0),
			Vector2(width / 2, length), Vector2(-width / 2, length)])
		poly.color = col
		pivot.add_child(poly)
		return pivot

	# -- poses: pure functions of local time --------------------------------

	func _face() -> void:
		scale.x = absf(scale.x) * facing

	func pose_idle(t: float) -> void:
		_face()
		hip.position.y = sin(t * 1.6) * 2.0          # breath
		head_pivot.rotation = sin(t * 0.9) * 0.02
		_swing(0.0, 0.0)
		_ears(sin(t * 1.6) * 0.04)
		_apply_face(0.1)

	func pose_walk(t: float, speed: float) -> void:
		_face()
		var w := t * 7.0 * speed
		hip.position.y = -absf(sin(w)) * 6.0          # bob
		hip.rotation = sin(w) * 0.03
		_swing(sin(w) * 0.55, -sin(w) * 0.45)
		head_pivot.rotation = -sin(w) * 0.05
		_ears(-sin(w - 0.6) * 0.22)                   # follow-through lag
		_apply_face(0.1)

	func pose_settle(t: float) -> void:
		_face()
		# overshoot then damp — the settle that sells weight
		var k := exp(-t * 5.0) * sin(t * 14.0)
		hip.position.y = k * 5.0
		hip.rotation = k * 0.05
		_swing(k * 0.3, -k * 0.3)
		_ears(k * 0.3)
		_apply_face(0.1)

	func pose_talk(t: float, open: float) -> void:
		_face()
		hip.position.y = sin(t * 2.2) * 1.5
		hip.rotation = 0.03                            # lean in
		head_pivot.rotation = sin(t * 3.1) * 0.03
		_swing(0.12, -0.05)
		_ears(sin(t * 2.2) * 0.05)
		_apply_face(open)

	func pose_sad_idle(t: float) -> void:
		_face()
		hip.position.y = sin(t * 1.2) * 1.5 + 3.0
		head_pivot.rotation = 0.10                     # head hangs
		_swing(0.15, 0.15)
		_ears(0.55)                                    # ears droop
		expression = "sad"
		_apply_face(0.05)

	func gaze(dir: float) -> void:
		head_pivot.rotation += dir * 0.22
		pupil_l.position.x = -12 + dir * 3.0
		pupil_r.position.x = 14 + dir * 3.0

	func _swing(arms: float, legs: float) -> void:
		arm_l.rotation = arms
		arm_r.rotation = -arms
		leg_l.rotation = legs
		leg_r.rotation = -legs

	func _ears(droop: float) -> void:
		ear_l.rotation = -droop
		ear_r.rotation = droop

	func _apply_face(mouth_open: float) -> void:
		mouth.scale.y = 1.0 + mouth_open * 4.0
		mouth.scale.x = 1.0 - mouth_open * 0.25
		var blink_scale := 0.12 if blink_now else 1.0
		eye_l.scale.y = blink_scale
		eye_r.scale.y = blink_scale
		match expression:
			"worried":
				brow_l.rotation = 0.35; brow_r.rotation = -0.35
				brow_l.position.y = -20; brow_r.position.y = -20
			"sad":
				brow_l.rotation = 0.5; brow_r.rotation = -0.5
				mouth.rotation = PI
			_:
				brow_l.rotation = 0.0; brow_r.rotation = 0.0
				mouth.rotation = 0.0
