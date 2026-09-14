import io
import json
import os
import socket
import tempfile
import unittest

import cairo
import numpy

from facegate import faces, faces_helper
from facegate.edge_hud import PREVIEW_FEATHER, STAR_STYLE, HudServer, PreviewModel, preview_look
from facegate.protocol import MAX_STREAM_BYTES, PREVIEW_HEIGHT, PREVIEW_WIDTH, decode, encode


class ModelStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "models", "someone.dat")

    def tearDown(self):
        self.directory.cleanup()

    def test_round_trip_keeps_howdys_format(self):
        model = faces.new_model([], "Face", [[0.1, 0.2]], 1_700_000_000)
        faces.save_models(self.path, [model])
        with open(self.path) as stored:
            self.assertEqual(json.load(stored), [{"time": 1_700_000_000, "label": "Face", "id": 0, "data": [[0.1, 0.2]]}])
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")

    def test_removing_the_last_face_deletes_the_file_like_howdy(self):
        faces.save_models(self.path, [faces.new_model([], "Face", [[0.0]], 1)])
        faces.save_models(self.path, faces.remove_model(faces.load_models(self.path), 0))
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(faces.load_models(self.path), [])

    def test_ids_never_repeat_after_a_removal(self):
        models = [{"id": 0, "label": "Face", "data": []}, {"id": 1, "label": "Face 2", "data": []}]
        remaining = faces.remove_model(models, 0)
        self.assertEqual(faces.new_model(remaining, "x", [], 0)["id"], 2)

    def test_labels_skip_names_in_use(self):
        self.assertEqual(faces.next_label([]), "Face")
        self.assertEqual(faces.next_label([{"label": "Face"}]), "Face 2")
        self.assertEqual(faces.next_label([{"label": "Face"}, {"label": "Face 2"}]), "Face 3")
        self.assertEqual(faces.clean_label("  Glasses\x00 on and a very long label indeed "), "Glasses on and a very lo")


class FramingTests(unittest.TestCase):
    def test_no_face_shows_the_widest_view_at_the_preview_aspect(self):
        self.assertEqual(faces.crop_target(340, 340, None, 1.5), (170, 170, 340 / 1.5))

    def test_crop_stays_inside_the_frame(self):
        x, y, width, height = faces.crop_rect(330, 5, 200, 340, 340, 1.5)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + width, 340)
        self.assertLessEqual(y + height, 340)

    def test_follow_eases_rather_than_jumping(self):
        follow = faces.Follow()
        follow.step((100, 100, 200), 1 / 30)
        follow.step((200, 100, 200), 1 / 30)
        self.assertTrue(100 < follow.center_x < 200)

    def test_samples_need_novelty_or_patience(self):
        state = faces.ScanState(samples=[[0.0, 0.0]], last_sample=10.0)
        self.assertFalse(faces.accepts(state, [0.01, 0.0], 10.4))
        self.assertTrue(faces.accepts(state, [0.5, 0.0], 10.4))
        self.assertTrue(faces.accepts(state, [0.01, 0.0], 10.0 + faces.SAMPLE_PATIENCE + 0.1))


class ThreadedScanTests(unittest.TestCase):
    def test_preview_keeps_the_camera_rate_while_analysis_holds_the_interpreter(self):
        import time as clock

        import cv2

        scanner = faces.Scanner.__new__(faces.Scanner)
        scanner.cv2 = cv2
        scanner.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        scanner.max_height = 320
        scanner.dark_threshold = 100
        lit = numpy.full((340, 340), 120, numpy.uint8)

        def read_frame():
            clock.sleep(1 / 30)
            return lit

        class Face:
            def left(self): return 110
            def top(self): return 90
            def right(self): return 230
            def bottom(self): return 230
            def height(self): return 140

        def detector(image, _upsample):
            clock.sleep(0.03)
            return [Face()]

        def encode(_color, _rect):
            # Like dlib's recognition: a quarter second that never lets go of
            # Python's interpreter lock.
            deadline = clock.perf_counter() + 0.25
            spins = 0
            while clock.perf_counter() < deadline:
                spins += 1
            return [float(clock.monotonic())]

        scanner.detector, scanner.encode, scanner.read_frame = detector, encode, read_frame
        started = clock.monotonic()
        frames, faces_seen, progress = 0, [], 0
        for event in scanner.scan(PREVIEW_WIDTH, PREVIEW_HEIGHT, lambda: clock.monotonic() - started > 2.0):
            if event["event"] == "frame":
                frames += 1
                faces_seen.append(event["face"])
            progress += event["event"] == "progress"
        self.assertGreaterEqual(frames, 48)
        # Analysis still ran, in its own process, and frames say when a face is found.
        self.assertGreater(progress, 0)
        self.assertFalse(faces_seen[0])
        self.assertTrue(faces_seen[-1])


