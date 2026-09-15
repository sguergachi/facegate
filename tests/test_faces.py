import io
import json
import os
import socket
import tempfile
import base64
import unittest
from pathlib import Path

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from facegate import cameras, faces, faces_helper
from facegate import facemesh as faces_mesh
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

    def enroll(self, username, face_name, stopped, width, height, mesh=None):
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
        loader = faces.load_face_mesh
        faces.load_face_mesh = lambda: None
        self.addCleanup(setattr, faces, "load_face_mesh", loader)

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

    def test_an_ir_preview_keeps_its_brightness(self):
        grey = faces._jpeg_to_greyscale(self.jpeg(rgba=0x303030FF), PREVIEW_WIDTH, PREVIEW_HEIGHT)
        self.assertEqual(len(grey), PREVIEW_WIDTH * PREVIEW_HEIGHT)
        self.assertEqual(len(set(grey)), 1)
        self.assertLess(abs(grey[0] - 0x30), 4)

    def test_a_bright_colour_preview_does_not_overflow_under_the_spotlight(self):
        # Luma 0xF1 inverts to 0x0E; 16-bit channel weighting wrapped it.
        grey = faces._jpeg_to_greyscale(self.jpeg(rgba=0xFFFF80FF), PREVIEW_WIDTH, PREVIEW_HEIGHT)
        spotlight = faces._spotlight(PREVIEW_WIDTH, PREVIEW_HEIGHT).ravel()
        # Where the spotlight is half lit, a wrapped value lands far from dark.
        half = int(abs(spotlight - 0.5).argmin())
        self.assertLess(grey[half], 0x20)

    def test_a_colour_preview_is_inverted_so_the_face_gets_the_bright_stars(self):
        # Luma of 0x4080C0 is 0x74; inverted, it lights like an IR-lit face.
        grey = faces._jpeg_to_greyscale(self.jpeg(), PREVIEW_WIDTH, PREVIEW_HEIGHT)
        self.assertEqual(len(grey), PREVIEW_WIDTH * PREVIEW_HEIGHT)
        middle = (PREVIEW_HEIGHT // 2) * PREVIEW_WIDTH + PREVIEW_WIDTH // 2
        self.assertLess(abs(grey[middle] - (255 - 0x74)), 8)
        # The spotlight darkens the room at the frame's edges.
        self.assertEqual(grey[0], 0)
        self.assertEqual(grey[PREVIEW_WIDTH - 1], 0)

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

class ScriptedBus:
    """A system bus whose EnrollStart plays back a list of Gaze signals."""

    def __init__(self, signals):
        self.handlers = {}
        self.signals = signals

    def signal_subscribe(self, _sender, _interface, member, _path, _arg0, _flags, callback):
        self.handlers[member] = callback
        return len(self.handlers)

    def signal_unsubscribe(self, _subscription):
        pass

    def call_sync(self, _service, _path, _interface, method, *_rest):
        if method == "EnrollStart":
            for member, variant in self.signals:
                self.handlers[member](None, "", "", "", member, variant)
        return GLib.Variant("()", ())


def enroll_status(prompt, done=False):
    return ("EnrollStatus", GLib.Variant("(suubsd)", ("Face", 1, 5, done, prompt, 0.0)))


def face_status(status):
    return ("FaceStatus", GLib.Variant("(s)", (status,)))


class EnrollHintTests(unittest.TestCase):
    def test_the_pose_instruction_stays_up_until_something_is_wrong(self):
        bus = ScriptedBus([
            enroll_status("look-left"),
            face_status("ready"),
            face_status("usable"),
            face_status("too-dark"),
            face_status("ready"),
            enroll_status("completed", done=True),
        ])
        events = faces.GazeClient(bus=bus).enroll("someone", "Face", lambda: False)
        hints = [event["text"] for event in events if event["event"] == "hint"]
        self.assertEqual(hints, [
            "Waiting for the camera…",
            "Slowly turn your head to the left.",
            "Too dark. Move a little closer.",
            "Slowly turn your head to the left.",
        ])


class ClaimTests(unittest.TestCase):
    def test_a_face_check_taking_the_claim_cannot_leave_the_camera_on(self):
        class PreemptedBus(ScriptedBus):
            def __init__(self):
                super().__init__([])
                self.owner = None
                self.calls = []
                self.enrolling = False

            def call_sync(self, service, path, interface, method, *rest):
                self.calls.append(method)
                if method == "Claim":
                    self.owner = "helper"
                elif method == "EnrollStart":
                    # Polkit's face check claims as root and releases.
                    self.owner = None
                    self.enrolling = True
                elif method in ("EnrollStop", "Release"):
                    if self.owner != "helper":
                        raise GLib.Error("GDBus.Error:org.freedesktop.DBus.Error.Failed: Daemon is not claimed")
                    if method == "EnrollStop":
                        self.enrolling = False
                    else:
                        self.owner = None
                return GLib.Variant("()", ())

        bus = PreemptedBus()
        list(faces.GazeClient(bus=bus).enroll("someone", "Face", lambda: True))
        self.assertFalse(bus.enrolling)
        self.assertEqual(bus.calls, ["Claim", "EnrollStart", "Claim", "EnrollStop", "Release"])


class GuidanceTests(unittest.TestCase):
    @staticmethod
    def face(yaw=0.0, pitch=0.5):
        import numpy

        points = numpy.zeros((478, 3), numpy.float32)
        points[468, :2] = (100, 100)
        points[473, :2] = (140, 100)
        points[61, :2] = (110, 140)
        points[291, :2] = (130, 140)
        points[1, :2] = (120 + yaw * 40, 100 + pitch * 40)
        return points

    def guide(self):
        from facegate import guidance

        self.now = 100.0
        return guidance.Guide(clock=lambda: self.now)

    def test_head_pose_measures_what_gaze_measures(self):
        from facegate import guidance

        yaw, pitch = guidance.head_pose(self.face(yaw=-0.2, pitch=0.3))
        self.assertAlmostEqual(yaw, -0.2, places=4)
        self.assertAlmostEqual(pitch, 0.3, places=4)

    def test_it_says_which_way_to_turn_until_the_pose_is_reached(self):
        guide = self.guide()
        guide.prompt("look-straight")
        guide.pose(self.face(yaw=0.3))
        self.assertEqual(guide.hint(), "Turn a little to the left, to face the camera.")
        guide.pose(self.face())
        self.assertIsNone(guide.hint())  # waits to be sure
        self.now += 0.5
        self.assertEqual(guide.hint(), "Hold still…")
        guide.prompt("captured")
        self.assertEqual(guide.hint(), "Got it.")
        self.assertEqual(guide.baseline, (0.0, 0.5))
        self.now += 1
        guide.prompt("look-left")
        guide.pose(None)
        guide.hint()
        self.now += 0.5
        guide.hint()
        self.assertEqual(guide.shown, "Slowly turn your head to the left.")
        guide.pose(self.face(yaw=-0.1))
        self.assertEqual(guide.advice(), "A little more to the left…")
        guide.pose(self.face(yaw=-0.2))
        self.assertEqual(guide.advice(), "Hold it there…")
        guide.status("too-dark")
        self.assertEqual(guide.hint(), "Too dark. Face a light, or move closer.")

    def test_tilting_asks_to_keep_facing_forward(self):
        guide = self.guide()
        guide.prompt("look-straight")
        guide.pose(self.face())
        guide.prompt("captured")
        self.now += 1
        guide.prompt("look-up")
        guide.pose(self.face(yaw=0.3, pitch=0.4))
        self.assertEqual(guide.advice(), "Keep your head facing forward as you tilt.")
        guide.pose(self.face(pitch=0.42))
        self.assertEqual(guide.advice(), "Hold it there…")


class MeshPreviewTests(unittest.TestCase):
    class FakeMesh:
        def __init__(self, answers):
            self.answers = list(answers)

        def find(self, image):
            assert image.ndim == 3 and image.shape[2] == 3
            return self.answers.pop(0)

    def test_previews_become_face_mesh_points_in_preview_pixels(self):
        import numpy

        mesh = numpy.array([[100, 100, 0.0], [200, 300, 1.0], [150, 200, 0.5]], numpy.float32)
        bus = ScriptedBus([
            ("PreviewFrame", GLib.Variant("(ay)", (PreviewTests.jpeg(),))),
            enroll_status("completed", done=True),
        ])
        events = list(faces.GazeClient(bus=bus).enroll("someone", "Face", lambda: False, mesh=self.FakeMesh([mesh])))
        meshes = [event for event in events if event["event"] == "mesh"]
        self.assertEqual(len(meshes), 1)
        self.assertNotIn("frame", [event["event"] for event in events])
        points = numpy.frombuffer(base64.b64decode(meshes[0]["points"]), numpy.float32).reshape(-1, 3)
        # The face fills the preview's height, centred.
        self.assertAlmostEqual(float(points[:, 1].max() - points[:, 1].min()), PREVIEW_HEIGHT / faces_mesh.Framing.ZOOM, places=3)
        self.assertAlmostEqual(float(points[:, 0].mean()), PREVIEW_WIDTH / 2, places=3)
        # Each point keeps its nearness.
        self.assertLess(points[0, 2], points[2, 2])
        self.assertEqual(points[1, 2], 1.0)

    def test_a_mirrored_eye_pair_is_not_a_face_upside_down(self):
        import math

        self.assertAlmostEqual(faces_mesh.upright(math.pi - 0.1), -0.1)
        self.assertAlmostEqual(faces_mesh.upright(0.2), 0.2)
        self.assertAlmostEqual(faces_mesh.upright(-math.pi + 0.3), 0.3)

    @staticmethod
    def face_mesh():
        """478 points scattered over a face-sized oval, nearest at its middle."""
        import numpy

        random = numpy.random.default_rng(3)
        angle = random.uniform(0, 6.283, 478)
        radius = numpy.sqrt(random.uniform(0, 1, 478))
        x = 156 + numpy.cos(angle) * radius * 55
        y = 104 + numpy.sin(angle) * radius * 75
        return numpy.stack([x, y, 1 - radius], axis=1).astype(numpy.float32)

    def test_dust_condenses_onto_the_face_and_spreads_when_it_is_lost(self):
        import numpy

        from facegate.edge_hud import STAR_STYLE
        from facegate.starlight import Starlight

        stars = Starlight(PREVIEW_WIDTH, PREVIEW_HEIGHT, STAR_STYLE)
        roles = [set(stars.face_stars.tolist()), set(stars.arrow_stars.tolist()), set(stars.ambient_stars.tolist())]
        self.assertFalse(roles[0] & roles[1] or roles[0] & roles[2] or roles[1] & roles[2])
        points = self.face_mesh()
        stars.submit_mesh(points)
        self.assertTrue(stars.face)
        rest, lit = stars.dust
        # Every mote rests on the face, not on its points, and light stipples it.
        self.assertTrue((rest[:, 0] > 90).all() and (rest[:, 0] < 222).all())
        self.assertGreater(len(numpy.unique(rest.round(1), axis=0)), len(points) * 2)
        self.assertTrue((lit == 0).any() and (lit > 0.2).any())
        for _ in range(180):
            stars.advance(1 / 60)
        settled = stars.position[stars.face_stars]
        self.assertLess(float(numpy.abs(settled - rest).max()), 2.5)
        stars.submit_mesh(numpy.zeros((0, 3), numpy.float32))
        self.assertFalse(stars.face)
        for _ in range(180):
            stars.advance(1 / 60)
        self.assertIsNone(stars.vertices)
        self.assertGreater(float(numpy.abs(stars.position[stars.face_stars] - rest).mean()), 10)

    def test_a_frame_without_a_mesh_still_lights_the_field(self):
        from facegate.edge_hud import STAR_STYLE
        from facegate.starlight import Starlight

        stars = Starlight(PREVIEW_WIDTH, PREVIEW_HEIGHT, STAR_STYLE)
        stars.submit(bytes(range(256)) * (PREVIEW_WIDTH * PREVIEW_HEIGHT // 256) + bytes(PREVIEW_WIDTH * PREVIEW_HEIGHT % 256))
        for _ in range(60):
            stars.advance(1 / 60)
        self.assertTrue(stars.face)
        self.assertGreater(float(stars.light.max()), 0.2)


class CoachingArrowTests(unittest.TestCase):
    def test_each_instruction_names_its_arrow(self):
        from facegate import guidance

        self.assertEqual(guidance.arrow_for("Slowly turn your head to the left."), "turn-left")
        self.assertEqual(guidance.arrow_for("A little more to the right…"), "turn-right")
        self.assertEqual(guidance.arrow_for("A little higher…"), "tilt-up")
        self.assertEqual(guidance.arrow_for("Lower your chin a little."), "tilt-down")
        self.assertEqual(guidance.arrow_for("Move a little to the left."), "move-left")
        self.assertEqual(guidance.arrow_for("Hold it there…"), "")

    def test_an_off_centre_face_is_told_which_way_to_move_as_seen_in_the_mirror(self):
        guide = GuidanceTests.guide(self)
        guide.prompt("look-straight")
        guide.pose(GuidanceTests.face(), (80, 140))  # the face sits right of centre
        guide.status("not-centered")
        self.assertEqual(guide.hint(), "Move a little to the left.")

    def test_arrow_stars_point_the_right_way_and_leave_the_face_alone(self):
        import numpy

        from facegate.edge_hud import STAR_STYLE
        from facegate.starlight import Starlight

        stars = Starlight(PREVIEW_WIDTH, PREVIEW_HEIGHT, STAR_STYLE)
        head = stars.arrow_wing_star
        for name, axis, sign in (("turn-left", 0, -1), ("turn-right", 0, 1), ("tilt-up", 1, -1), ("move-down", 1, 1)):
            points, light = stars.arrow_shape(name, 0.0)
            tip = points[head].mean(axis=0)
            tail = points[~head & (stars.arrow_seed < 0.3)].mean(axis=0)
            self.assertGreater((tip[axis] - tail[axis]) * sign, 5, name)
            self.assertTrue(numpy.isfinite(points).all())
        stars.set_arrow("turn-left")
        for _ in range(60):
            stars.advance(1 / 60)
        self.assertGreater(float(stars.light[stars.arrow_stars].max()), 0.3)
        stars.set_arrow("")
        for _ in range(90):
            stars.advance(1 / 60)
        self.assertLess(stars.arrow_amount, 0.01)

    def test_switching_cameras_keeps_the_face_where_it_was(self):
        import numpy

        framing = faces_mesh.Framing(PREVIEW_WIDTH, PREVIEW_HEIGHT)
        colour = numpy.array([[300, 200, 0.5], [340, 260, 1.0], [320, 300, 0.2]], numpy.float32)
        for _ in range(3):
            before = framing.place(colour + [8, 0, 0], (640, 480))
        # The IR camera sees the same face larger and elsewhere.
        infrared = numpy.array([[100, 80, 0.5], [160, 170, 1.0], [130, 230, 0.2]], numpy.float32)
        after = framing.place(infrared, (340, 340))
        centre = lambda points: (points[:, :2].min(axis=0) + points[:, :2].max(axis=0)) / 2
        self.assertLess(float(numpy.abs(centre(after) - centre(before)).max()), 1.0)
        height = lambda points: float(numpy.ptp(points[:, 1]))
        self.assertAlmostEqual(height(after), height(before), delta=1.0)


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
