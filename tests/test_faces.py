import io
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from facegate import cameras, faces, faces_helper
from facegate.edge_hud import HudServer
from facegate.protocol import MAX_STREAM_BYTES, PREVIEW_HEIGHT, PREVIEW_WIDTH, decode, encode


class FaceRecordTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.metadata = Path(self.directory.name) / "faces.json"

    def tearDown(self):
        self.directory.cleanup()

    def test_summary_contains_only_display_metadata(self):
        records = [("Face 2", 5, True, True), ("Face", 3, True, False)]
        result = faces.summarize(records, {"Face": 1_700_000_000})
        self.assertEqual(
            result,
            [
                {"id": "Face", "label": "Face", "time": 1_700_000_000, "samples": 3, "rgb": True, "ir": False},
                {"id": "Face 2", "label": "Face 2", "time": 0, "samples": 5, "rgb": True, "ir": True},
            ],
        )
        self.assertNotIn("embedding", json.dumps(result))

    def test_display_timestamps_are_private_and_atomic(self):
        faces.save_metadata({"Face": 1_700_000_000}, self.metadata)
        self.assertEqual(faces.load_metadata(self.metadata), {"Face": 1_700_000_000})
        self.assertEqual(oct(os.stat(self.metadata).st_mode & 0o777), "0o600")

    def test_invalid_metadata_is_ignored(self):
        self.metadata.write_text('["not", "a", "mapping"]')
        self.assertEqual(faces.load_metadata(self.metadata), {})

    def test_labels_skip_names_in_use(self):
        self.assertEqual(faces.next_label([]), "Face")
        self.assertEqual(faces.next_label([{"label": "Face"}]), "Face 2")
        self.assertEqual(faces.next_label([{"label": "Face"}, {"label": "Face 2"}]), "Face 3")
        self.assertEqual(faces.clean_label("  Glasses\x00 on and a very long label indeed "), "Glasses on and a very lo")

    def test_gaze_missing_user_means_no_faces_but_other_errors_propagate(self):
        client = object.__new__(faces.GazeClient)

        def missing(*_args):
            raise faces.GazeError("User 'someone' not found")

        client._call = missing
        self.assertEqual(client.list_faces("someone"), [])

        def unavailable(*_args):
            raise faces.GazeError("Gaze daemon unavailable.")

        client._call = unavailable
        with self.assertRaisesRegex(faces.GazeError, "daemon unavailable"):
            client.list_faces("someone")


