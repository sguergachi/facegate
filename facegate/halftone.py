"""The enrollment preview's halftone, drawn on the GPU.

The camera arrives as a small greyscale frame at up to 30 per second. Drawing
thousands of dots for it on the CPU, in Python, stalls GTK's frame clock, so
this module hands the whole effect to one fragment shader in a Gtk.GLArea.

Each camera frame costs one 312 x 208 texture upload. The last two frames are
kept, and the shader blends from the older to the newer over the time frames
actually take to arrive, so the dots move continuously rather than stepping.
For every device pixel it finds the nearest dot on the hexagonal grid, reads
that dot's brightness (softened, auto-levelled, noise-floored and curved),
and covers the pixel with an anti-aliased circle, feathered into a rounded
rectangle, tinted, with a sweeping band of light.

The Python side does no per-dot or per-pixel work. GL is reached through
ctypes, for OpenGL or OpenGL ES, whichever GTK's context uses, so there is no
new dependency; without either, the widget reports `failed` and the preview
falls back to drawing on the CPU.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import time
from dataclasses import dataclass

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

try:
    import numpy
except ImportError:  # Howdy depends on NumPy; this only guards odd installs.
    numpy = None

GL_TRIANGLES = 0x0004
GL_BLEND = 0x0BE2
GL_VIEWPORT = 0x0BA2
GL_UNPACK_ALIGNMENT = 0x0CF5
GL_TEXTURE_2D = 0x0DE1
GL_UNSIGNED_BYTE = 0x1401
GL_RED = 0x1903
GL_LINEAR = 0x2601
GL_TEXTURE_MAG_FILTER = 0x2800
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_WRAP_S = 0x2802
GL_TEXTURE_WRAP_T = 0x2803
GL_COLOR_BUFFER_BIT = 0x4000
GL_CLAMP_TO_EDGE = 0x812F
GL_R8 = 0x8229
GL_TEXTURE0 = 0x84C0
GL_FRAGMENT_SHADER = 0x8B30
GL_VERTEX_SHADER = 0x8B31
GL_COMPILE_STATUS = 0x8B81
GL_LINK_STATUS = 0x8B82

# The same shaders serve desktop GL and GL ES; only the first lines differ.
HEADERS = {False: "#version 150\n", True: "#version 300 es\nprecision highp float;\n"}

# One triangle that covers the whole target, with no vertex buffer at all.
VERTEX = """
void main() {
    vec2 corner = vec2(float((gl_VertexID & 1) * 4 - 1), float((gl_VertexID & 2) * 2 - 1));
    gl_Position = vec4(corner, 0.0, 1.0);
}
"""

FRAGMENT = """
out vec4 color;
uniform sampler2D older;
uniform sampler2D newer;
uniform vec4 ranges;
uniform float blend;
uniform vec2 size;
uniform vec2 viewport;
uniform float shake;
uniform float pitch;
uniform float row_height;
uniform float top;
uniform float rows;
uniform vec2 lefts;
uniform vec2 columns;
uniform float max_radius;
uniform float corner;
uniform float feather;
uniform float floor_level;
uniform float gamma;
uniform vec3 tint;
uniform vec3 band_color;
uniform float band_middle;
uniform float band_alpha;

// A dot's brightness in one frame: five taps across its cell soften the
// camera's grain, then the frame's own range, a noise floor and a curve.
float level(sampler2D frame, vec2 uv, float low, float high) {
    vec2 texel = 2.0 / size;
    float value = texture(frame, uv).r * 0.4
        + (texture(frame, uv + vec2(texel.x, 0.0)).r + texture(frame, uv - vec2(texel.x, 0.0)).r
         + texture(frame, uv + vec2(0.0, texel.y)).r + texture(frame, uv - vec2(0.0, texel.y)).r) * 0.15;
    float stretched = clamp((value - low) / max(high - low, 0.02), 0.0, 1.0);
    return pow(clamp((stretched - floor_level) / (1.0 - floor_level), 0.0, 1.0), gamma);
}

