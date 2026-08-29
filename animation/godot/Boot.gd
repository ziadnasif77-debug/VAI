# Boot — reads the Scene Document's dimension and instances the right player.
# The document decides; the renderer obeys (amendment #1 in both dimensions).
extends Node

func _ready() -> void:
	var argv := OS.get_cmdline_user_args()
	var scene_path := ""
	var i := 0
	while i < argv.size() - 1:
		if argv[i] == "--scene":
			scene_path = argv[i + 1]
		i += 1
	var dimension := "2d"
	if scene_path != "":
		var parsed = JSON.parse_string(FileAccess.get_file_as_string(scene_path))
		if parsed != null:
			dimension = String((parsed as Dictionary).get("dimension", "2d"))
	var player_path := "res://ScenePlayer3D.tscn" if dimension == "3d" else "res://ScenePlayer.tscn"
	add_child((load(player_path) as PackedScene).instantiate())