class FakeGazeClient:
    def __init__(self, records=None, stream=None, error=None):
        self.records = list(records or [])
        self.stream = list(stream or [])
        self.error = error
        self.deleted = []
        self.renamed = []
        self.enrollments = []

    def list_faces(self, username):
        if self.error:
            raise self.error
        return list(self.records)

    def delete_face(self, username, face_name):
        self.deleted.append((username, face_name))
        self.records = [record for record in self.records if record[0] != face_name]
        return True

    def rename_face(self, username, old_name, new_name):
        self.renamed.append((username, old_name, new_name))
        self.records = [
            (new_name, samples, rgb, ir) if name == old_name else (name, samples, rgb, ir)
            for name, samples, rgb, ir in self.records
        ]
        return True

    def enroll(self, username, face_name, stopped, width, height):
        self.enrollments.append((username, face_name, width, height))
        if self.error:
            raise self.error
        for event in self.stream:
            if stopped():
                return
            if event.get("event") == "completed":
                self.records.append((face_name, 5, True, True))
            yield event


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.metadata = Path(self.directory.name) / "faces.json"
        self.output = io.BytesIO()
        self.client = FakeGazeClient()
        self.helper = faces_helper.Helper(
            "someone",
            self.output,
            client=self.client,
            metadata_file=self.metadata,
        )
        self.helper.commands.read = lambda timeout: None

    def tearDown(self):
        self.directory.cleanup()

    def events(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def test_enroll_streams_events_and_gaze_owns_the_saved_face(self):
        self.client.stream = [
            {"event": "hint", "text": "Hold still"},
            {"event": "progress", "value": 0.5},
            {"event": "completed"},
        ]
        self.helper.handle({"cmd": "enroll", "width": PREVIEW_WIDTH, "height": PREVIEW_HEIGHT})
        self.assertEqual([event["event"] for event in self.events()], ["hint", "progress", "added"])
        self.assertEqual(self.events()[-1]["faces"][0]["label"], "Face")
        self.assertEqual(self.client.enrollments, [("someone", "Face", PREVIEW_WIDTH, PREVIEW_HEIGHT)])
        self.assertGreater(faces.load_metadata(self.metadata)["Face"], 0)

    def test_cancel_stops_enrollment_without_writing_metadata(self):
        self.client.stream = [{"event": "progress", "value": 0.2}] * 3
        commands = iter([None, {"cmd": "cancel"}])
        self.helper.commands.read = lambda timeout: next(commands, None)
        self.helper.handle({"cmd": "enroll"})
        self.assertEqual(self.events()[-1], {"event": "cancelled"})
        self.assertFalse(self.metadata.exists())

    def test_gaze_errors_are_reported(self):
        self.client.error = faces.GazeError("Gaze daemon unavailable.")
        self.helper.handle({"cmd": "enroll"})
        self.assertEqual(self.events(), [{"event": "error", "message": "Gaze daemon unavailable."}])

    def test_oversized_previews_are_refused(self):
        self.helper.handle({"cmd": "enroll", "width": 4000, "height": 4000})
        self.assertEqual(self.events()[0]["event"], "error")

    def test_command_line_runs_one_operation(self):
        self.client.records = [("Face", 5, True, True)]
        self.assertEqual(self.helper.run(["list"]), 0)
        self.assertEqual(self.events()[-1]["faces"][0]["label"], "Face")
        self.assertEqual(self.helper.run(["remove", "Face"]), 0)
        self.assertEqual(self.events()[-1]["faces"], [])
        self.assertEqual(self.client.deleted, [("someone", "Face")])
        self.assertEqual(self.helper.run(["rm", "-rf", "/"]), 2)
        self.assertEqual(self.events()[-1]["event"], "error")

    def test_rename_is_owned_by_gaze_and_preserves_the_added_time(self):
        self.client.records = [("Face", 5, True, True)]
        faces.save_metadata({"Face": 1_700_000_000}, self.metadata)
        self.assertEqual(self.helper.run(["rename", "Face", "Glasses"]), 0)
        self.assertEqual(self.client.renamed, [("someone", "Face", "Glasses")])
        self.assertEqual(faces.load_metadata(self.metadata), {"Glasses": 1_700_000_000})
        self.assertEqual(self.events()[-1]["faces"][0]["label"], "Glasses")

    def test_remove_rejects_unknown_face_without_calling_gaze(self):
        self.helper.handle({"cmd": "remove", "id": "Not enrolled"})
        self.assertEqual(self.events()[-1]["event"], "error")
        self.assertEqual(self.client.deleted, [])


class PreviewTests(unittest.TestCase):
    def test_live_signal_dispatch_is_bounded(self):
        class BusyContext:
            def __init__(self):
                self.iterations = 0

            def pending(self):
                return True

            def iteration(self, may_block):
                self.iterations += 1
                self.asserted_may_block = may_block

        context = BusyContext()
        self.assertEqual(faces.drain_main_context(context), 32)
        self.assertEqual(context.iterations, 32)
        self.assertFalse(context.asserted_may_block)

    @staticmethod
    def jpeg(width=640, height=480, rgba=0x4080C0FF):
        from gi.repository import GdkPixbuf

        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
        pixbuf.fill(rgba)
        return bytes(pixbuf.save_to_bufferv("jpeg", [], [])[1])

    def test_jpeg_preview_becomes_a_greyscale_frame(self):
        grey = faces._jpeg_to_greyscale(self.jpeg(), PREVIEW_WIDTH, PREVIEW_HEIGHT)
        self.assertEqual(len(grey), PREVIEW_WIDTH * PREVIEW_HEIGHT)
        self.assertEqual(len(set(grey)), 1)
        self.assertLess(abs(grey[0] - 0x74), 8)

    def test_a_flood_of_previews_keeps_only_the_newest_and_never_delays_progress(self):
        from gi.repository import GLib

        jpeg = self.jpeg()

        class FloodBus:
            def __init__(self):
                self.handlers = {}

            def signal_subscribe(self, _sender, _interface, member, _path, _arg0, _flags, callback):
                self.handlers[member] = callback
                return len(self.handlers)

            def signal_unsubscribe(self, _subscription):
                pass

            def emit(self, member, variant):
                self.handlers[member](None, "", "", "", member, variant)

            def call_sync(self, _service, _path, _interface, method, *_rest):
                if method == "EnrollStart":
                    for _ in range(200):
                        self.emit("PreviewFrame", GLib.Variant("(ay)", (jpeg,)))
                    self.emit("EnrollStatus", GLib.Variant("(suubsd)", ("Face", 5, 5, True, "completed", 0.0)))
                return GLib.Variant("()", ())

        client = faces.GazeClient(bus=FloodBus())
        events = list(client.enroll("someone", "Face", lambda: False))
        kinds = [event["event"] for event in events]
        self.assertEqual(kinds.count("frame"), 1)
        self.assertEqual(kinds[-2:], ["progress", "completed"])

class CameraTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.sysfs = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def node(self, name, modalias=None):
        device = self.sysfs / name / "device"
        device.mkdir(parents=True)
        if modalias:
            (device / "modalias").write_text(modalias + "\n")

    def test_the_luma_only_node_is_the_infrared_camera(self):
        # A BRIO: colour, metadata, IR, metadata, all under one USB id.
        for name in ("video0", "video1", "video2", "video3"):
            self.node(name, "usb:v046Dp085Ed0317dcEFdsc02dp01ic0Eisc02ip00in03")
        formats = {"/dev/video0": [b"YUYV", b"MJPG"], "/dev/video2": [b"GREY"]}
        found = cameras.find_ir_camera(self.sysfs, lambda node: formats.get(node, []))
        self.assertEqual(found, "usb:046d:085e")

    def test_colour_cameras_are_not_mistaken_for_infrared(self):
        self.node("video0", "usb:v1234p5678")
        found = cameras.find_ir_camera(self.sysfs, lambda _node: [b"YUYV", b"GREY"])
        self.assertIsNone(found)

    def test_a_non_usb_ir_node_is_named_by_path(self):
        self.node("video10")
        self.node("video4")
        found = cameras.find_ir_camera(self.sysfs, lambda node: [b"Y16 "] if node == "/dev/video10" else [])
        self.assertEqual(found, "/dev/video10")

    class Gaze:
        def __init__(self, ir=""):
            self.settings = {"rgb": "primary", "ir": ir, "emitter_enabled": False, "dark_luma": 20, "parallel": "never"}
            self.changes = []

        def cameras(self):
            return dict(self.settings)

        def set_cameras(self, **changes):
            self.changes.append(changes)

    def test_gaze_is_given_the_infrared_camera_it_lacks(self):
        gaze = self.Gaze()
        cameras.configure_ir_camera(gaze, lambda: "usb:046d:085e")
        self.assertEqual(gaze.changes, [{"ir": "usb:046d:085e", "emitter_enabled": True}])

    def test_a_chosen_infrared_camera_is_never_replaced(self):
        gaze = self.Gaze(ir="/dev/video6")
        cameras.configure_ir_camera(gaze, lambda: "usb:046d:085e")
        self.assertEqual(gaze.changes, [])

    def test_only_the_camera_struct_changes_in_gaze_config(self):
        from gi.repository import GLib

        signature = "((ss)(sssdds)(ssbys)(bbbbbtts)(ud)(bdd)(b))"
        config = (
            ("cpu", "cpu"),
            ("medium", "", "", 0.0, 0.0, ""),
            ("primary", "", False, 20, "never"),
            (True, True, False, False, False, 0, 0, "screen_lock"),
            (2, 0.25),
            (True, 0.8, 2.0),
            (True,),
        )

        class ConfigBus:
            written = None

            def call_sync(self, _service, _path, _interface, method, parameters, *_rest):
                if method == "Get":
                    return GLib.Variant("(v)", (GLib.Variant(signature, config),))
                self.written = parameters.unpack()
                return GLib.Variant("()", ())

        bus = ConfigBus()
        faces.GazeClient(bus=bus).set_cameras(ir="usb:046d:085e", emitter_enabled=True)
        interface, name, value = bus.written
        self.assertEqual((interface, name), ("com.gundulabs.Gaze", "Config"))
        expected = list(config)
        expected[2] = ("primary", "usb:046d:085e", True, 20, "never")
        self.assertEqual(value, tuple(expected))


class StreamTests(unittest.TestCase):
    def test_a_frame_line_fits_the_stream_limit(self):
        line = encode({"frame": "A" * (PREVIEW_WIDTH * PREVIEW_HEIGHT * 4 // 3 + 4), "w": PREVIEW_WIDTH, "h": PREVIEW_HEIGHT})
        self.assertLess(len(line), MAX_STREAM_BYTES)
        with self.assertRaises(ValueError):
            decode(line)
        self.assertEqual(decode(line.rstrip(b"\n"), MAX_STREAM_BYTES)["w"], PREVIEW_WIDTH)

    def test_first_line_keeps_what_follows_it(self):
        reader, writer = socket.socketpair()
        writer.sendall(encode({"phase": "enroll"}) + b'{"hint":')
        first, rest = HudServer._first_line(reader)
        self.assertEqual(decode(first), {"phase": "enroll"})
        self.assertEqual(rest, b'{"hint":')
        reader.close()
        writer.close()


if __name__ == "__main__":
    unittest.main()