void main() {
    // This pixel in the preview's own points, y down, following any shake.
    vec2 point = vec2(gl_FragCoord.x, viewport.y - gl_FragCoord.y) * size / viewport - vec2(shake, 0.0);
    float device = size.x / viewport.x;
    // The nearest dot is the nearest column in one of the two rows around it.
    float first = floor((point.y - top) / row_height);
    float nearest = 1e9;
    vec2 centre = vec2(0.0);
    for (int step = 0; step < 2; step++) {
        float row = clamp(first + float(step), 0.0, rows - 1.0);
        int odd = int(mod(row, 2.0));
        float column = clamp(floor((point.x - lefts[odd]) / pitch + 0.5), 0.0, columns[odd] - 1.0);
        vec2 candidate = vec2(lefts[odd] + column * pitch, top + row * row_height);
        float away = distance(point, candidate);
        if (away < nearest) {
            nearest = away;
            centre = candidate;
        }
    }
    vec2 uv = centre / size;
    float lit = mix(level(older, uv, ranges.x, ranges.y), level(newer, uv, ranges.z, ranges.w), blend);
    float radius = max_radius * sqrt(lit);
    float cover = radius < 0.45 ? 0.0 : clamp((radius - nearest) / device + 0.5, 0.0, 1.0);
    // The rounded rectangle's last `feather` points fade to nothing.
    vec2 inset = abs(point - size * 0.5) - (size * 0.5 - corner);
    float edge = length(max(inset, 0.0)) + min(max(inset.x, inset.y), 0.0) - corner;
    float fade = smoothstep(0.0, 1.0, clamp(-edge / feather, 0.0, 1.0));
    float band = max(0.0, 1.0 - abs(point.y - band_middle) / 36.0) * band_alpha;
    float alpha = cover * fade;
    color = vec4(mix(tint, band_color, band) * alpha, alpha);
}
"""


def frame_levels(pixels: bytes, low_share: float, high_share: float) -> tuple[float, float]:
    """The brightness (0..1) at two percentiles of a greyscale frame."""
    if numpy is not None:
        counts = numpy.cumsum(numpy.bincount(numpy.frombuffer(pixels, numpy.uint8)[::3], minlength=256))
        total = counts[-1]
        low = int(numpy.searchsorted(counts, low_share * total))
        high = int(numpy.searchsorted(counts, high_share * total))
    else:
        ordered = sorted(pixels[::17])
        low, high = ordered[int(low_share * (len(ordered) - 1))], ordered[int(high_share * (len(ordered) - 1))]
    return low / 255, max(low + 5, high) / 255


@dataclass
class Look:
    """Everything the draw pass needs that changes from frame to frame."""

    tint: tuple[float, float, float] = (1.0, 1.0, 1.0)
    band_color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    band_middle: float = -100.0
    band_alpha: float = 0.0
    shake: float = 0.0


class _GL:
    """The few GL entry points the shader uses, bound through ctypes."""

    SIGNATURES = {
        "glCreateShader": (ctypes.c_uint, [ctypes.c_uint]),
        "glShaderSource": (None, [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p), ctypes.c_void_p]),
        "glCompileShader": (None, [ctypes.c_uint]),
        "glGetShaderiv": (None, [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]),
        "glGetShaderInfoLog": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p]),
        "glDeleteShader": (None, [ctypes.c_uint]),
        "glCreateProgram": (ctypes.c_uint, []),
        "glAttachShader": (None, [ctypes.c_uint, ctypes.c_uint]),
        "glLinkProgram": (None, [ctypes.c_uint]),
        "glGetProgramiv": (None, [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]),
        "glGetProgramInfoLog": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_void_p, ctypes.c_char_p]),
        "glDeleteProgram": (None, [ctypes.c_uint]),
        "glUseProgram": (None, [ctypes.c_uint]),
        "glGetUniformLocation": (ctypes.c_int, [ctypes.c_uint, ctypes.c_char_p]),
        "glUniform1i": (None, [ctypes.c_int, ctypes.c_int]),
        "glUniform1f": (None, [ctypes.c_int, ctypes.c_float]),
        "glUniform2f": (None, [ctypes.c_int, ctypes.c_float, ctypes.c_float]),
        "glUniform3f": (None, [ctypes.c_int, ctypes.c_float, ctypes.c_float, ctypes.c_float]),
        "glUniform4f": (None, [ctypes.c_int, ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float]),
        "glGenVertexArrays": (None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
        "glBindVertexArray": (None, [ctypes.c_uint]),
        "glDeleteVertexArrays": (None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
        "glGenTextures": (None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
        "glDeleteTextures": (None, [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]),
        "glBindTexture": (None, [ctypes.c_uint, ctypes.c_uint]),
        "glActiveTexture": (None, [ctypes.c_uint]),
        "glTexParameteri": (None, [ctypes.c_uint, ctypes.c_uint, ctypes.c_int]),
        "glTexImage2D": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]),
        "glTexSubImage2D": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_char_p]),
        "glPixelStorei": (None, [ctypes.c_uint, ctypes.c_int]),
        "glGetIntegerv": (None, [ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]),
        "glDisable": (None, [ctypes.c_uint]),
        "glClearColor": (None, [ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float]),
        "glClear": (None, [ctypes.c_uint]),
        "glDrawArrays": (None, [ctypes.c_uint, ctypes.c_int, ctypes.c_int]),
    }

    def __init__(self, es: bool) -> None:
        # glvnd dispatches each API through its own library.
        name = ctypes.util.find_library("GLESv2" if es else "OpenGL") or ("libGLESv2.so.2" if es else "libOpenGL.so.0")
        library = ctypes.CDLL(name)
        self.header = HEADERS[es]
        for symbol, (result, arguments) in self.SIGNATURES.items():
            function = getattr(library, symbol)
            function.restype, function.argtypes = result, arguments
            setattr(self, symbol[2:], function)

    def compile(self, kind: int, source: str) -> int:
        shader = self.CreateShader(kind)
        text = ctypes.c_char_p((self.header + source).encode())
        self.ShaderSource(shader, 1, ctypes.byref(text), None)
        self.CompileShader(shader)
        status = ctypes.c_int()
        self.GetShaderiv(shader, GL_COMPILE_STATUS, ctypes.byref(status))
        if not status.value:
            log = ctypes.create_string_buffer(4096)
            self.GetShaderInfoLog(shader, 4096, None, log)
            raise RuntimeError(log.value.decode(errors="replace"))
        return shader

    def program(self) -> int:
        program = self.CreateProgram()
        shaders = (self.compile(GL_VERTEX_SHADER, VERTEX), self.compile(GL_FRAGMENT_SHADER, FRAGMENT))
        for shader in shaders:
            self.AttachShader(program, shader)
        self.LinkProgram(program)
        for shader in shaders:
            self.DeleteShader(shader)
        status = ctypes.c_int()
        self.GetProgramiv(program, GL_LINK_STATUS, ctypes.byref(status))
        if not status.value:
            log = ctypes.create_string_buffer(4096)
            self.GetProgramInfoLog(program, 4096, None, log)
            raise RuntimeError(log.value.decode(errors="replace"))
        return program

    def texture(self, width: int, height: int) -> int:
        name = ctypes.c_uint()
        self.GenTextures(1, ctypes.byref(name))
        self.BindTexture(GL_TEXTURE_2D, name.value)
        for parameter, value in ((GL_TEXTURE_MIN_FILTER, GL_LINEAR), (GL_TEXTURE_MAG_FILTER, GL_LINEAR), (GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE), (GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)):
            self.TexParameteri(GL_TEXTURE_2D, parameter, value)
        self.TexImage2D(GL_TEXTURE_2D, 0, GL_R8, width, height, 0, GL_RED, GL_UNSIGNED_BYTE, None)
        return name.value


class HalftoneArea(Gtk.GLArea):
    """The dots, for a frame of `width` x `height` on a given hexagonal grid."""

    def __init__(self, width: int, height: int, grid: dict[str, float], style: dict[str, float]) -> None:
        super().__init__()
        self.width, self.height = width, height
        self.grid, self.style = grid, style
        self.set_size_request(width, height)
        self.set_has_depth_buffer(False)
        self.set_auto_render(False)
        self.set_can_target(False)
        self.look = Look()
        self.failed = False
        self.ready = False
        self.gl: _GL | None = None
        # Frames arrive on any thread; they are uploaded on the next render.
        self.pending: bytes | None = None
        self.pending_range = (0.0, 1.0)
        self.reset()
        self.connect("realize", self._realize)
        self.connect("unrealize", self._unrealize)
        self.connect("render", self._render)

    # Feeding --------------------------------------------------------------------

    def submit(self, pixels: bytes) -> None:
        """Take the newest frame; it is uploaded on the next render."""
        self.pending_range = frame_levels(pixels, self.style["low_share"], self.style["high_share"])
        self.pending = pixels
        self.queue_render()

    def reset(self) -> None:
        self.pending = None
        self.frames = 0
        # The older and newer frames' brightness ranges, and when the newer came.
        self.ranges = [(0.0, 1.0), (0.0, 1.0)]
        self.arrived = 0.0
        self.interval = 1 / 30
        self.queue_render()

    # GL lifecycle ---------------------------------------------------------------

    def _realize(self, _area: Gtk.GLArea) -> None:
        self.make_current()
        error = self.get_error()
        if error is not None:
            self._fail(error.message)
            return
        try:
            gl = self.gl = _GL(self.get_context().get_use_es())
            self.program = gl.program()
            vao = ctypes.c_uint()
            gl.GenVertexArrays(1, ctypes.byref(vao))
            self.vao = vao.value
            gl.PixelStorei(GL_UNPACK_ALIGNMENT, 1)
            # The two most recent frames take turns being the newer one.
            self.textures = [gl.texture(self.width, self.height) for _ in range(2)]
            self.newer = 0
            self.uniforms: dict[str, int] = {}
            self.ready = True
        except (OSError, AttributeError, RuntimeError) as error:
            self._fail(str(error))

    def _fail(self, reason: str) -> None:
        self.failed = True
        self.ready = False
        print("[facegate] preview falls back to CPU drawing: %s" % reason.strip(), flush=True)

    def _unrealize(self, _area: Gtk.GLArea) -> None:
        if not self.ready or self.gl is None:
            return
        self.make_current()
        gl = self.gl
        gl.DeleteTextures(2, (ctypes.c_uint * 2)(*self.textures))
        gl.DeleteVertexArrays(1, ctypes.byref(ctypes.c_uint(self.vao)))
        gl.DeleteProgram(self.program)
        self.ready = False

    # Rendering ------------------------------------------------------------------

    def _uniform(self, name: str) -> int:
        if name not in self.uniforms:
            self.uniforms[name] = self.gl.GetUniformLocation(self.program, name.encode())
        return self.uniforms[name]

    def _render(self, _area: Gtk.GLArea, _context: Gdk.GLContext) -> bool:
        if not self.ready:
            return False
        gl = self.gl
        now = time.monotonic()
        pixels, self.pending = self.pending, None
        if pixels is not None:
            # The newest frame replaces the older one, which the blend had left.
            self.newer = 1 - self.newer
            gl.BindTexture(GL_TEXTURE_2D, self.textures[self.newer])
            gl.TexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, self.width, self.height, GL_RED, GL_UNSIGNED_BYTE, pixels)
            self.ranges = [self.ranges[1], self.pending_range]
            if self.frames:
                # Blend over the time frames really take, so each blend ends as the next frame lands.
                self.interval += (min(max(now - self.arrived, 1 / 120), 0.25) - self.interval) * 0.3
            self.arrived = now
            self.frames += 1

        viewport = (ctypes.c_int * 4)()
        gl.GetIntegerv(GL_VIEWPORT, viewport)
        gl.Disable(GL_BLEND)
        gl.ClearColor(0, 0, 0, 0)
        gl.Clear(GL_COLOR_BUFFER_BIT)
        if not self.frames:
            return True
        grid, style, look = self.grid, self.style, self.look
        uniform = self._uniform
        gl.UseProgram(self.program)
        gl.BindVertexArray(self.vao)
        older = 1 - self.newer if self.frames > 1 else self.newer
        for unit, texture in ((0, self.textures[older]), (1, self.textures[self.newer])):
            gl.ActiveTexture(GL_TEXTURE0 + unit)
            gl.BindTexture(GL_TEXTURE_2D, texture)
        gl.Uniform1i(uniform("older"), 0)
        gl.Uniform1i(uniform("newer"), 1)
        older_range = self.ranges[0] if self.frames > 1 else self.ranges[1]
        gl.Uniform4f(uniform("ranges"), *older_range, *self.ranges[1])
        gl.Uniform1f(uniform("blend"), min(1.0, (now - self.arrived) / self.interval))
        gl.Uniform2f(uniform("size"), self.width, self.height)
        gl.Uniform2f(uniform("viewport"), viewport[2], viewport[3])
        gl.Uniform2f(uniform("lefts"), grid["left_even"], grid["left_odd"])
        gl.Uniform2f(uniform("columns"), grid["columns_even"], grid["columns_odd"])
        for name in ("pitch", "row_height", "top", "rows"):
            gl.Uniform1f(uniform(name), grid[name])
        for name, key in (("max_radius", "max_radius"), ("corner", "corner"), ("feather", "feather"), ("floor_level", "floor"), ("gamma", "gamma")):
            gl.Uniform1f(uniform(name), style[key])
        gl.Uniform3f(uniform("tint"), *look.tint)
        gl.Uniform3f(uniform("band_color"), *look.band_color)
        gl.Uniform1f(uniform("band_middle"), look.band_middle)
        gl.Uniform1f(uniform("band_alpha"), look.band_alpha)
        gl.Uniform1f(uniform("shake"), look.shake)
        gl.DrawArrays(GL_TRIANGLES, 0, 3)
        gl.UseProgram(0)
        gl.BindVertexArray(0)
        return True
