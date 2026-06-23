"""
MXWendler media plugin: render a spinning OpenGL cube into a media surface.

Address a clip's media as:  generative://cube_spin_opengl

The host (mxw_cachedmedia_plugin) calls:
    onOpen(uri)           -> (width, height, length, fps, has_alpha)
    onRenderFrame(frame)  -> H*W*4 uint8 numpy buffer, BGRA byte order
    onClose()             -> None

Rendering uses ModernGL *attached to MXWendler's own OpenGL context*. We must
NOT create a standalone context: MXWendler makes its context current every
frame, so a standalone context would no longer be current when onRenderFrame
runs, and the GL objects (VBO/VAO/FBO) created in it would be dereferenced
against the wrong context -> driver crash (atio6axx.dll on AMD).

Instead we lazily call moderngl.create_context() on the first frame, when the
host guarantees MXWendler's GL context is current. All our GL objects then live
in that same context. We render into our own offscreen framebuffer, read it
back, and restore the default framebuffer so MXWendler's rendering is unaffected.

Install once with:
    pip install moderngl numpy
"""

import time
import math

import numpy as np
import moderngl


# ----------------------------------------------------------------------------------
# math helpers (row-major, textbook; uploaded transposed for column-major GLSL)
def perspective(fovy_deg, aspect, near, far):
    f = 1.0 / math.tan(math.radians(fovy_deg) / 2.0)
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0] = f / aspect
    m[1, 1] = f
    m[2, 2] = (far + near) / (near - far)
    m[2, 3] = (2 * far * near) / (near - far)
    m[3, 2] = -1.0
    return m


def translate(x, y, z):
    m = np.identity(4, dtype=np.float32)
    m[0, 3], m[1, 3], m[2, 3] = x, y, z
    return m


def rotate(angle_deg, x, y, z):
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    n = math.sqrt(x * x + y * y + z * z) or 1.0
    x, y, z = x / n, y / n, z / n
    m = np.identity(4, dtype=np.float32)
    m[0, 0] = c + x * x * (1 - c)
    m[0, 1] = x * y * (1 - c) - z * s
    m[0, 2] = x * z * (1 - c) + y * s
    m[1, 0] = y * x * (1 - c) + z * s
    m[1, 1] = c + y * y * (1 - c)
    m[1, 2] = y * z * (1 - c) - x * s
    m[2, 0] = z * x * (1 - c) - y * s
    m[2, 1] = z * y * (1 - c) + x * s
    m[2, 2] = c + z * z * (1 - c)
    return m


VERTEX_SHADER = """
#version 330
uniform mat4 mvp;
in vec3 in_pos;
in vec3 in_col;
out vec3 v_col;
void main() {
    v_col = in_col;
    gl_Position = mvp * vec4(in_pos, 1.0);
}
"""

FRAGMENT_SHADER = """
#version 330
in vec3 v_col;
out vec4 f_col;
void main() {
    f_col = vec4(v_col, 1.0);
}
"""


