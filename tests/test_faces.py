import io
import json
import os
import socket
import tempfile
import unittest

import cairo

from facegate import faces, faces_helper
from facegate.edge_hud import PREVIEW_FEATHER, HudServer, PreviewModel, feather_mask, grey_surface, paint_preview
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
    def test_feather_is_opaque_inside_and_clear_at_the_rim(self):
        mask = feather_mask(PREVIEW_WIDTH, PREVIEW_HEIGHT)
        data, stride = mask.get_data(), mask.get_stride()
        self.assertEqual(data[(PREVIEW_HEIGHT // 2) * stride + PREVIEW_WIDTH // 2], 255)
        self.assertEqual(data[0], 0)
        self.assertEqual(data[(PREVIEW_HEIGHT // 2) * stride], 0)
        self.assertLess(data[(PREVIEW_HEIGHT // 2) * stride + PREVIEW_FEATHER // 2], 255)

    def test_grey_frames_pad_rows_to_the_surface_stride(self):
        surface = grey_surface(bytes(range(30)) * 2, 30, 2)
        self.assertEqual(surface.get_stride(), 32)
        self.assertEqual(bytes(surface.get_data())[32:62], bytes(range(30)))
        with self.assertRaises(ValueError):
            grey_surface(b"\x00" * 10, 30, 2)

    def test_every_state_paints_with_and_without_frames(self):
        frame = grey_surface(bytes(PREVIEW_WIDTH * PREVIEW_HEIGHT), PREVIEW_WIDTH, PREVIEW_HEIGHT)
        for state in ("scanning", "success", "failure"):
            for progress, blend in ((0.0, 0.0), (0.6, 0.5), (1.0, 1.0)):
                for current, previous in ((None, None), (frame, None), (frame, frame)):
                    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, PREVIEW_WIDTH, PREVIEW_HEIGHT)
                    model = PreviewModel(state=state, progress=progress, result=progress, pulse=blend, blend=blend, elapsed=1.3)
                    paint_preview(cairo.Context(surface), PREVIEW_WIDTH, PREVIEW_HEIGHT, current, model, previous)


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