class FakeScanner:
    def __init__(self, events):
        self.events = events

    def scan(self, width, height, stopped):
        for event in self.events:
            if stopped():
                return
            yield event


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.directory.name, "models"))
        self.output = io.BytesIO()
        self.helper = faces_helper.Helper("someone", self.output, self.directory.name)
        self.helper.commands.read = lambda timeout: None
        self.original = faces.howdy_scanner

    def tearDown(self):
        faces.howdy_scanner = self.original
        self.directory.cleanup()

    def events(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def test_enroll_streams_then_saves_one_model_with_every_sample(self):
        released = []
        stream = [{"event": "hint", "text": "Hold still"}, {"event": "progress", "value": 0.5}, {"event": "captured", "encodings": [[0.1], [0.2]]}]
        faces.howdy_scanner = lambda _dir: (FakeScanner(stream), lambda: released.append(True))
        self.helper.handle({"cmd": "enroll", "width": PREVIEW_WIDTH, "height": PREVIEW_HEIGHT})
        kinds = [event["event"] for event in self.events()]
        self.assertEqual(kinds, ["hint", "progress", "added"])
        self.assertEqual(self.events()[-1]["faces"][0]["label"], "Face")
        self.assertEqual(faces.load_models(self.helper.path)[0]["data"], [[0.1], [0.2]])
        self.assertEqual(released, [True])

    def test_cancel_stops_the_scan_without_saving(self):
        faces.howdy_scanner = lambda _dir: (FakeScanner([{"event": "progress", "value": 0.2}] * 3), lambda: None)
        commands = iter([None, {"cmd": "cancel"}])
        self.helper.commands.read = lambda timeout: next(commands, None)
        self.helper.handle({"cmd": "enroll"})
        self.assertEqual(self.events()[-1], {"event": "cancelled"})
        self.assertFalse(os.path.exists(self.helper.path))

    def test_scan_errors_are_reported(self):
        def broken(_dir):
            raise faces.EnrollError("Howdy has no camera configured.")

        faces.howdy_scanner = broken
        self.helper.handle({"cmd": "enroll"})
        self.assertEqual(self.events(), [{"event": "error", "message": "Howdy has no camera configured."}])

    def test_oversized_previews_are_refused(self):
        self.helper.handle({"cmd": "enroll", "width": 4000, "height": 4000})
        self.assertEqual(self.events()[0]["event"], "error")

    def test_command_line_runs_one_operation(self):
        faces.save_models(self.helper.path, [faces.new_model([], "Face", [[0.0]], 1)])
        self.assertEqual(self.helper.run(["list"]), 0)
        self.assertEqual(self.events()[-1]["faces"][0]["label"], "Face")
        self.assertEqual(self.helper.run(["remove", "0"]), 0)
        self.assertEqual(self.events()[-1]["faces"], [])
        self.assertEqual(self.helper.run(["rm", "-rf", "/"]), 2)
        self.assertEqual(self.events()[-1]["event"], "error")

    def test_rename_keeps_the_face_and_cleans_the_name(self):
        faces.save_models(self.helper.path, [faces.new_model([], "Face", [[0.5]], 1)])
        self.assertEqual(self.helper.run(["rename", "0", "  Glasses\n"]), 0)
        self.assertEqual(self.events()[-1]["faces"][0]["label"], "Glasses")
        self.assertEqual(faces.load_models(self.helper.path)[0]["data"], [[0.5]])
        self.helper.handle({"cmd": "rename", "id": 0, "label": "   "})
        self.assertEqual(self.events()[-1]["event"], "error")
        self.assertEqual(faces.load_models(self.helper.path)[0]["label"], "Glasses")

    def test_remove(self):
        faces.save_models(self.helper.path, [faces.new_model([], "Face", [[0.0]], 1)])
        self.helper.handle({"cmd": "remove", "id": 0})
        self.assertEqual(self.events()[-1]["faces"], [])


class PreviewTests(unittest.TestCase):
    def setUp(self):
        from facegate import starlight

        self.starlight = starlight
        self.frame = bytes(range(256)) * (PREVIEW_WIDTH * PREVIEW_HEIGHT // 256) + bytes(PREVIEW_WIDTH * PREVIEW_HEIGHT % 256)

    def run_for(self, stars, seconds):
        for _ in range(round(seconds * 60)):
            stars.advance(1 / 60)

    def test_feather_is_clear_at_the_rim_and_full_inside(self):
        points = numpy.array([[PREVIEW_WIDTH / 2, PREVIEW_HEIGHT / 2], [0, 0], [0, PREVIEW_HEIGHT / 2]], numpy.float32)
        visible = self.starlight.feather(points, PREVIEW_WIDTH, PREVIEW_HEIGHT, 30, PREVIEW_FEATHER)
        self.assertEqual(visible[0], 1)
        self.assertEqual(visible[1], 0)
        self.assertEqual(visible[2], 0)

    def test_spots_fill_the_frame(self):
        spots = self.starlight.star_spots(PREVIEW_WIDTH, PREVIEW_HEIGHT, STAR_STYLE["pitch"])
        self.assertGreater(len(spots), 2000)
        self.assertTrue(((spots[:, 0] > 0) & (spots[:, 0] < PREVIEW_WIDTH) & (spots[:, 1] > 0) & (spots[:, 1] < PREVIEW_HEIGHT)).all())

    def test_the_cloud_shows_at_once_and_drifts_while_waiting_for_a_face(self):
        stars = self.starlight.Starlight(PREVIEW_WIDTH, PREVIEW_HEIGHT, STAR_STYLE)
        self.run_for(stars, 1.0)
        # No camera frame yet, and the cloud is already alive.
        self.assertEqual(stars.presence, 1.0)
        self.assertGreater(stars.light.sum(), 100)
        before = stars.position.copy()
        self.run_for(stars, 0.5)
        moved = numpy.hypot(*(stars.position - before).T)
        self.assertGreater(moved.mean(), 0.5)
        self.assertLess(moved.mean(), 20)

    def test_stars_gather_into_the_face_then_return_to_the_cloud(self):
        stars = self.starlight.Starlight(PREVIEW_WIDTH, PREVIEW_HEIGHT, STAR_STYLE)
        self.run_for(stars, 0.5)
        stars.submit(self.frame, face=True)
        self.run_for(stars, 1.5)
        self.assertGreater(stars.gathered, 0.95)
        self.assertLess(numpy.hypot(*(stars.position - stars.spots).T).mean(), 1.0)
        stars.submit(self.frame, face=False)
        self.run_for(stars, 1.5)
        self.assertLess(stars.gathered, 0.05)
        # Within a spring's trailing distance of the still-drifting cloud.
        self.assertLess(numpy.hypot(*(stars.position - stars._cloud()).T).mean(), 5.0)

    def test_brighter_pixels_make_brighter_stars_whatever_the_exposure(self):
        stars = self.starlight.Starlight(PREVIEW_WIDTH, PREVIEW_HEIGHT, STAR_STYLE)
        pixels = bytes(20 if x < PREVIEW_WIDTH // 2 else 60 for _y in range(PREVIEW_HEIGHT) for x in range(PREVIEW_WIDTH))
        brightness = stars.brightness(pixels)
        xs = stars.spots[:, 0]
        self.assertEqual(brightness[xs < PREVIEW_WIDTH / 2 - 8].max(), 0)
        self.assertGreater(brightness[xs > PREVIEW_WIDTH / 2 + 8].min(), 0.95)
        with self.assertRaises(ValueError):
            stars.submit(b"\x00" * 10)

    def test_stars_wink_out_when_done(self):
        stars = self.starlight.Starlight(PREVIEW_WIDTH, PREVIEW_HEIGHT, STAR_STYLE)
        stars.submit(self.frame)
        self.run_for(stars, 1.0)
        stars.fade_out()
        self.run_for(stars, 1.0)
        self.assertEqual(stars.presence, 0.0)
        self.assertFalse(stars.visible)
        self.assertEqual(stars.light.max(), 0)

    def test_stars_splat_at_display_density_and_become_render_nodes(self):
        from gi.repository import Gtk

        stars = self.starlight.Starlight(PREVIEW_WIDTH, PREVIEW_HEIGHT, STAR_STYLE)
        stars.submit(self.frame)
        self.run_for(stars, 0.5)
        texture = stars.splat(2.0)
        self.assertEqual((texture.get_width(), texture.get_height()), (PREVIEW_WIDTH * 2, PREVIEW_HEIGHT * 2))
        for state in ("scanning", "success", "failure"):
            look = self.starlight.Look()
            look.tint, look.glow, look.shake = preview_look(PreviewModel(state=state, result=0.5, pulse=0.5, state_elapsed=0.1))
            snapshot = Gtk.Snapshot()
            stars.snapshot(snapshot, 2.0, look)
            self.assertIsNotNone(snapshot.to_node())


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