def _cube_geometry():
    # 8 corners, each given a colour so faces read as gradients
    v = np.array([
        [-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
        [-1, -1,  1], [1, -1,  1], [1, 1,  1], [-1, 1,  1],
    ], dtype=np.float32)
    c = np.array([
        [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
        [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],
    ], dtype=np.float32)
    idx = np.array([
        0, 1, 2, 2, 3, 0,   4, 5, 6, 6, 7, 4,
        0, 4, 7, 7, 3, 0,   1, 5, 6, 6, 2, 1,
        3, 2, 6, 6, 7, 3,   0, 1, 5, 5, 4, 0,
    ], dtype=np.int32)
    interleaved = np.hstack([v, c]).astype("f4")
    return interleaved.tobytes(), idx.tobytes()


# ----------------------------------------------------------------------------------
class cube_instance:
    def __init__(self):
        self.width = 1024
        self.height = 1024
        self.fps = 60.0
        # rotation is integrated over time so the clip playback speed can scale it
        # and reverse it: angle += dt * media_speed each frame. media_speed is set
        # per instance by the host via onSetSpeed().
        self.angle = 0.0
        self.last_time = time.monotonic()
        self.media_speed = 1.0
        self.ctx = None
        self.prog = None
        self.vao = None
        self.fbo = None
        self.fbo_size = None   # (w, h) the current fbo was built for

    def ensure_gl(self):
        # build GL objects lazily, on the render thread, with MXWendler's
        # context current -> create_context() attaches to *that* context.
        if self.ctx is None:
            self.ctx = moderngl.create_context()

            self.prog = self.ctx.program(
                vertex_shader=VERTEX_SHADER, fragment_shader=FRAGMENT_SHADER)

            vbo_data, ibo_data = _cube_geometry()
            vbo = self.ctx.buffer(vbo_data)
            ibo = self.ctx.buffer(ibo_data)
            self.vao = self.ctx.vertex_array(
                self.prog, [(vbo, "3f 3f", "in_pos", "in_col")], ibo)

        # (re)build the offscreen framebuffer whenever the render size changed
        if self.fbo is None or self.fbo_size != (self.width, self.height):
            color = self.ctx.texture((self.width, self.height), 4)
            depth = self.ctx.depth_renderbuffer((self.width, self.height))
            self.fbo = self.ctx.framebuffer(color_attachments=[color], depth_attachment=depth)
            self.fbo_size = (self.width, self.height)


storage = {}


def onOpen(uri):
    # do NOT create the GL context here: defer to the first onRenderFrame, where
    # MXWendler guarantees its own GL context is current.
    inst = cube_instance()
    storage[media_id] = inst

    # width, height, length(frames), fps, has_alpha
    return (inst.width, inst.height, 1, inst.fps, True)


def onRenderFrame(frame):
    inst = storage.get(media_id)
    if inst is None:
        return np.zeros((1024, 1024, 4), dtype=np.uint8)

    inst.ensure_gl()

    # integrate rotation scaled by the clip playback speed (set by the host as the
    # module global 'media_speed'). speed 0 freezes, negative spins backwards.
    now = time.monotonic()
    dt = now - inst.last_time
    inst.last_time = now
    inst.angle += dt * inst.media_speed

    t = inst.angle
    aspect = inst.width / float(inst.height)

    model = rotate(t * 40.0, 1, 0, 0) @ rotate(t * 55.0, 0, 1, 0)
    view = translate(0, 0, -4.5)
    proj = perspective(45.0, aspect, 0.1, 100.0)
    mvp = proj @ view @ model

    # render into our own offscreen framebuffer (lives in MXWendler's context)
    inst.ctx.enable(moderngl.DEPTH_TEST)
    inst.fbo.use()
    inst.ctx.clear(0.0, 0.0, 0.0, 0.0, depth=1.0)
    # GLSL is column-major -> upload the transpose of our row-major matrices
    inst.prog["mvp"].write(np.ascontiguousarray(mvp.T).tobytes())
    inst.vao.render()

    # read back RGBA, origin bottom-left -> flip to top-down, then RGBA -> BGRA
    raw = inst.fbo.read(components=4, alignment=1)
    img = np.frombuffer(raw, dtype=np.uint8).reshape((inst.height, inst.width, 4))
    img = np.flipud(img)
    bgra = img[:, :, [2, 1, 0, 3]]
    # the host (mxw_cachedmedia_plugin) snapshots and restores all GL state
    # around this call, so we don't need to unbind our fbo / reset enables here.
    return np.ascontiguousarray(bgra)


def onSizeChange(w, h):
    # the host changed our render size. just record it; ensure_gl() rebuilds the
    # offscreen framebuffer at the new size on the next frame.
    inst = storage.get(media_id)
    if inst is None:
        return
    inst.width = int(w)
    inst.height = int(h)


def onSetSpeed(speed):
    # the host changed the clip playback speed. store it per instance: scales the
    # rotation, 0 freezes the spin and negative values spin it backwards.
    inst = storage.get(media_id)
    if inst is None:
        return
    inst.media_speed = float(speed)


def onClose():
    inst = storage.pop(media_id, None)
    if inst is None:
        return
    # objects live in MXWendler's context; releasing the wrapper is enough.
    # do NOT release the context itself -- we did not create it.
    inst.ctx = None
    inst.prog = None
    inst.vao = None
    inst.fbo = None
