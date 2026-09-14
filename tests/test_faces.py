import io
import json
import os
import socket
import tempfile
import unittest

import cairo

from facegate import faces, faces_helper
from facegate.edge_hud import DOT_STYLE, PREVIEW_FEATHER, HudServer, preview_look, PreviewModel
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
    def test_preview_keeps_the_camera_rate_while_detection_is_slow(self):
        import threading
        import time as clock

        import numpy

        scanner = faces.Scanner.__new__(faces.Scanner)
        import cv2

        scanner.cv2 = cv2
        scanner.clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        scanner.max_height = 320
        scanner.dark_threshold = 100
        scanner.encode = None
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

        detections = []

        def detector(image, _upsample):
            # A detector far slower than the camera.
            detections.append(threading.get_ident())
            clock.sleep(0.12)
            return [Face()]

        scanner.detector = detector
        scanner.read_frame = read_frame
        started = clock.monotonic()
        frames = 0
        for event in scanner.scan(PREVIEW_WIDTH, PREVIEW_HEIGHT, lambda: clock.monotonic() - started > 1.0):
            frames += event["event"] == "frame"
        self.assertGreaterEqual(frames, 24)
        self.assertLessEqual(len(detections), 10)
        self.assertNotIn(threading.get_ident(), detections)


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
        from facegate import halftone

        self.halftone = halftone

    def test_feather_is_opaque_inside_and_clear_at_the_rim(self):
        alpha = self.halftone.feather_alpha(PREVIEW_WIDTH, PREVIEW_HEIGHT, 30, PREVIEW_FEATHER)
        texels = self.halftone.FEATHER_TEXELS
        middle = alpha.shape[0] // 2
        self.assertEqual(alpha[middle, alpha.shape[1] // 2], 255)
        self.assertEqual(alpha[0, 0], 0)
        self.assertEqual(alpha[middle, 0], 0)
        self.assertLess(alpha[middle, PREVIEW_FEATHER * texels // 2], 255)

    def test_dots_fill_the_field_on_a_hexagonal_grid(self):
        grid = self.halftone.dot_grid(PREVIEW_WIDTH, PREVIEW_HEIGHT, DOT_STYLE["pitch"])
        centres = self.halftone.dot_centres(grid)
        radius = DOT_STYLE["max_radius"]
        self.assertGreater(len(centres), 1000)
        self.assertTrue(all(radius <= x <= PREVIEW_WIDTH - radius for x, _y in centres))
        ys = sorted({y for _x, y in centres})
        self.assertAlmostEqual(ys[1] - ys[0], ys[2] - ys[1])
        # Odd rows sit exactly half a pitch across, so one tile repeats the grid.
        self.assertAlmostEqual(grid["left_odd"] - grid["left_even"], grid["pitch"] / 2)

    def test_the_distance_tile_is_zero_at_every_dot_and_rises_between(self):
        grid = self.halftone.dot_grid(PREVIEW_WIDTH, PREVIEW_HEIGHT, DOT_STYLE["pitch"])
        tile = self.halftone.distance_tile(grid["pitch"], grid["row_height"], DOT_STYLE["max_radius"])
        height, width = tile.shape
        self.assertLess(tile[height // 4, width // 2], 20)
        self.assertLess(tile[3 * height // 4, 0], 20)
        self.assertEqual(tile[height // 2, width // 4], 255)

    def test_brighter_pixels_make_bigger_dots_whatever_the_exposure(self):
        halftone = self.halftone.Halftone(PREVIEW_WIDTH, PREVIEW_HEIGHT, DOT_STYLE)
        pixels = bytes(20 if x < PREVIEW_WIDTH // 2 else 60 for _y in range(PREVIEW_HEIGHT) for x in range(PREVIEW_WIDTH))
        sizes = halftone.sizes(pixels)
        centres = self.halftone.dot_centres(halftone.grid)
        left = [size for (x, _y), size in zip(centres, sizes) if x < PREVIEW_WIDTH / 2 - 8]
        right = [size for (x, _y), size in zip(centres, sizes) if x > PREVIEW_WIDTH / 2 + 8]
        self.assertEqual(max(left), 0)
        self.assertGreater(min(right), 0.95)
        with self.assertRaises(ValueError):
            halftone.submit(b"\x00" * 10)

    def test_frames_become_render_nodes_in_every_state(self):
        from gi.repository import Gtk

        halftone = self.halftone.Halftone(PREVIEW_WIDTH, PREVIEW_HEIGHT, DOT_STYLE)
        self.assertIsNone(halftone.dots(1.0, (1, 1, 1)))
        frame = bytes(range(256)) * (PREVIEW_WIDTH * PREVIEW_HEIGHT // 256) + bytes(PREVIEW_WIDTH * PREVIEW_HEIGHT % 256)
        halftone.submit(frame)
        halftone.submit(frame)
        for state in ("scanning", "success", "failure"):
            model = PreviewModel(state=state, progress=0.5, result=0.5, pulse=0.5, elapsed=1.3, state_elapsed=0.1)
            look = self.halftone.Look()
            look.tint, look.band_color, look.band_middle, look.band_alpha, look.shake = preview_look(model, PREVIEW_HEIGHT)
            snapshot = Gtk.Snapshot()
            halftone.snapshot(snapshot, 2.0, look)
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
